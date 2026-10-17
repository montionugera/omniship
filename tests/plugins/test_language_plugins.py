import os
import sys
from pathlib import Path

import pytest
import yaml

from omniship import Pipeline
from omniship.config.models import NodeConfig, OmniShipConfig
from omniship.core.context import ExecutionContext
from omniship.core.execution import (
    Architecture,
    ExecutionHost,
    IdentityToken,
    OperatingSystem,
)
from omniship.core.node import NodeInputs
from omniship.core.stage import Stage
from omniship.plugins.bun import (
    Bun,
    BunBuild,
    BunInstall,
    BunTest,
    BunToolchain,
    register_bun_plugin,
)
from omniship.plugins.github import GitHubActions, GitHubPermission
from omniship.plugins.github.actions import GitHubActionsGenerator
from omniship.plugins.github.operations import register_github_plugin
from omniship.plugins.go import (
    Go,
    GoArch,
    GoBuild,
    GoFmt,
    GoModDownload,
    GoModule,
    GoOS,
    GoTarget,
    GoTest,
    GoToolchain,
    GoVet,
)
from omniship.plugins.node import (
    Node,
    NodeBuild,
    NodeInstall,
    NodeTest,
    NodeToolchain,
    NpmPublish,
    register_node_plugin,
)
from omniship.plugins.python import PyPIPublish, Python
from omniship.plugins.registry import PluginRegistry
from omniship.plugins.rust import (
    CargoBuild,
    CargoPublish,
    CargoTest,
    Rust,
    RustToolchain,
)
from omniship.plugins.zig import Zig, ZigBuild, ZigTest, ZigToolchain
from omniship.runtime import TaskContext, TaskFailure
from omniship.workflow.compiler import compile_pipeline


@pytest.mark.parametrize(
    ("requirement", "dependency", "inputs"),
    [
        (
            NodeToolchain(version="24.8.0"),
            "setup-node",
            {
                "node-version": "24.8.0",
                "package-manager-cache": False,
                "registry-url": "https://registry.npmjs.org",
            },
        ),
        (
            BunToolchain(version="1.3.3"),
            "setup-bun",
            {"bun-version": "1.3.3"},
        ),
        (
            GoToolchain(version_file="go.mod"),
            "setup-go",
            {"go-version-file": "go.mod", "cache": False},
        ),
        (
            RustToolchain(toolchain="1.85.0", components=["clippy"]),
            "setup-rust-toolchain",
            {"toolchain": "1.85.0", "components": "clippy", "cache": False},
        ),
        (
            ZigToolchain(version="0.15.1"),
            "setup-zig",
            {"version": "0.15.1"},
        ),
    ],
)
def test_toolchains_resolve_to_locked_github_actions(
    requirement,
    dependency: str,
    inputs: dict[str, object],
) -> None:
    step = requirement.github()

    assert step.dependency == dependency
    assert step.inputs == inputs


def test_go_toolchain_can_enable_module_cache() -> None:
    step = GoToolchain(
        version_file="go.mod", cache=True, cache_dependency_path="go.sum"
    ).github()

    assert step.inputs == {
        "go-version-file": "go.mod",
        "cache": True,
        "cache-dependency-path": "go.sum",
    }


