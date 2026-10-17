from __future__ import annotations

import subprocess
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from omniship.plugins.python.wheels import publish_staged_wheels
from omniship.runtime.context import TaskFailure

if TYPE_CHECKING:
    from omniship.runtime.context import TaskContext, TaskLog


@dataclass(frozen=True)
class ProjectTools:
    workspace_root: Path

    def version(self) -> str:
        path = self.workspace_root / "pyproject.toml"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            version = data["project"]["version"]
        except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
            raise TaskFailure(
                "Could not read [project].version from pyproject.toml"
            ) from exc
        if not isinstance(version, str) or not version:
            raise TaskFailure("Project version must be a non-empty string")
        return version


@dataclass(frozen=True)
class PythonTools:
    workspace_root: Path
    env: Mapping[str, str]
    log: TaskLog | None = None

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            arguments,
            cwd=self.workspace_root,
            env=dict(self.env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        output: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            output.append(line)
            if self.log is not None:
                self.log.output(line.rstrip("\r\n"))
        return_code = process.wait()
        stdout = "".join(output)
        if return_code != 0:
            raise TaskFailure(stdout.strip() or "Python tool failed")
        return subprocess.CompletedProcess(
            arguments,
            return_code,
            stdout=stdout,
            stderr="",
        )

    # Tools run in the project's environment through uv: OmniShip itself may
    # run from an isolated uvx environment without the project's dependencies.
    def pytest(self) -> None:
        self._run(["uv", "run", "python", "-m", "pytest"])

    def pyright(self) -> None:
        self._run(["uv", "run", "pyright"])

    def ruff(self, *, fix: bool = False) -> None:
        arguments = ["uv", "run", "ruff", "check", "."]
        if fix:
            arguments.append("--fix")
        self._run(arguments)

    def build_wheels(self, output: str = "dist") -> list[Path]:
        output_path = (self.workspace_root / output).resolve()
        if not output_path.is_relative_to(self.workspace_root.resolve()):
            raise TaskFailure("Wheel output path escapes workspace")
        with tempfile.TemporaryDirectory(
            prefix=".omniship-wheel-",
            dir=self.workspace_root,
        ) as staging_directory:
            staging = Path(staging_directory)
            self._run(["uv", "build", "--wheel", "--out-dir", str(staging)])
            wheels = publish_staged_wheels(staging, output_path)
        if not wheels:
            raise TaskFailure("Wheel build produced no .whl files")
        return wheels

    def build_wheel(self, output: str = "dist") -> Path:
        wheels = self.build_wheels(output)
        if len(wheels) != 1:
            raise TaskFailure(f"Expected one wheel, found {len(wheels)}")
        return wheels[0]


class Python(PythonTools):
    """Typed Python capability facade for an imperative task context."""

    def __init__(self, context: TaskContext) -> None:
        super().__init__(context.workspace, context.env, context.log)
        self.context = context
        self.project = ProjectTools(context.workspace)

    def publish(
        self,
        *,
        files: Iterable[str] = (),
        publish_url: str | None = None,
        trusted_publishing: bool = False,
        dry_run: bool = False,
    ) -> None:
        if (
            not trusted_publishing
            and not dry_run
            and not self.env.get("UV_PUBLISH_TOKEN")
        ):
            raise TaskFailure(
                "UV_PUBLISH_TOKEN is required when dry_run is false"
            )
        selected = tuple(files)
        if not selected:
            selected = tuple(
                str(artifact.path)
                for artifact in self.context.artifacts
                if artifact.path.is_file()
                and (
                    artifact.path.suffix == ".whl"
                    or artifact.path.name.endswith(".tar.gz")
                )
            )
        if not selected:
            # Without files uv publishes dist/*, which may hold stale builds.
            raise TaskFailure(
                "No Python distributions to publish; pass files or provide "
                "wheel or sdist artifacts"
            )
        arguments = ["uv", "publish", *selected]
        if publish_url is not None:
            arguments.extend(["--publish-url", publish_url])
        if trusted_publishing:
            arguments.extend(["--trusted-publishing", "always"])
        if dry_run:
            arguments.append("--dry-run")
        self._run(arguments)
