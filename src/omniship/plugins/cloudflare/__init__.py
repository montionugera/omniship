"""Cloudflare Pages and Workers deployment of prebuilt artifacts.

Framework builds stay in Node, Bun, or custom Build tasks. Ship copies one
imported artifact into an isolated directory outside the workspace and
publishes it with Wrangler, so deployment never reads or rebuilds source.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


@dataclass(frozen=True)
class CloudflareToolchain:
    """Install an exact Wrangler version; provision Node.js separately."""

    version: str
    name: str = "cloudflare/toolchain"

    def __post_init__(self) -> None:
        exact_version(self.version, tool="Wrangler")

    def github(self) -> dict[str, str]:
        return {
            "name": "Set up Wrangler",
            "run": f"npm install --global wrangler@{self.version}",
        }


class _DeployConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: str = Field(default="dist", min_length=1)
    account_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]+$")


class _PagesConfig(_DeployConfig):
    project: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    branch: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class _WorkerConfig(_DeployConfig):
    config: str = "wrangler.json"
    environment: str | None = Field(default=None, pattern=_NAME)
    worker: str | None = Field(default=None, pattern=_NAME)
    target: Literal["preview", "production"] = "preview"
    preview_alias: str | None = Field(default=None, pattern=_NAME)

    @field_validator("config")
    @classmethod
    def _config_path(cls, value: str) -> str:
        value = relative_file(value, field_name="config")
        if Path(value).suffix not in {".json", ".jsonc", ".toml"}:
            raise ValueError("config must be a .json, .jsonc, or .toml file")
        return value

    @model_validator(mode="after")
    def _alias_needs_preview(self) -> _WorkerConfig:
        if self.preview_alias is not None and self.target != "preview":
            raise ValueError("preview_alias requires the preview target")
        return self


def _deployment_url(value: object) -> str:
    url = value if isinstance(value, str) else ""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(char.isspace() for char in url)
    ):
        raise TaskFailure("Wrangler did not return a valid deployment URL")
    return url


class Cloudflare(ToolFacade):
    """Imperative Cloudflare deployment; authentication comes from the environment.

    CLOUDFLARE_API_TOKEN is required. The account ID may be declared or
    supplied through CLOUDFLARE_ACCOUNT_ID. No login is performed.
    """

    def _environment(self, cfg: _DeployConfig) -> dict[str, str]:
        if not self.context.env.get("CLOUDFLARE_API_TOKEN"):
            raise TaskFailure("CLOUDFLARE_API_TOKEN is required")
        env = dict(self.context.env)
        if cfg.account_id is not None:
            env["CLOUDFLARE_ACCOUNT_ID"] = cfg.account_id
        if not env.get("CLOUDFLARE_ACCOUNT_ID"):
            raise TaskFailure("CLOUDFLARE_ACCOUNT_ID is required")
        env["CI"] = "1"
        env["WRANGLER_SEND_METRICS"] = "false"
        return env

    def _artifact(self, name: str) -> Artifact:
        matches = [item for item in self.context.artifacts if item.name == name]
        if len(matches) != 1:
            raise TaskFailure(f"Expected exactly one Cloudflare artifact named {name}")
        path = matches[0].path
        if not path.resolve().is_relative_to(self.context.workspace):
            raise TaskFailure("Cloudflare artifact escapes workspace")
        if not path.is_dir():
            raise TaskFailure(f"Cloudflare artifact {name} must be a directory")
        if path.is_symlink() or any(item.is_symlink() for item in path.rglob("*")):
            raise TaskFailure("Cloudflare artifact cannot contain a symlink")
        return matches[0]

    def _command(
        self, args: list[str], *, env: dict[str, str], cwd: Path
    ) -> list[dict[str, object]]:
        """Run Wrangler with the token redacted; return its output-file entries."""
        executable = shutil.which(
            "wrangler", path=env.get("PATH", os.environ.get("PATH"))
        )
        if executable is None:
            raise TaskFailure("Wrangler is missing; install CloudflareToolchain")
        token = env["CLOUDFLARE_API_TOKEN"]
        with TemporaryDirectory(prefix="wrangler-output-") as temporary:
            output_file = Path(temporary) / "output.json"
            process = subprocess.Popen(
                [executable, *args],
                cwd=cwd,
                env={
                    **os.environ,
                    **env,
                    "WRANGLER_OUTPUT_FILE_PATH": str(output_file),
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            lines: list[str] = []
            assert process.stdout is not None
            for line in process.stdout:
                safe = line.replace(token, "[REDACTED]")
                lines.append(safe)
                self.context.log.output(safe.rstrip("\r\n"))
            if process.wait() != 0:
                raise TaskFailure("".join(lines).strip() or "Wrangler command failed")
            if not output_file.is_file():
                return []
            try:
                return [
                    json.loads(line)
                    for line in output_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except ValueError as exc:
                raise TaskFailure("Wrangler wrote an invalid output file") from exc

    def _publish(self, url: object) -> str:
        url = _deployment_url(url)
        self.context.outputs.set("deployment_url", url)
        return url

    def deploy_pages(
        self,
        *,
        project: str,
        branch: str,
        artifact: str = "dist",
        account_id: str | None = None,
    ) -> str:
        """Upload a static-site artifact to a Pages project branch.

        Cloudflare treats the project's production branch as production and
        every other branch as a preview. Expose deployment_url as an output.
        """
        cfg = _PagesConfig(
            project=project, branch=branch, artifact=artifact, account_id=account_id
        )
        env = self._environment(cfg)
        selected = self._artifact(cfg.artifact)
        with TemporaryDirectory(prefix="cloudflare-pages-") as temporary:
            staged = Path(temporary) / "site"
            shutil.copytree(selected.path, staged)
            entries = self._command(
                [
                    "pages",
                    "deploy",
                    str(staged),
                    "--project-name",
                    cfg.project,
                    "--branch",
                    cfg.branch,
                ],
                env=env,
                cwd=Path(temporary),
            )
        detailed = [e for e in entries if e.get("type") == "pages-deploy-detailed"]
        if not detailed:
            raise TaskFailure("Wrangler did not report the Pages deployment")
        return self._publish(detailed[-1].get("url"))

    def deploy_worker(
        self,
        *,
        artifact: str = "dist",
        config: str = "wrangler.json",
        environment: str | None = None,
        worker: str | None = None,
        target: str = "preview",
        preview_alias: str | None = None,
        account_id: str | None = None,
    ) -> str | None:
        """Upload a prebuilt Worker described by a config inside the artifact.

        The preview target uploads a version without serving production
        traffic; production deploys it. Wrangler bundling is disabled. Sets
        deployment_url when Cloudflare reports a URL for the deployment.
        """
        cfg = _WorkerConfig(
            artifact=artifact,
            config=config,
            environment=environment,
            worker=worker,
            target=target,
            preview_alias=preview_alias,
            account_id=account_id,
        )
        env = self._environment(cfg)
        selected = self._artifact(cfg.artifact)
        if not (selected.path / cfg.config).is_file():
            raise TaskFailure(f"Cloudflare artifact is missing {cfg.config}")
        command = ["deploy"] if cfg.target == "production" else ["versions", "upload"]
        with TemporaryDirectory(prefix="cloudflare-worker-") as temporary:
            staged = Path(temporary) / "worker"
            shutil.copytree(selected.path, staged)
            args = [*command, "--config", str(staged / cfg.config), "--no-bundle"]
            if cfg.environment is not None:
                args.extend(["--env", cfg.environment])
            if cfg.worker is not None:
                args.extend(["--name", cfg.worker])
            if cfg.preview_alias is not None:
                args.extend(["--preview-alias", cfg.preview_alias])
            entries = self._command(args, env=env, cwd=Path(temporary))
        if cfg.target == "production":
            urls = [
                target
                for entry in entries
                if entry.get("type") == "deploy"
                for target in entry.get("targets") or []
                if isinstance(target, str) and target.startswith("https://")
            ]
            url = urls[0] if urls else None
        else:
            uploads = [e for e in entries if e.get("type") == "version-upload"]
            last = uploads[-1] if uploads else {}
            url = last.get("preview_alias_url") or last.get("preview_url")
        return None if url is None else self._publish(url)


def _ship_only(block: str, stage: Stage) -> None:
    if stage != Stage.SHIP:
        raise WorkflowError(f"{block} can only be used in the ship stage")


@dataclass(frozen=True)
class CloudflarePagesDeploy:
    project: str
    branch: str
    artifact: str = "dist"
    account_id: str | None = None
    name: str = "cloudflare-pages-deploy"

    def compile(self, stage: Stage, workspace_root: Path) -> list[NodeSpec]:
        params = asdict(self)
        params.pop("name")
        cfg = _PagesConfig.model_validate(params)
        _ship_only("CloudflarePagesDeploy", stage)
        return [
            NodeSpec(
                self.name,
                stage,
                "cloudflare/pages-deploy",
                cfg.model_dump(mode="json"),
            )
        ]


@dataclass(frozen=True)
class CloudflareWorkerDeploy:
    artifact: str = "dist"
    config: str = "wrangler.json"
    environment: str | None = None
    worker: str | None = None
    target: str = "preview"
    preview_alias: str | None = None
    account_id: str | None = None
    name: str = "cloudflare-worker-deploy"

    def compile(self, stage: Stage, workspace_root: Path) -> list[NodeSpec]:
        params = asdict(self)
        params.pop("name")
        cfg = _WorkerConfig.model_validate(params)
        _ship_only("CloudflareWorkerDeploy", stage)
        return [
            NodeSpec(
                self.name,
                stage,
                "cloudflare/worker-deploy",
                cfg.model_dump(mode="json"),
            )
        ]


def register_cloudflare_plugin(registry: PluginRegistry) -> None:
    for name, model, invoke, description in (
        (
            "pages-deploy",
            _PagesConfig,
            lambda ctx, cfg: Cloudflare(ctx).deploy_pages(**cfg.model_dump()),
            "Cloudflare Pages prebuilt deployment",
        ),
        (
            "worker-deploy",
            _WorkerConfig,
            lambda ctx, cfg: Cloudflare(ctx).deploy_worker(**cfg.model_dump()),
            "Cloudflare Workers prebuilt deployment",
        ),
    ):
        operation = FacadeOperation(f"cloudflare/{name}", Stage.SHIP, model, invoke)
        registry.register_operation(
            operation,
            OperationDefinition(
                name=operation.name,
                stages=operation.stages,
                description=description,
                config_model=model,
            ),
        )
    registry.register_requirement_resolver(
        "github/actions",
        CloudflareToolchain,
        lambda requirement: requirement.github(),
    )


__all__ = [
    "Cloudflare",
    "CloudflarePagesDeploy",
    "CloudflareToolchain",
    "CloudflareWorkerDeploy",
    "register_cloudflare_plugin",
]