def test_toolchain_setup_uses_the_locked_action_pin(tmp_path: Path) -> None:
    registry = PluginRegistry()
    register_node_plugin(registry)
    github = GitHubActions()
    config = OmniShipConfig(
        check={
            "verify": NodeConfig(
                uses="core/noop",
                requirements=(NodeToolchain(version="24.8.0"),),
            )
        }
    )

    generated = GitHubActionsGenerator(registry).generate(
        config,
        tmp_path / "workflow.py",
        tmp_path / "omniship.yaml",
        Pipeline(targets=[github]),
    )
    check_file = next(item for item in generated if item.path.name == "check.yml")
    document = yaml.safe_load(check_file.content)

    assert {
        "name": "Set up Node.js",
        "uses": ("actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"),
        "with": {
            "node-version": "24.8.0",
            "package-manager-cache": False,
            "registry-url": "https://registry.npmjs.org",
        },
    } in document["jobs"]["check-verify"]["steps"]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: NodeToolchain(),
        lambda: NodeToolchain(version="24", version_file=".node-version"),
        lambda: NodeToolchain(version_file="C:\\outside\\.node-version"),
        lambda: BunToolchain(),
        lambda: GoToolchain(),
        lambda: RustToolchain(toolchain="stable"),
        lambda: RustToolchain(toolchain="1.85.0", components="clippy"),
        lambda: ZigToolchain(version="latest"),
    ],
)
def test_toolchains_reject_implicit_or_moving_versions(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_language_blocks_compile_to_plugin_operations(tmp_path: Path) -> None:
    pipeline = Pipeline()

    @pipeline.check
    def check(stage):
        stage.task(NodeInstall())
        stage.task(NodeTest())
        stage.task(BunInstall())
        stage.task(BunTest())
        stage.task(GoTest())
        stage.task(ZigTest())
        stage.task(CargoTest())

    @pipeline.build
    def build(stage):
        stage.task(NodeBuild())
        stage.task(BunBuild())
        stage.task(GoBuild(output="dist/demo"))
        stage.task(ZigBuild())
        stage.task(CargoBuild())

    @pipeline.ship
    def ship(stage):
        stage.task(NpmPublish(dry_run=True))
        stage.task(PyPIPublish(dry_run=True))
        stage.task(CargoPublish(dry_run=True))

    config = compile_pipeline(pipeline, tmp_path / "workflow.py")

    assert config.check["node-install"].uses == "node/install"
    assert config.check["bun-test"].uses == "bun/test"
    assert config.check["go-test"].uses == "go/test"
    assert config.check["zig-test"].uses == "zig/test"
    assert config.check["cargo-test"].uses == "rust/cargo-test"
    assert config.build["go-build"].with_["output"] == "dist/demo"
    assert config.ship["npm-publish"].uses == "node/npm-publish"
    assert config.ship["pypi-publish"].uses == "python/pypi-publish"
    assert config.ship["cargo-publish"].uses == "rust/cargo-publish"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("register", "operation_name", "facade", "params"),
    [
        (
            register_node_plugin,
            "node/test",
            Node,
            {"script": "test", "install": True, "clean": True},
        ),
        (
            register_bun_plugin,
            "bun/test",
            Bun,
            {"install": True, "frozen_lockfile": True},
        ),
    ],
)
async def test_test_blocks_install_dependencies_in_the_same_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    register,
    operation_name: str,
    facade,
    params: dict[str, object],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        facade,
        "install",
        lambda self, **kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        facade,
        "test",
        lambda self, **kwargs: calls.append("test"),
    )
    registry = PluginRegistry()
    register(registry)

    result = await registry.get_operation(operation_name).execute(
        ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK),
        NodeInputs(params=params),
    )

    assert result.is_success
    assert calls == ["install", "test"]


@pytest.mark.asyncio
async def test_npm_publish_installs_dependencies_in_the_same_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        Node,
        "install",
        lambda self, **kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        Node,
        "publish",
        lambda self, **kwargs: calls.append("publish"),
    )
    registry = PluginRegistry()
    register_node_plugin(registry)

    result = await registry.get_operation("node/npm-publish").execute(
        ExecutionContext(workspace_root=tmp_path, stage=Stage.SHIP),
        NodeInputs(
            params={
                "install": True,
                "clean": True,
                "dry_run": True,
            }
        ),
    )

    assert result.is_success
    assert calls == ["install", "publish"]


def test_trusted_publishers_declare_identity_requirement(tmp_path: Path) -> None:
    pipeline = Pipeline()

    @pipeline.ship
    def ship(stage):
        stage.task(NpmPublish(trusted_publishing=True))
        stage.task(PyPIPublish(trusted_publishing=True))

    config = compile_pipeline(pipeline, tmp_path / "workflow.py")

    for node in config.ship.values():
        assert node.requirements == (IdentityToken(),)


def test_github_grants_identity_permission_for_trusted_publishing(
    tmp_path: Path,
) -> None:
    registry = PluginRegistry()
    register_github_plugin(registry)
    config = OmniShipConfig(
        ship={
            "publish": NodeConfig(
                uses="core/noop",
                requirements=(IdentityToken(),),
            )
        }
    )

    generated = GitHubActionsGenerator(registry).generate(
        config,
        tmp_path / "workflow.py",
        tmp_path / "omniship.yaml",
        Pipeline(targets=[GitHubActions()]),
    )
    ship_file = next(item for item in generated if item.path.name == "ship.yml")
    document = yaml.safe_load(ship_file.content)

    assert document["jobs"]["ship-publish"]["permissions"] == {
        "contents": GitHubPermission.READ.value,
        "id-token": GitHubPermission.WRITE.value,
    }


def test_go_target_is_build_specific_and_typed() -> None:
    target = GoTarget(GoOS.LINUX, GoArch.ARM64)
    assert target.environment == {"GOOS": "linux", "GOARCH": "arm64"}

    with pytest.raises(TypeError):
        GoTarget("linux", GoArch.ARM64)  # type: ignore[arg-type]


