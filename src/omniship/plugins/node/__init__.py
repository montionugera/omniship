"""Official Node.js workflow primitives and npm publishing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omniship.core.execution import IdentityToken
from omniship.core.stage import Stage
from omniship.plugins.github import GitHubActionStep
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


@dataclass(frozen=True, slots=True)
class NodeToolchain:
    version: str | None = None
    version_file: str | None = None
    name: str = "node/toolchain"

    def __post_init__(self) -> None:
        if (self.version is None) == (self.version_file is None):
            raise ValueError("NodeToolchain requires exactly one version source")
        if self.version is not None:
            exact_version(self.version, tool="Node.js")
        if self.version_file is not None:
            relative_file(self.version_file, field_name="Node.js version_file")

    def github(self) -> GitHubActionStep:
        inputs: dict[str, object] = {"package-manager-cache": False}
        if self.version is not None:
            inputs["node-version"] = self.version
        else:
            inputs["node-version-file"] = self.version_file
        # npm ignores NODE_AUTH_TOKEN unless an .npmrc references it;
        # setup-node writes that .npmrc only when registry-url is set.
        inputs["registry-url"] = "https://registry.npmjs.org"
        return GitHubActionStep("setup-node", "Set up Node.js", inputs)


class Node(ToolFacade):
    """Imperative Node.js and npm capabilities."""

    def install(self, *, clean: bool = True) -> None:
        self._run(["npm", "ci" if clean else "install"])

    def test(self, *, script: str = "test") -> None:
        self._run(["npm", "run", script])

    def build(
        self,
        *,
        script: str = "build",
        artifacts: Iterable[str] = (),
    ) -> None:
        self._run(["npm", "run", script])
        self._add_artifacts(tuple(artifacts))

    def publish(
        self,
        *,
        files: tuple[str, ...] = (),
        from_artifacts: bool = False,
        access: Literal["public", "restricted"] | None = None,
        tag: str | None = None,
        provenance: bool = False,
        trusted_publishing: bool = False,
        dry_run: bool = False,
    ) -> None:
        if not trusted_publishing:
            self._require_env("NODE_AUTH_TOKEN", dry_run=dry_run)
        if from_artifacts and files:
            raise TaskFailure("Choose files or from_artifacts, not both")
        if from_artifacts:
            selected = sorted(
                (
                    item
                    for item in self.context.artifacts
                    if item.metadata.get("npm.package") == "true"
                ),
                key=lambda item: int(item.metadata.get("npm.order", "0")),
            )
            if not selected:
                raise TaskFailure("No npm package artifacts were provided")
            files = tuple(str(item.path) for item in selected)
        packages = []
        for value in files:
            artifact = self.context.artifacts.get(value)
            path = artifact.path if artifact else self.context.workspace / value
            path = path.resolve()
            if (
                not path.is_relative_to(self.context.workspace)
                or not path.is_file()
                or path.suffix != ".tgz"
            ):
                raise TaskFailure(f"Invalid npm tarball: {value}")
            packages.append(path)
        arguments = []
        if access is not None:
            arguments.extend(["--access", access])
        if tag is not None:
            arguments.extend(["--tag", tag])
        if provenance or trusted_publishing:
            arguments.append("--provenance")
        if dry_run:
            arguments.append("--dry-run")
        if packages:
            for path in packages:
                self._run(["npm", "publish", str(path), "--ignore-scripts", *arguments])
        else:
            self._run(["npm", "publish", *arguments])


@dataclass(frozen=True)
class NodeInstall:
    clean: bool = True
    name: str = "node-install"

    def compile(self, stage: Stage, workspace_root: Path) -> list[NodeSpec]:
        if stage != Stage.CHECK:
            raise WorkflowError("NodeInstall can only be used in the check stage")
        return [NodeSpec(self.name, stage, "node/install", {"clean": self.clean})]


@dataclass(frozen=True)
class NodeTest:
    script: str = "test"
    install: bool = True
    clean: bool = True
    name: str = "node-test"

    def compile(self, stage: Stage, workspace_root: Path) -> list[NodeSpec]:
        if stage != Stage.CHECK:
            raise WorkflowError("NodeTest can only be used in the check stage")
        return [
            NodeSpec(
                self.name,
                stage,
                "node/test",
                {
                    "script": self.script,
                    "install": self.install,
                    "clean": self.clean,
                },
            )
        ]


@dataclass(frozen=True)
class NodeBuild:
    script: str = "build"
    install: bool = True
    clean: bool = True
    artifacts: tuple[str, ...] = ()
    name: str = "node-build"

    def compile(self, stage: Stage, workspace_root: Path) -> list[NodeSpec]:
        if stage != Stage.BUILD:
            raise WorkflowError("NodeBuild can only be used in the build stage")
        return [
            NodeSpec(
                self.name,
                stage,
                "node/build",
                {
                    "script": self.script,
                    "install": self.install,
                    "clean": self.clean,
                    "artifacts": list(self.artifacts),
                },
            )
        ]


@dataclass(frozen=True)
class NpmPublish:
    files: tuple[str, ...] = ()
    from_artifacts: bool = False
    access: Literal["public", "restricted"] | None = None
    tag: str | None = None
    provenance: bool = False
    trusted_publishing: bool = False
    install: bool = True
    clean: bool = True
    dry_run: bool = False
    name: str = "npm-publish"

    def compile(self, stage: Stage, workspace_root: Path) -> list[NodeSpec]:
        if stage != Stage.SHIP:
            raise WorkflowError("NpmPublish can only be used in the ship stage")
        requirements = (IdentityToken(),) if self.trusted_publishing else ()
        return [
            NodeSpec(
                self.name,
                stage,
                "node/npm-publish",
                {
                    "files": list(self.files),
                    "from_artifacts": self.from_artifacts,
                    "access": self.access,
                    "tag": self.tag,
                    "provenance": self.provenance,
                    "trusted_publishing": self.trusted_publishing,
                    "install": self.install,
                    "clean": self.clean,
                    "dry_run": self.dry_run,
                },
                requirements=requirements,
            )
        ]


class _InstallConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clean: bool = True


class _ScriptConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str = Field(min_length=1)
    install: bool = True
    clean: bool = True


class _BuildConfig(_ScriptConfig):
    artifacts: list[str] = Field(default_factory=list)


class _PublishConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: tuple[str, ...] = ()
    from_artifacts: bool = False
    access: Literal["public", "restricted"] | None = None
    tag: str | None = None
    provenance: bool = False
    trusted_publishing: bool = False
    install: bool = True
    clean: bool = True
    dry_run: bool = False


def _operations() -> tuple[FacadeOperation, ...]:
    return (
        FacadeOperation(
            "node/install",
            Stage.CHECK,
            _InstallConfig,
            lambda ctx, cfg: Node(ctx).install(clean=cfg.clean),
        ),
        FacadeOperation(
            "node/test",
            Stage.CHECK,
            _ScriptConfig,
            _test,
        ),
        FacadeOperation(
            "node/build",
            Stage.BUILD,
            _BuildConfig,
            _build,
        ),
        FacadeOperation(
            "node/npm-publish",
            Stage.SHIP,
            _PublishConfig,
            _publish,
        ),
    )


def _test(ctx, cfg) -> None:
    node = Node(ctx)
    if cfg.install:
        node.install(clean=cfg.clean)
    node.test(script=cfg.script)


def _build(ctx, cfg) -> None:
    node = Node(ctx)
    if cfg.install:
        node.install(clean=cfg.clean)
    node.build(script=cfg.script, artifacts=cfg.artifacts)


def _publish(ctx, cfg) -> None:
    node = Node(ctx)
    if cfg.install and not cfg.files and not cfg.from_artifacts:
        node.install(clean=cfg.clean)
    node.publish(
        files=cfg.files,
        from_artifacts=cfg.from_artifacts,
        access=cfg.access,
        tag=cfg.tag,
        provenance=cfg.provenance,
        trusted_publishing=cfg.trusted_publishing,
        dry_run=cfg.dry_run,
    )


def register_node_plugin(registry: PluginRegistry) -> None:
    for operation in _operations():
        registry.register_operation(
            operation,
            OperationDefinition(
                name=operation.name,
                stages=operation.stages,
                description=f"Run {operation.name}",
                config_model=operation.config_model,
            ),
        )
    registry.register_requirement_resolver(
        "github/actions",
        NodeToolchain,
        lambda requirement: requirement.github(),
    )


__all__ = [
    "Node",
    "NodeBuild",
    "NodeInstall",
    "NodeTest",
    "NodeToolchain",
    "NpmPublish",
    "register_node_plugin",
]
