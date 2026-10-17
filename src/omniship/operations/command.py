import asyncio
import os
import time
from pathlib import Path

from pydantic import BaseModel, Field

from omniship.core.artifact import Artifact
from omniship.core.context import ExecutionContext
from omniship.core.node import NodeInputs
from omniship.core.process import new_process_group, stream_process
from omniship.core.result import NodeResult, NodeStatus
from omniship.core.stage import Stage
from omniship.plugins.metadata import OperationDefinition


class CommandConfig(BaseModel):
    run: str | list[str] = Field(
        ...,
        description="Shell command or argument list to execute",
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory relative to workspace root",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables",
    )
    artifacts: list[str] = Field(
        default_factory=list,
        description="Artifact file or directory paths produced by this command",
    )
    timeout: float | None = Field(
        default=None,
        description="Execution timeout in seconds",
    )


class CommandOperation:
    name: str = "core/command"
    stages: frozenset[Stage] = frozenset({Stage.CHECK, Stage.BUILD, Stage.SHIP})
    cacheable: bool = False

    async def execute(
        self,
        context: ExecutionContext,
        inputs: NodeInputs,
    ) -> NodeResult:
        start_time = time.monotonic()
        try:
            cfg = CommandConfig.model_validate(inputs.params)
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                duration=time.monotonic() - start_time,
                error_message=f"Invalid command configuration: {e}",
            )

        working_dir = (
            context.workspace_root / cfg.cwd if cfg.cwd else context.workspace_root
        )
        working_dir = working_dir.resolve()

        merged_env = os.environ.copy()
        merged_env.update(context.env)
        merged_env.update(cfg.env)

        try:
            if isinstance(cfg.run, str):
                proc = await asyncio.create_subprocess_shell(
                    cfg.run,
                    cwd=str(working_dir),
                    env=merged_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **new_process_group(),
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cfg.run,
                    cwd=str(working_dir),
                    env=merged_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **new_process_group(),
                )

            output = await stream_process(proc, context, timeout=cfg.timeout)

            duration = time.monotonic() - start_time
            stdout = output.stdout
            stderr = output.stderr

            if output.timed_out:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    duration=duration,
                    stdout=stdout,
                    stderr=stderr,
                    error_message=f"Command timed out after {cfg.timeout}s",
                )

            if output.return_code != 0:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    duration=duration,
                    stdout=stdout,
                    stderr=stderr,
                    error_message=(
                        f"Command failed with exit code {output.return_code}"
                    ),
                )

            # Validate artifacts
            artifacts: list[Artifact] = []
            for art_path_str in cfg.artifacts:
                art_path = Path(art_path_str)
                if not art_path.is_absolute():
                    resolved_art_path = (working_dir / art_path).resolve()
                else:
                    resolved_art_path = art_path.resolve()

                if not resolved_art_path.exists():
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        duration=duration,
                        stdout=stdout,
                        stderr=stderr,
                        error_message=f"Artifact declared at '{art_path_str}' was not found after command completed",
                    )
                artifacts.append(
                    Artifact.from_path(
                        resolved_art_path,
                        name=art_path_str,
                        metadata={"declared_path": art_path_str},
                    )
                )

            return NodeResult(
                status=NodeStatus.SUCCESS,
                duration=duration,
                stdout=stdout,
                stderr=stderr,
                artifacts=tuple(artifacts),
            )

        except Exception as exc:
            duration = time.monotonic() - start_time
            return NodeResult(
                status=NodeStatus.FAILED,
                duration=duration,
                error_message=str(exc),
            )


def get_command_definition() -> OperationDefinition:
    return OperationDefinition(
        name=CommandOperation.name,
        stages=CommandOperation.stages,
        description="Run arbitrary shell commands or executable arguments",
        config_model=CommandConfig,
        cacheable=False,
    )