def test_go_build_rejects_output_outside_workspace_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed = False

    def run(self, arguments, *, env=None):
        nonlocal executed
        executed = True

    monkeypatch.setattr(Go, "_run", run)

    with pytest.raises(ValueError, match="inside the workspace"):
        Go(TaskContext(tmp_path, {})).build(output="../outside")

    assert executed is False


def test_go_checks_and_build_options_compile_to_plugin_operations(
    tmp_path: Path,
) -> None:
    pipeline = Pipeline()

    @pipeline.check
    def check(stage):
        stage.task(GoFmt())
        stage.task(GoModDownload())
        stage.task(GoVet(packages=("./cmd/...",)))
        stage.task(GoTest(race=True, timeout="5m", tags=("integration",)))

    @pipeline.build
    def build(stage):
        stage.task(
            GoBuild(
                output="dist/demo",
                target=GoTarget(GoOS.WINDOWS, GoArch.AMD64),
                cgo_enabled=False,
                tags=("release",),
                ldflags="-s -w",
                trimpath=True,
            )
        )

    config = compile_pipeline(pipeline, tmp_path / "workflow.py")
    assert config.check["go-fmt"].uses == "go/fmt"
    assert config.check["go-mod-download"].uses == "go/mod-download"
    assert config.check["go-vet"].with_["packages"] == ["./cmd/..."]
    assert config.check["go-test"].with_["race"] is True
    assert config.build["go-build"].with_ == {
        "output": "dist/demo.exe",
        "package": ".",
        "goos": "windows",
        "goarch": "amd64",
        "cgo_enabled": False,
        "tags": ["release"],
        "ldflags": "-s -w",
        "trimpath": True,
    }


def test_go_facade_constructs_check_and_cross_build_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(self, arguments, *, env=None):
        calls.append((list(arguments), dict(env or {})))
        if arguments[:2] == ["go", "build"]:
            output = tmp_path / arguments[arguments.index("-o") + 1]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"binary")

    monkeypatch.setattr(Go, "_run", run)
    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
    go = Go(TaskContext(tmp_path, {}))
    go.fmt()
    go.mod_download()
    go.vet(packages=("./cmd/...",))
    go.test(race=True, timeout="5m", tags=("integration",))
    go.build(
        output="dist/demo",
        target=GoTarget(GoOS.WINDOWS, GoArch.AMD64),
        cgo_enabled=False,
        tags=("release",),
        ldflags="-s -w",
        trimpath=True,
    )

    assert calls == [
        (["gofmt", "-l", "main.go"], {}),
        (["go", "mod", "download"], {}),
        (["go", "vet", "./cmd/..."], {}),
        (
            ["go", "test", "-race", "-timeout", "5m", "-tags", "integration", "./..."],
            {},
        ),
        (
            [
                "go",
                "build",
                "-trimpath",
                "-tags",
                "release",
                "-ldflags",
                "-s -w",
                "-o",
                "dist/demo.exe",
                ".",
            ],
            {"GOOS": "windows", "GOARCH": "amd64", "CGO_ENABLED": "0"},
        ),
    ]


def test_go_build_uses_target_file_extension(tmp_path: Path) -> None:
    windows = GoBuild(output="dist/tool", target=GoTarget(GoOS.WINDOWS, GoArch.AMD64))
    wasm = GoBuild(output="dist/tool", target=GoTarget(GoOS.WASIP1, GoArch.WASM))

    assert windows.compile(Stage.BUILD, tmp_path)[0].params["output"] == "dist/tool.exe"
    assert wasm.compile(Stage.BUILD, tmp_path)[0].params["output"] == "dist/tool.wasm"


def test_go_build_expands_target_in_artifact_name(tmp_path: Path) -> None:
    block = GoBuild(
        output="dist/tool-{os}-{arch}",
        target=GoTarget(GoOS.WINDOWS, GoArch.ARM64),
    )

    assert block.compile(Stage.BUILD, tmp_path)[0].params["output"] == (
        "dist/tool-windows-arm64.exe"
    )


def test_go_native_windows_build_uses_exe_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def run(self, arguments, *, env=None):
        commands.append(list(arguments))
        output = tmp_path / arguments[arguments.index("-o") + 1]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"binary")

    monkeypatch.setattr(Go, "_run", run)
    context = TaskContext(
        tmp_path,
        {},
        host=ExecutionHost(OperatingSystem.WINDOWS, Architecture.X86_64),
    )
    Go(context).build(output="dist/tool")

    assert commands[0][commands[0].index("-o") + 1] == "dist/tool.exe"


