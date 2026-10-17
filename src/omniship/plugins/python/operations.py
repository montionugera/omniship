import asyncio
import os
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from omniship.core.artifact import Artifact
from omniship.core.context import ExecutionContext
from omniship.core.node import NodeInputs
from omniship.core.process import stream_process
from omniship.core.result import NodeResult, NodeStatus
from omniship.core.stage import Stage
from omniship.plugins.metadata import OperationDefinition
from omniship.plugins.python.runtime import Python
from omniship.plugins.python.wheels import publish_staged_wheels
from omniship.plugins.tooling import FacadeOperation


class RuffConfig(BaseModel):
    fix: bool = False


class PytestConfig(BaseModel):
    coverage: bool = False
    minimum_coverage: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_coverage(self):
        if self.minimum_coverage is not None and not self.coverage:
            raise ValueError("minimum_coverage requires coverage=true")
        return self


class WheelConfig(BaseModel):
    verify: bool = True
    output: str = "dist"


class PyPIPublishConfig(BaseModel):
    files: list[str] = Field(default_factory=list)
    publish_url: str | None = None
    trusted_publishing: bool = False
    dry_run: bool = False


# Tools run in the project's environment through uv: OmniShip itself may run
# from an isolated uvx environment without the project's dependencies.
async def _execute_tool(
    context: ExecutionContext,
    arguments: list[str],
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=context.workspace_root,
        env={**os.environ, **context.env},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output = await stream_process(process, context)
    return (
        output.return_code,
        output.stdout,
        output.stderr,
    )


class RuffOperation:
    name = "python/ruff"
    stages = frozenset({Stage.CHECK})
    cacheable = False

    async def execute(
        self, context: ExecutionContext, inputs: NodeInputs
    ) -> NodeResult:
        started = time.monotonic()
        try:
            config = RuffConfig.model_validate(inputs.params)
        except Exception as exc:
            return _invalid(started, exc)
        arguments = ["uv", "run", "ruff", "check", "."]
        if config.fix:
            arguments.append("--fix")
        return await _command_result(context, arguments, started)


class PytestOperation:
    name = "python/pytest"
    stages = frozenset({Stage.CHECK})
    cacheable = False

    async def execute(
        self, context: ExecutionContext, inputs: NodeInputs
    ) -> NodeResult:
        started = time.monotonic()
        try:
            config = PytestConfig.model_validate(inputs.params)
        except Exception as exc:
            return _invalid(started, exc)
        arguments = ["uv", "run", "python", "-m", "pytest"]
        if config.coverage:
            arguments.append("--cov=.")
        if config.minimum_coverage is not None:
            arguments.append(f"--cov-fail-under={config.minimum_coverage}")
        return await _command_result(context, arguments, started)


class WheelOperation:
    name = "python/wheel"
    stages = frozenset({Stage.BUILD})
    cacheable = False

    async def execute(
        self, context: ExecutionContext, inputs: NodeInputs
    ) -> NodeResult:
        started = time.monotonic()
        try:
            config = WheelConfig.model_validate(inputs.params)
        except Exception as exc:
            return _invalid(started, exc)
        output = (context.workspace_root / config.output).resolve()
        if not output.is_relative_to(context.workspace_root.resolve()):
            return NodeResult(
                status=NodeStatus.FAILED,
                duration=time.monotonic() - started,
                error_message="Wheel output path escapes workspace",
            )
        wheels: list[Path] = []
        try:
            with tempfile.TemporaryDirectory(
                prefix=".omniship-wheel-",
                dir=context.workspace_root,
            ) as staging_directory:
                staging = Path(staging_directory)
                arguments = ["uv", "build", "--wheel", "--out-dir", str(staging)]
                return_code, stdout, stderr = await _execute_tool(context, arguments)
                if return_code == 0:
                    wheels = publish_staged_wheels(staging, output)
        except Exception as exc:
            return NodeResult(
                status=NodeStatus.FAILED,
                duration=time.monotonic() - started,
                error_message=str(exc),
            )
        if return_code != 0:
            return NodeResult(
                status=NodeStatus.FAILED,
                duration=time.monotonic() - started,
                stdout=stdout,
                stderr=stderr,
                error_message=f"Python wheel build failed with exit code {return_code}",
            )
        if config.verify and not wheels:
            return NodeResult(
                status=NodeStatus.FAILED,
                duration=time.monotonic() - started,
                stdout=stdout,
                stderr=stderr,
                error_message="Wheel build produced no .whl files",
            )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            duration=time.monotonic() - started,
            stdout=stdout,
            stderr=stderr,
            artifacts=tuple(Artifact.from_path(path) for path in wheels),
        )


def _invalid(started: float, exc: Exception) -> NodeResult:
    return NodeResult(
        status=NodeStatus.FAILED,
        duration=time.monotonic() - started,
        error_message=f"Invalid Python operation configuration: {exc}",
    )


async def _command_result(
    context: ExecutionContext,
    arguments: list[str],
    started: float,
) -> NodeResult:
    try:
        return_code, stdout, stderr = await _execute_tool(context, arguments)
    except Exception as exc:
        return NodeResult(
            status=NodeStatus.FAILED,
            duration=time.monotonic() - started,
            error_message=str(exc),
        )
    return NodeResult(
        status=NodeStatus.SUCCESS if return_code == 0 else NodeStatus.FAILED,
        duration=time.monotonic() - started,
        stdout=stdout,
        stderr=stderr,
        error_message=(
            None if return_code == 0 else f"Tool failed with exit code {return_code}"
        ),
    )


def get_python_definitions() -> tuple[OperationDefinition, ...]:
    return (
        OperationDefinition(
            name="python/ruff",
            stages=RuffOperation.stages,
            description="Lint a Python project with Ruff",
            config_model=RuffConfig,
        ),
        OperationDefinition(
            name="python/pytest",
            stages=PytestOperation.stages,
            description="Test a Python project with pytest",
            config_model=PytestConfig,
        ),
        OperationDefinition(
            name="python/wheel",
            stages=WheelOperation.stages,
            description="Build Python wheel artifacts",
            config_model=WheelConfig,
        ),
        OperationDefinition(
            name="python/pypi-publish",
            stages=frozenset({Stage.SHIP}),
            description="Publish Python distributions to a package index",
            config_model=PyPIPublishConfig,
        ),
    )


def get_pypi_publish_operation() -> FacadeOperation:
    return FacadeOperation(
        "python/pypi-publish",
        Stage.SHIP,
        PyPIPublishConfig,
        lambda ctx, cfg: Python(ctx).publish(
            files=cfg.files,
            publish_url=cfg.publish_url,
            trusted_publishing=cfg.trusted_publishing,
            dry_run=cfg.dry_run,
        ),
    )
