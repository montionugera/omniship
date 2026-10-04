import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from omniship.core.artifact import ArtifactSet
from omniship.core.artifact_bundle import export_artifacts, import_artifacts
from omniship.core.context import ExecutionContext
from omniship.core.node import NodeInputs
from omniship.core.result import NodeStatus
from omniship.core.stage import Stage
from omniship.plugins.discovery import load_plugins
from omniship.plugins.github.actions import GitHubActionsGenerator
from omniship.plugins.registry import PluginRegistry
from omniship.plugins.vercel import (
    Vercel,
    VercelBuild,
    VercelDeploy,
    VercelToolchain,
    register_vercel_plugin,
)
from omniship.runtime import TaskContext, TaskFailure
from omniship.workflow.compiler import compile_pipeline
from omniship.workflow.errors import WorkflowError

ENV = {
    "VERCEL_TOKEN": "test-secret-token",
    "VERCEL_ORG_ID": "team_example",
    "VERCEL_PROJECT_ID": "prj_example",
}


def output(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text('{"version":3}')
    (path / "static").mkdir()
    (path / "static/index.html").write_text("hello")


def test_build_ship_handoff_preserves_output_without_project_secrets(
    tmp_path, monkeypatch
):
    calls = []

    def command(self, args, *, env):
        calls.append((args, env))
        if args[0] == "pull":
            cache = tmp_path / ".vercel"
            cache.mkdir()
            (cache / ".env.production.local").write_text("PRIVATE=value")
        elif args[0] == "build":
            output(Path(args[args.index("--output") + 1]))
        else:
            staged = Path(args[args.index("--cwd") + 1])
            assert (staged / ".vercel/output/static/index.html").read_text() == "hello"
            assert not (staged / ".vercel/.env.production.local").exists()
            return "https://demo.vercel.app\n"
        return ""

    monkeypatch.setattr(Vercel, "_command", command)
    artifact = Vercel(TaskContext(tmp_path, ENV)).build(target="production")
    assert artifact.name == "output"
    assert calls[0][0][:4] == ["pull", "--yes", "--environment", "production"]
    assert "--prod" in calls[1][0]
    assert "--standalone" in calls[1][0]
    bundle = tmp_path / "bundle"
    export_artifacts(ArtifactSet([artifact]), bundle)
    imported = import_artifacts(bundle)
    ctx = TaskContext(tmp_path, ENV, imported)
    assert Vercel(ctx).deploy(target="production") == "https://demo.vercel.app"
    assert ctx.outputs.get("deployment_url") == "https://demo.vercel.app"
    assert "--prebuilt" in calls[-1][0]
    assert "--prod" in calls[-1][0]
    assert all(ENV["VERCEL_TOKEN"] not in str(args) for args, _ in calls)
    assert not list(bundle.rglob(".env*"))


@pytest.mark.parametrize("target", ["preview", "staging"])
def test_deploy_external_prebuilt_artifact_and_custom_target(
    tmp_path, monkeypatch, target
):
    output(tmp_path / ".vercel/output")
    ctx = TaskContext(tmp_path, ENV)
    ctx.artifacts.add(".vercel/output")
    calls = []
    monkeypatch.setattr(
        Vercel,
        "_command",
        lambda self, args, **kw: calls.append(args) or "https://demo.vercel.app",
    )
    Vercel(ctx).deploy(target=target)
    assert calls[0][0] == "deploy"
    assert "--prod" not in calls[0]
    assert ("--target" in calls[0]) == (target == "staging")


@pytest.mark.parametrize("missing", list(ENV))
def test_missing_credentials_fail_before_cli(tmp_path, monkeypatch, missing):
    env = {key: value for key, value in ENV.items() if key != missing}
    monkeypatch.setattr(Vercel, "_command", lambda *a, **kw: pytest.fail("CLI invoked"))
    with pytest.raises(TaskFailure, match=missing):
        Vercel(TaskContext(tmp_path, env)).build()


def test_stale_output_does_not_make_failed_build_succeed(tmp_path, monkeypatch):
    output(tmp_path / ".vercel/output")
    monkeypatch.setattr(Vercel, "_command", lambda *a, **kw: "")
    with pytest.raises(TaskFailure, match="config.json"):
        Vercel(TaskContext(tmp_path, ENV)).build()


def test_missing_duplicate_invalid_and_mismatched_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(Vercel, "_command", lambda *a, **kw: pytest.fail("CLI invoked"))
    ctx = TaskContext(tmp_path, ENV)
    with pytest.raises(TaskFailure, match="exactly one"):
        Vercel(ctx).deploy()
    output(tmp_path / "output")
    ctx.artifacts.add("output", metadata={"vercel.target": "production"})
    with pytest.raises(TaskFailure, match="target"):
        Vercel(ctx).deploy()
    ctx.artifacts.add("output")
    with pytest.raises(TaskFailure, match="exactly one"):
        Vercel(ctx).deploy()
    (tmp_path / "output/config.json").write_text('{"version":2}')
    ctx = TaskContext(tmp_path, ENV)
    ctx.artifacts.add("output")
    with pytest.raises(TaskFailure, match="version 3"):
        Vercel(ctx).deploy()


def test_symlinks_and_path_escape_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(Vercel, "_command", lambda *a, **kw: pytest.fail("CLI invoked"))
    with pytest.raises(ValueError, match="workspace"):
        VercelBuild(directory="../outside").compile(Stage.BUILD, tmp_path)
    output(tmp_path / "output")
    (tmp_path / "output/link").symlink_to(tmp_path / "output/static/index.html")
    ctx = TaskContext(tmp_path, ENV)
    ctx.artifacts.add("output")
    with pytest.raises(TaskFailure, match="symlink"):
        Vercel(ctx).deploy()


def test_project_cache_cannot_escape_workspace(tmp_path, monkeypatch):
    (tmp_path / ".vercel").symlink_to(tmp_path.parent, target_is_directory=True)
    monkeypatch.setattr(Vercel, "_command", lambda *a, **kw: pytest.fail("CLI invoked"))
    with pytest.raises(TaskFailure, match="workspace"):
        Vercel(TaskContext(tmp_path, ENV)).build()


@pytest.mark.asyncio
async def test_operations_delegate_to_facade_and_discover(tmp_path, monkeypatch):
    registry = PluginRegistry()
    register_vercel_plugin(registry)
    monkeypatch.setattr(Vercel, "build", lambda self, **kw: None)
    monkeypatch.setattr(
        Vercel,
        "deploy",
        lambda self, **kw: self.context.outputs.set(
            "deployment_url", "https://demo.vercel.app"
        ),
    )
    for name, stage in [("build", Stage.BUILD), ("deploy", Stage.SHIP)]:
        result = await registry.get_operation(f"vercel/{name}").execute(
            ExecutionContext(tmp_path, stage), NodeInputs(params={})
        )
        assert result.status == NodeStatus.SUCCESS
        if stage == Stage.SHIP:
            assert result.outputs["deployment_url"] == "https://demo.vercel.app"
        assert load_plugins().get_operation(f"vercel/{name}")


@pytest.mark.parametrize("fail", [False, True])
def test_cli_streams_redact_token_and_keep_stdout_separate(tmp_path, monkeypatch, fail):
    from omniship.plugins import vercel

    real_popen = subprocess.Popen
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs["env"]))
        script = (
            "import os,sys; print('https://demo.vercel.app'); "
            "print('diagnostic ' + os.environ['VERCEL_TOKEN'],file=sys.stderr); "
            f"sys.exit({int(fail)})"
        )
        return real_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(vercel.shutil, "which", lambda *a, **kw: sys.executable)
    monkeypatch.setattr(vercel.subprocess, "Popen", popen)
    ctx = TaskContext(tmp_path, ENV)
    if fail:
        with pytest.raises(TaskFailure, match=r"diagnostic \[REDACTED\]") as error:
            Vercel(ctx)._command(["deploy", "--prebuilt"], env=ENV)
        assert ENV["VERCEL_TOKEN"] not in str(error.value)
    else:
        assert (
            Vercel(ctx)._command(["deploy", "--prebuilt"], env=ENV).strip()
            == "https://demo.vercel.app"
        )
    assert ENV["VERCEL_TOKEN"] not in "\n".join(ctx.log.lines)
    assert ENV["VERCEL_TOKEN"] not in str(calls[0][0])