def test_go_module_constructs_submodule_release_tag() -> None:
    module = GoModule("bindings/go")

    assert module.tag("1.2.0") == "bindings/go/v1.2.0"
    with pytest.raises(ValueError, match="version"):
        module.tag("latest")


def test_imperative_facades_build_expected_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(self, arguments, *, env=None):
        calls.append((list(arguments), dict(env or {})))
        if list(arguments[:2]) == ["go", "build"]:
            output = tmp_path / arguments[arguments.index("-o") + 1]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"binary")

    for facade in (Node, Bun, Go, Zig, Rust, Python):
        monkeypatch.setattr(facade, "_run", run, raising=False)

    context = TaskContext(tmp_path, {})
    Node(context).install()
    Bun(context).test()
    Go(context).build(
        output="dist/demo",
        target=GoTarget(GoOS.LINUX, GoArch.ARM64),
    )
    Zig(context).build(optimize="ReleaseSafe")
    Rust(context).build(release=True)

    assert calls == [
        (["npm", "ci"], {}),
        (["bun", "test"], {}),
        (
            ["go", "build", "-o", "dist/demo", "."],
            {"GOOS": "linux", "GOARCH": "arm64"},
        ),
        (["zig", "build", "-Doptimize=ReleaseSafe"], {}),
        (["cargo", "build", "--release"], {}),
    ]


@pytest.mark.parametrize(
    ("publish", "message"),
    [
        (lambda context: Node(context).publish(), "NODE_AUTH_TOKEN"),
        (lambda context: Python(context).publish(), "UV_PUBLISH_TOKEN"),
        (lambda context: Rust(context).publish(), "CARGO_REGISTRY_TOKEN"),
    ],
)
def test_publishers_require_runtime_credentials(
    tmp_path: Path,
    publish,
    message: str,
) -> None:
    with pytest.raises(TaskFailure, match=message):
        publish(TaskContext(tmp_path, {}))


def test_publishers_never_put_tokens_in_command_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(self, arguments, *, env=None):
        commands.append(list(arguments))

    monkeypatch.setattr(Node, "_run", run, raising=False)
    monkeypatch.setattr(Python, "_run", run, raising=False)
    monkeypatch.setattr(Rust, "_run", run, raising=False)

    Node(TaskContext(tmp_path, {"NODE_AUTH_TOKEN": "npm-secret"})).publish()
    Python(TaskContext(tmp_path, {"UV_PUBLISH_TOKEN": "pypi-secret"})).publish(
        files=["demo.whl"]
    )
    Rust(TaskContext(tmp_path, {"CARGO_REGISTRY_TOKEN": "cargo-secret"})).publish()

    flattened = " ".join(argument for command in commands for argument in command)
    assert "npm-secret" not in flattened
    assert "pypi-secret" not in flattened
    assert "cargo-secret" not in flattened


def test_trusted_publishers_do_not_require_registry_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(self, arguments, *, env=None):
        commands.append(list(arguments))

    monkeypatch.setattr(Node, "_run", run, raising=False)
    monkeypatch.setattr(Python, "_run", run, raising=False)

    Node(TaskContext(tmp_path, {})).publish(trusted_publishing=True)
    Python(TaskContext(tmp_path, {})).publish(
        files=["demo.whl"], trusted_publishing=True
    )

    assert commands == [
        ["npm", "publish", "--provenance"],
        ["uv", "publish", "demo.whl", "--trusted-publishing", "always"],
    ]


def test_tool_facade_tolerates_non_utf8_tool_output(tmp_path: Path) -> None:
    from omniship.plugins.tooling import ToolFacade

    context = TaskContext(tmp_path, dict(os.environ))
    script = "import sys; sys.stdout.buffer.write(b'caf\\xe9 ok\\n')"

    output = ToolFacade(context)._run([sys.executable, "-c", script])

    assert output == "caf� ok\n"


def test_tool_facade_resolves_executables_from_task_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from omniship.plugins.tooling import ToolFacade

    resolved: list[tuple[str, str | None]] = []

    def which(name: str, path: str | None = None) -> str:
        resolved.append((name, path))
        return sys.executable

    monkeypatch.setattr("omniship.plugins.tooling.shutil.which", which)
    context = TaskContext(tmp_path, {**os.environ, "PATH": "task-path"})

    ToolFacade(context)._run(["npm", "-c", "pass"])

    assert resolved == [("npm", "task-path")]
