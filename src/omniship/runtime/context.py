from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from omniship.core.artifact import Artifact
from omniship.core.execution import ExecutionHost
from omniship.core.logging import LogLevel, LogStream


class TaskFailure(Exception):
    pass


@dataclass
class TaskLog:
    lines: list[str] = field(default_factory=list)
    _emit: Callable[[LogLevel, str, LogStream], None] | None = None

    def _write(
        self,
        level: LogLevel,
        message: str,
        stream: LogStream = LogStream.LOG,
    ) -> None:
        text = str(message)
        self.lines.append(text)
        if self._emit is not None:
            self._emit(level, text, stream)

    def debug(self, message: str) -> None:
        self._write(LogLevel.DEBUG, message)

    def info(self, message: str) -> None:
        self._write(LogLevel.INFO, message)

    def warning(self, message: str) -> None:
        self._write(LogLevel.WARNING, message)

    def error(self, message: str) -> None:
        self._write(LogLevel.ERROR, message)

    def output(self, message: str, *, stderr: bool = False) -> None:
        stream = LogStream.STDERR if stderr else LogStream.STDOUT
        self._write(LogLevel.INFO, message, stream)


@dataclass
class TaskArtifacts:
    workspace_root: Path
    existing: tuple[Artifact, ...] = ()
    values: list[Artifact] = field(default_factory=list)

    def __iter__(self) -> Iterator[Artifact]:
        return iter((*self.existing, *self.values))

    def get(self, name: str) -> Artifact | None:
        return next((artifact for artifact in self if artifact.name == name), None)

    def add(
        self,
        path: str | Path,
        name: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> Artifact:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.workspace_root):
            raise TaskFailure(f"Artifact path escapes workspace: {path}")
        if not candidate.exists():
            raise TaskFailure(f"Artifact does not exist: {path}")
        artifact = Artifact.from_path(candidate, name=name, metadata=metadata)
        self.values.append(artifact)
        return artifact


@dataclass
class TaskOutputs:
    """Named values a task can expose to dependent jobs."""

    output_file: str | None = None
    values: dict[str, str] = field(default_factory=dict)

    def set(self, name: str, value: str) -> None:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise ValueError("task output name must be an identifier")
        if not isinstance(value, str):
            raise TypeError("task output value must be a string")
        self.values[name] = value
        if self.output_file is None:
            return
        if "\n" in value or "\r" in value:
            delimiter = f"omniship_{uuid4().hex}"
            entry = f"{name}<<{delimiter}\n{value}\n{delimiter}\n"
        else:
            entry = f"{name}={value}\n"
        with Path(self.output_file).open("a", encoding="utf-8") as output:
            output.write(entry)

    def get(self, name: str) -> str | None:
        return self.values.get(name)


@dataclass(frozen=True)
class GitTools:
    workspace_root: Path

    def _read(self, *arguments: str) -> str:
        process = subprocess.run(
            ["git", *arguments],
            cwd=self.workspace_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            raise TaskFailure(process.stderr.strip() or "Git command failed")
        return process.stdout.strip()

    def branch(self) -> str:
        return self._read("branch", "--show-current")

    def tags(self) -> list[str]:
        output = self._read("tag", "--list")
        return output.splitlines() if output else []

    def remote_url(self) -> str:
        return self._read("remote", "get-url", "origin")

    def changelog(self) -> str:
        return self._read("log", "--pretty=format:%s")


class TaskContext:
    def __init__(
        self,
        workspace_root: Path,
        env: Mapping[str, str],
        artifacts: Iterable[Artifact] = (),
        inputs: Mapping[str, Any] | None = None,
        log_sink: Callable[[LogLevel, str, LogStream], None] | None = None,
        host: ExecutionHost | None = None,
    ) -> None:
        self.workspace = workspace_root.resolve()
        self.env = dict(env)
        self.log = TaskLog(_emit=log_sink)
        self.artifacts = TaskArtifacts(self.workspace, tuple(artifacts))
        output_file = self.env.get("OMNISHIP_OUTPUT")
        reference = re.fullmatch(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
            output_file or "",
        )
        if reference is not None:
            output_file = self.env.get(reference.group(1) or reference.group(2))
        self.outputs = TaskOutputs(output_file or None)
        self.inputs = dict(inputs or {})
        self.git = GitTools(self.workspace)
        self.host = host or ExecutionHost.detect()

    def fail(self, message: str) -> None:
        raise TaskFailure(message)