def test_project_overrides_and_target_validation(tmp_path, monkeypatch):
    calls = []

    def command(self, args, *, env):
        calls.append((args, env))
        output(Path(args[args.index("--output") + 1]))
        return ""

    monkeypatch.setattr(Vercel, "_command", command)
    artifact = Vercel(TaskContext(tmp_path, {"VERCEL_TOKEN": "secret"})).build(
        project_id="prj_override", team_id="team_override", target="staging", pull=False
    )
    assert len(calls) == 1
    assert calls[0][1]["VERCEL_PROJECT_ID"] == "prj_override"
    assert "--target" in calls[0][0]
    assert artifact.metadata["vercel.target"] == "staging"
    with pytest.raises(ValueError):
        VercelBuild(target="--help").compile(Stage.BUILD, tmp_path)


def test_typed_blocks_toolchain_and_example_generate(tmp_path):
    with pytest.raises(ValueError, match="exact"):
        VercelToolchain(version="latest")
    assert "vercel@62.2.0" in VercelToolchain(version="62.2.0").github()["run"]
    assert VercelBuild().compile(Stage.BUILD, tmp_path)[0].uses == "vercel/build"
    assert VercelDeploy().compile(Stage.SHIP, tmp_path)[0].uses == "vercel/deploy"
    for block, stage in [(VercelBuild(), Stage.SHIP), (VercelDeploy(), Stage.BUILD)]:
        with pytest.raises(WorkflowError, match="stage"):
            block.compile(stage, tmp_path)
    source = Path("examples/vercel-deploy/workflow.py").resolve()
    pipeline = runpy.run_path(str(source))["pipeline"]
    config = compile_pipeline(pipeline, source)
    generated = GitHubActionsGenerator(load_plugins()).generate(
        config, source, source.parent / "omniship.yaml", pipeline
    )
    build = next(item.content for item in generated if item.path.name == "build.yml")
    ship = next(item.content for item in generated if item.path.name == "ship.yml")
    assert "vercel@62.2.0" in build
    assert "${{ secrets.VERCEL_TOKEN }}" in build
    assert "${{ secrets.VERCEL_TOKEN }}" in ship
    assert "deployment_url" in ship
