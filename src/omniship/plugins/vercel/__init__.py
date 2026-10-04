"""Vercel Build Output API artifacts and prebuilt deployment.

Application builds run only in Build. Ship stages an imported output artifact
and deploys it without downloading project environments or rebuilding source.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from threading import Thread
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from omniship.core.artifact import Artifact
from omniship.core.stage import Stage
from omniship.plugins.metadata import OperationDefinition
from omniship.plugins.registry import PluginRegistry
from omniship.plugins.tooling import (
    FacadeOperation,
    ToolFacade,
    exact_version,
    relative_file,
)
from omniship.runtime import TaskFailure
from omniship.workflow.errors import WorkflowError
from omniship.workflow.model import NodeSpec


@dataclass(frozen=True)
class VercelToolchain:
    """Install an exact CLI version; provision Node.js separately."""

    version: str
    name: str = "vercel/toolchain"

    def __post_init__(self) -> None:
        exact_version(self.version, tool="Vercel")

    def github(self) -> dict[str, str]:
        return {
            "name": "Set up Vercel",
            "run": f"npm install --global vercel@{self.version}",
        }


class _ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    team_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    target: str = Field(default="preview", pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    artifact: str = Field(default="output", min_length=1)


class _BuildConfig(_ProjectConfig):
    directory: str = "."
    pull: bool = True


class _DeployConfig(_ProjectConfig):
    pass


def _verify_output(path: Path) -> None:
    if path.is_symlink() or any(item.is_symlink() for item in path.rglob("*")):
        raise TaskFailure("Vercel output cannot contain a symlink")
    config = path / "config.json"
    if not config.is_file():
        raise TaskFailure("Vercel output is missing config.json")
    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise TaskFailure("Invalid Vercel output config.json") from exc
    if not isinstance(document, dict) or document.get("version") != 3:
        raise TaskFailure("Vercel output must use Build Output API version 3")


class Vercel(ToolFacade):
    """Imperative Vercel capabilities; authentication comes from VERCEL_TOKEN.

    Project and team IDs may be declared or supplied through VERCEL_PROJECT_ID
    and VERCEL_ORG_ID. No login or interactive project linking is performed.
    """

    def _environment(self, cfg: _ProjectConfig) -> dict[str, str]:
        self._require_env("VERCEL_TOKEN", dry_run=False)
        env = dict(self.context.env)
        if cfg.project_id is not None:
            env["VERCEL_PROJECT_ID"] = cfg.project_id
        if cfg.team_id is not None:
            env["VERCEL_ORG_ID"] = cfg.team_id
        for name in ("VERCEL_PROJECT_ID", "VERCEL_ORG_ID"):
            if not env.get(name):
                raise TaskFailure(f"{name} is required")
        env["CI"] = "1"
        env["VERCEL_TELEMETRY_DISABLED"] = "1"
        return env

    def _workspace_path(self, value: str) -> Path:
        path = self.context.workspace / relative_file(value, field_name="directory")
        if not path.resolve().is_relative_to(self.context.workspace):
            raise TaskFailure("Vercel path escapes workspace")
        return path.resolve()

    def _command(self, args: list[str], *, env: dict[str, str]) -> str:
        """Keep the token out of argv and redact it from both output streams."""
        executable = shutil.which(
            "vercel", path=env.get("PATH", os.environ.get("PATH"))
        )
        if executable is None:
            raise TaskFailure("Vercel CLI is missing; install VercelToolchain")
        token = env["VERCEL_TOKEN"]
        process = subprocess.Popen(
            [executable, *args],
            cwd=self.context.workspace,
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        stdout: list[str] = []
        stderr: list[str] = []

        def read(stream, lines, *, is_error=False):
            for line in stream:
                safe = line.replace(token, "[REDACTED]")
                lines.append(safe)
                self.context.log.output(safe.rstrip("\r\n"), stderr=is_error)
            stream.close()

        assert process.stdout is not None and process.stderr is not None
        reader = Thread(
            target=read, args=(process.stderr, stderr), kwargs={"is_error": True}
        )
        reader.start()
        read(process.stdout, stdout)
        code = process.wait()
        reader.join()
        if code != 0:
            raise TaskFailure(
                "".join([*stderr, *stdout]).strip() or "Vercel command failed"
            )
        return "".join(stdout)

    def build(
        self,
        *,
        directory: str = ".",
        target: str = "preview",
        project_id: str | None = None,
        team_id: str | None = None,
        artifact: str = "output",
        pull: bool = True,
    ) -> Artifact:
        """Pull settings and build fresh output; register only deployable files.

        ``pull=False`` uses previously prepared project settings. Downloaded
        environment files remain in the project's .vercel cache and are never
        included in the registered artifact. Successful outputs are retained
        under .omniship for artifact export.
        """
        cfg = _BuildConfig(
            directory=directory,
            target=target,
            project_id=project_id,
            team_id=team_id,
            artifact=artifact,
            pull=pull,
        )
        env = self._environment(cfg)
        project = self._workspace_path(cfg.directory)
        if not project.is_dir():
            raise TaskFailure("Vercel project directory does not exist")
        cache = self._workspace_path(f"{cfg.directory}/.vercel")
        if any(
            not item.resolve().is_relative_to(self.context.workspace)
            for item in cache.rglob("*")
        ):
            raise TaskFailure("Vercel project cache escapes workspace")
        root = self._workspace_path(".omniship")
        root.mkdir(exist_ok=True)
        build_root = Path(mkdtemp(prefix="vercel-build-", dir=root))
        output = build_root / "output"
        try:
            if cfg.pull:
                self._command(
                    [
                        "pull",
                        "--yes",
                        "--environment",
                        cfg.target,
                        "--cwd",
                        str(project),
                    ],
                    env=env,
                )
            args = [
                "build",
                "--yes",
                "--standalone",
                "--cwd",
                str(project),
                "--output",
                str(output),
            ]
            if cfg.target == "production":
                args.append("--prod")
            elif cfg.target != "preview":
                args.extend(["--target", cfg.target])
            self._command(args, env=env)
            _verify_output(output)
            return self.context.artifacts.add(
                output,
                name=cfg.artifact,
                metadata={
                    "vercel.target": cfg.target,
                    "vercel.project": env["VERCEL_PROJECT_ID"],
                    "vercel.team": env["VERCEL_ORG_ID"],
                },
            )
        except Exception:
            shutil.rmtree(build_root)
            raise

    def deploy(
        self,
        *,
        artifact: str = "output",
        target: str = "preview",
        project_id: str | None = None,
        team_id: str | None = None,
    ) -> str:
        """Deploy a registered prebuilt artifact and expose deployment_url."""
        cfg = _DeployConfig(
            artifact=artifact, target=target, project_id=project_id, team_id=team_id
        )
        env = self._environment(cfg)
        matches = [item for item in self.context.artifacts if item.name == cfg.artifact]
        if len(matches) != 1:
            raise TaskFailure(
                f"Expected exactly one Vercel artifact named {cfg.artifact}"
            )
        selected = matches[0]
        if not selected.path.resolve().is_relative_to(self.context.workspace):
            raise TaskFailure("Vercel artifact escapes workspace")
        _verify_output(selected.path)
        for key, expected in {
            "vercel.target": cfg.target,
            "vercel.project": env["VERCEL_PROJECT_ID"],
            "vercel.team": env["VERCEL_ORG_ID"],
        }.items():
            if key in selected.metadata and selected.metadata[key] != expected:
                raise TaskFailure(f"Vercel artifact {key} does not match deployment")
        with TemporaryDirectory(
            prefix="vercel-deploy-", dir=self.context.workspace
        ) as temporary:
            staging = Path(temporary)
            shutil.copytree(selected.path, staging / ".vercel/output")
            (staging / ".vercel/project.json").write_text(
                json.dumps(
                    {
                        "projectId": env["VERCEL_PROJECT_ID"],
                        "orgId": env["VERCEL_ORG_ID"],
                    }
                ),
                encoding="utf-8",
            )
            args = ["deploy", "--prebuilt", "--yes", "--cwd", str(staging)]
            if cfg.target == "production":
                args.append("--prod")
            elif cfg.target != "preview":
                args.extend(["--target", cfg.target])
            url = self._command(args, env=env).strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(char.isspace() for char in url)
        ):
            raise TaskFailure("Vercel did not return a valid deployment URL")
        self.context.outputs.set("deployment_url", url)
        return url


@dataclass(frozen=True)
class VercelBuild:
    directory: str = "."
    target: str = "preview"
    project_id: str | None = None
    team_id: str | None = None
    artifact: str = "output"
    pull: bool = True
    name: str = "vercel-build"

    def compile(self, stage: Stage, workspace_root: Path) -> list[NodeSpec]:
        if stage != Stage.BUILD:
            raise WorkflowError("VercelBuild can only be used in the build stage")
        params = asdict(self)
        params.pop("name")
        cfg = _BuildConfig.model_validate(params)
        relative_file(cfg.directory, field_name="directory")
        return [NodeSpec(self.name, stage, "vercel/build", cfg.model_dump(mode="json"))]


@dataclass(frozen=True)
class VercelDeploy:
    artifact: str = "output"
    target: str = "preview"
    project_id: str | None = None
    team_id: str | None = None
    name: str = "vercel-deploy"

    def compile(self, stage: Stage, workspace_root: Path) -> list[NodeSpec]:
        if stage != Stage.SHIP:
            raise WorkflowError("VercelDeploy can only be used in the ship stage")
        params = asdict(self)
        params.pop("name")
        cfg = _DeployConfig.model_validate(params)
        return [
            NodeSpec(self.name, stage, "vercel/deploy", cfg.model_dump(mode="json"))
        ]


def register_vercel_plugin(registry: PluginRegistry) -> None:
    for name, stage, model, invoke in (
        (
            "build",
            Stage.BUILD,
            _BuildConfig,
            lambda ctx, cfg: Vercel(ctx).build(**cfg.model_dump()),
        ),
        (
            "deploy",
            Stage.SHIP,
            _DeployConfig,
            lambda ctx, cfg: Vercel(ctx).deploy(**cfg.model_dump()),
        ),
    ):
        operation = FacadeOperation(f"vercel/{name}", stage, model, invoke)
        registry.register_operation(
            operation,
            OperationDefinition(
                name=operation.name,
                stages=operation.stages,
                description=f"Vercel prebuilt {name}",
                config_model=model,
            ),
        )
    registry.register_requirement_resolver(
        "github/actions", VercelToolchain, lambda requirement: requirement.github()
    )


__all__ = [
    "Vercel",
    "VercelBuild",
    "VercelDeploy",
    "VercelToolchain",
    "register_vercel_plugin",
]
