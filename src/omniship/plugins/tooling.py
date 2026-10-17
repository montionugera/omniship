"""Shared implementation helpers for official command-line tool plugins."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel

from omniship.core.context import ExecutionContext
from omniship.core.node import NodeInputs
from omniship.core.result import NodeResult, NodeStatus
from omniship.core.stage import Stage
from omniship.runtime import TaskContext, TaskFailure


class ToolFacade:
    """Base for imperative facades that execute one external CLI safely."""

    def __init__(self, context: TaskContext) -> None:
        self.context = context

    def _run(
        self,
        arguments: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> str:
        process_env = {**self.context.env, **dict(env or {})}
        # Resolve against the task PATH; this also finds npm.cmd on Windows.
        executable = (
            shutil.which(arguments[0], path=process_env.get("PATH")) or arguments[0]
        )
        process = subprocess.Popen(
            [executable, *arguments[1:]],
            cwd=self.context.workspace,
            env=process_env,
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
            self.context.log.output(line.rstrip("\r\n"))
        if process.wait() != 0:
            raise TaskFailure("".join(output).strip() or f"{arguments[0]} failed")
        return "".join(output)

    def _require_env(self, name: str, *, dry_run: bool) -> None:
        if not dry_run and not self.context.env.get(name):
            raise TaskFailure(f"{name} is required when dry_run is false")

    def _add_artifacts(self, paths: Sequence[str]) -> None:
        if isinstance(paths, (str, bytes)):
            raise TypeError("artifact paths must be an iterable of paths")
        for path in paths:
            self.context.artifacts.add(path)


class FacadeOperation:
    """Operation adapter that invokes an imperative facade in a worker thread."""

    cacheable = False

    def __init__(
        self,
        name: str,
        stage: Stage,
        config_model: type[BaseModel],
        invoke: Callable[[TaskContext, BaseModel], None],
    ) -> None:
        self.name = name
        self.stages = frozenset({stage})
        self.config_model = config_model
        self.invoke = invoke

    async def execute(
        self,
        context: ExecutionContext,
        inputs: NodeInputs,
    ) -> NodeResult:
        started = time.monotonic()
        try:
            config = self.config_model.model_validate(inputs.params)
            task_context = TaskContext(
                context.workspace_root,
                {**os.environ, **context.env},
                context.artifacts.to_list(),
                context.inputs,
                context.emit_log,
                host=context.host,
            )
            await asyncio.to_thread(self.invoke, task_context, config)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                duration=time.monotonic() - started,
                outputs=dict(task_context.outputs.values),
                artifacts=tuple(task_context.artifacts.values),
                stdout="\n".join(task_context.log.lines),
            )
        except Exception as exc:
            return NodeResult(
                status=NodeStatus.FAILED,
                duration=time.monotonic() - started,
                error_message=f"{type(exc).__name__}: {exc}",
                stderr=traceback.format_exc(),
            )


def relative_file(value: str, *, field_name: str) -> str:
    """Validate a workspace-relative configuration path without resolving it."""

    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or re.match(r"^[A-Za-z]:/", normalized)
    ):
        raise ValueError(f"{field_name} must stay inside the workspace")
    return normalized


def exact_version(value: str, *, tool: str) -> str:
    """Reject aliases and ranges so setup remains reproducible."""

    if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.+-]+)?", value) is None:
        raise ValueError(f"{tool} version must be an exact version")
    return value
