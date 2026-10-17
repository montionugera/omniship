import json
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
from omniship.plugins.cloudflare import (
    Cloudflare,
    CloudflarePagesDeploy,
    CloudflareToolchain,
    CloudflareWorkerDeploy,
    register_cloudflare_plugin,
)
from omniship.plugins.discovery import load_plugins
from omniship.plugins.github.actions import GitHubActionsGenerator
from omniship.plugins.registry import PluginRegistry
from omniship.runtime import TaskContext, TaskFailure
from omniship.workflow.compiler import compile_pipeline
from omniship.workflow.errors import WorkflowError

ENV = {
    "CLOUDFLARE_API_TOKEN": "test-secret-token",
    "CLOUDFLARE_ACCOUNT_ID": "0123456789abcdef0123456789abcdef",
}
PAGES_URL = "https://1a2b3c4d.demo.pages.dev"
WORKER_URL = "https://api.example.workers.dev"


def site(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.html").write_text("hello")
    return path


def worker(path: Path, config: str = "wrangler.json") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.js").write_text("export default {}")
    (path / config).parent.mkdir(parents=True, exist_ok=True)
    (path / config).write_text('{"name":"api","main":"index.js"}')
    return path


def record(calls, entries):
    def command(self, args, *, env, cwd):
        calls.append((args, env, cwd))
        return entries

    return command


def test_pages_deploys_imported_artifact_from_isolated_copy(tmp_path, monkeypatch):
    calls = []

    def command(self, args, *, env, cwd):
        calls.append((args, env, cwd))
        staged = Path(args[2])
        assert (staged / "index.html").read_text() == "hello"
        assert not staged.is_relative_to(tmp_path)
        assert Path(cwd) == staged.parent
        return [{"type": "pages-deploy-detailed", "url": PAGES_URL}]

    monkeypatch.setattr(Cloudflare, "_command", command)
    build = TaskContext(tmp_path, ENV)
    artifact = build.artifacts.add(site(tmp_path / "dist"))
    (tmp_path / "functions").mkdir()
    bundle = tmp_path / "bundle"
    export_artifacts(ArtifactSet([artifact]), bundle)
    ctx = TaskContext(tmp_path, ENV, import_artifacts(bundle))

    url = Cloudflare(ctx).deploy_pages(project="demo", branch="main")

    assert url == PAGES_URL
    assert ctx.outputs.get("deployment_url") == PAGES_URL
    args, env, _ = calls[0]
    assert args[:2] == ["pages", "deploy"]
    assert args[3:] == ["--project-name", "demo", "--branch", "main"]
    assert env["CLOUDFLARE_ACCOUNT_ID"] == ENV["CLOUDFLARE_ACCOUNT_ID"]
    assert all(ENV["CLOUDFLARE_API_TOKEN"] not in arg for arg in args)


def test_worker_production_deploys_prebuilt_config_and_selects_workers_dev(
    tmp_path, monkeypatch
):
    calls = []
    entries = [{"type": "deploy", "targets": ["example.com/*", WORKER_URL]}]
    monkeypatch.setattr(Cloudflare, "_command", record(calls, entries))
    ctx = TaskContext(tmp_path, ENV)
    ctx.artifacts.add(worker(tmp_path / "dist", "api/wrangler.json"))

    url = Cloudflare(ctx).deploy_worker(
        target="production",
        config="api/wrangler.json",
        environment="staging",
        worker="api-staging",
    )

    assert url == WORKER_URL
    assert ctx.outputs.get("deployment_url") == WORKER_URL
    args, _, cwd = calls[0]
    config = Path(args[args.index("--config") + 1])
    assert args[0] == "deploy"
    assert config.name == "wrangler.json" and config.is_relative_to(cwd)
    assert "--no-bundle" in args
    assert args[args.index("--env") + 1] == "staging"
    assert args[args.index("--name") + 1] == "api-staging"


def test_worker_preview_uploads_version_and_prefers_alias_url(tmp_path, monkeypatch):
    calls = []
    entries = [
        {
            "type": "version-upload",
            "preview_url": "https://abc123-api.example.workers.dev",
            "preview_alias_url": "https://pr-12-api.example.workers.dev",
        }
    ]
    monkeypatch.setattr(Cloudflare, "_command", record(calls, entries))
    ctx = TaskContext(tmp_path, ENV)
    ctx.artifacts.add(worker(tmp_path / "dist"))

    url = Cloudflare(ctx).deploy_worker(preview_alias="pr-12")

    assert url == "https://pr-12-api.example.workers.dev"
    args = calls[0][0]
    assert args[:2] == ["versions", "upload"]
    assert args[args.index("--preview-alias") + 1] == "pr-12"
    assert "--env" not in args and "--name" not in args


def test_worker_without_url_target_sets_no_output(tmp_path, monkeypatch):
    entries = [{"type": "deploy", "targets": ["example.com/*", "schedule: 0 3 * * *"]}]
    monkeypatch.setattr(Cloudflare, "_command", record([], entries))
    ctx = TaskContext(tmp_path, ENV)
    ctx.artifacts.add(worker(tmp_path / "dist"))

    assert Cloudflare(ctx).deploy_worker(target="production") is None
    assert ctx.outputs.get("deployment_url") is None


def test_invalid_reported_url_fails(tmp_path, monkeypatch):
    entries = [{"type": "pages-deploy-detailed", "url": "http://insecure.pages.dev"}]
    monkeypatch.setattr(Cloudflare, "_command", record([], entries))
    ctx = TaskContext(tmp_path, ENV)
    ctx.artifacts.add(site(tmp_path / "dist"))

    with pytest.raises(TaskFailure, match="valid deployment URL"):
        Cloudflare(ctx).deploy_pages(project="demo", branch="main")


@pytest.mark.parametrize("missing", list(ENV))
def test_missing_credentials_fail_before_cli(tmp_path, monkeypatch, missing):
    env = {key: value for key, value in ENV.items() if key != missing}
    monkeypatch.setattr(
        Cloudflare, "_command", lambda *a, **kw: pytest.fail("CLI invoked")
    )
    ctx = TaskContext(tmp_path, env)
    ctx.artifacts.add(site(tmp_path / "dist"))

    with pytest.raises(TaskFailure, match=missing):
        Cloudflare(ctx).deploy_pages(project="demo", branch="main")


def test_account_id_parameter_overrides_environment(tmp_path, monkeypatch):
    calls = []
    entries = [{"type": "pages-deploy-detailed", "url": PAGES_URL}]
    monkeypatch.setattr(Cloudflare, "_command", record(calls, entries))
    ctx = TaskContext(tmp_path, {"CLOUDFLARE_API_TOKEN": "secret"})
    ctx.artifacts.add(site(tmp_path / "dist"))

    Cloudflare(ctx).deploy_pages(project="demo", branch="main", account_id="abc123")

    assert calls[0][1]["CLOUDFLARE_ACCOUNT_ID"] == "abc123"


def test_artifact_must_be_one_directory_without_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Cloudflare, "_command", lambda *a, **kw: pytest.fail("CLI invoked")
    )
    ctx = TaskContext(tmp_path, ENV)
    with pytest.raises(TaskFailure, match="exactly one"):
        Cloudflare(ctx).deploy_pages(project="demo", branch="main")
    (tmp_path / "dist").write_text("not a directory")
    ctx.artifacts.add("dist")
    with pytest.raises(TaskFailure, match="directory"):
        Cloudflare(ctx).deploy_pages(project="demo", branch="main")
    (tmp_path / "dist").unlink()
    site(tmp_path / "dist")
    (tmp_path / "dist/link").symlink_to(tmp_path / "dist/index.html")
    ctx = TaskContext(tmp_path, ENV)
    ctx.artifacts.add("dist")
    with pytest.raises(TaskFailure, match="symlink"):
        Cloudflare(ctx).deploy_pages(project="demo", branch="main")


def test_worker_config_must_exist_inside_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Cloudflare, "_command", lambda *a, **kw: pytest.fail("CLI invoked")
    )
    ctx = TaskContext(tmp_path, ENV)
    ctx.artifacts.add(site(tmp_path / "dist"))

    with pytest.raises(TaskFailure, match="wrangler.json"):
        Cloudflare(ctx).deploy_worker()


@pytest.mark.parametrize(
    "block",
    [
        lambda: CloudflarePagesDeploy(project="--help", branch="main"),
        lambda: CloudflarePagesDeploy(project="demo", branch="-x"),
        lambda: CloudflareWorkerDeploy(config="../wrangler.json"),
        lambda: CloudflareWorkerDeploy(config="wrangler.yaml"),
        lambda: CloudflareWorkerDeploy(environment="--dry-run"),
        lambda: CloudflareWorkerDeploy(target="staging"),
        lambda: CloudflareWorkerDeploy(target="production", preview_alias="pr-1"),
    ],
)
def test_blocks_reject_unsafe_or_invalid_values(tmp_path, block):
    with pytest.raises(ValueError):
        block().compile(Stage.SHIP, tmp_path)


@pytest.mark.asyncio
async def test_operations_delegate_to_facade_and_discover(tmp_path, monkeypatch):
    registry = PluginRegistry()
    register_cloudflare_plugin(registry)

    def deploy(self, **kw):
        self.context.outputs.set("deployment_url", PAGES_URL)

    monkeypatch.setattr(Cloudflare, "deploy_pages", deploy)
    monkeypatch.setattr(Cloudflare, "deploy_worker", deploy)
    for name, params in [
        ("pages-deploy", {"project": "demo", "branch": "main"}),
        ("worker-deploy", {}),
    ]:
        result = await registry.get_operation(f"cloudflare/{name}").execute(
            ExecutionContext(tmp_path, Stage.SHIP), NodeInputs(params=params)
        )
        assert result.status == NodeStatus.SUCCESS, result.error_message
        assert result.outputs["deployment_url"] == PAGES_URL
        assert load_plugins().get_operation(f"cloudflare/{name}")


@pytest.mark.parametrize("fail", [False, True])
def test_cli_redacts_token_and_reads_output_file(tmp_path, monkeypatch, fail):
    from omniship.plugins import cloudflare

    real_popen = subprocess.Popen
    calls = []

    def popen(args, **kwargs):
        calls.append(args)
        script = (
            "import json,os,sys; "
            "open(os.environ['WRANGLER_OUTPUT_FILE_PATH'],'a').write("
            "json.dumps({'type':'pages-deploy-detailed','url':'https://x.pages.dev'})"
            "+'\\n'); "
            "print('diagnostic ' + os.environ['CLOUDFLARE_API_TOKEN'],file=sys.stderr); "
            f"sys.exit({int(fail)})"
        )
        return real_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(cloudflare.shutil, "which", lambda *a, **kw: sys.executable)
    monkeypatch.setattr(cloudflare.subprocess, "Popen", popen)
    ctx = TaskContext(tmp_path, ENV)
    facade = Cloudflare(ctx)
    if fail:
        with pytest.raises(TaskFailure, match=r"diagnostic \[REDACTED\]") as error:
            facade._command(["pages", "deploy"], env=ENV, cwd=tmp_path)
        assert ENV["CLOUDFLARE_API_TOKEN"] not in str(error.value)
    else:
        entries = facade._command(["pages", "deploy"], env=ENV, cwd=tmp_path)
        assert entries == [
            {"type": "pages-deploy-detailed", "url": "https://x.pages.dev"}
        ]
    assert ENV["CLOUDFLARE_API_TOKEN"] not in "\n".join(ctx.log.lines)
    assert ENV["CLOUDFLARE_API_TOKEN"] not in json.dumps(calls)


def test_typed_blocks_toolchain_and_example_generate(tmp_path):
    with pytest.raises(ValueError, match="exact"):
        CloudflareToolchain(version="latest")
    assert "wrangler@4.145.0" in CloudflareToolchain(version="4.145.0").github()["run"]
    pages = CloudflarePagesDeploy(project="demo", branch="main")
    assert pages.compile(Stage.SHIP, tmp_path)[0].uses == "cloudflare/pages-deploy"
    assert (
        CloudflareWorkerDeploy().compile(Stage.SHIP, tmp_path)[0].uses
        == "cloudflare/worker-deploy"
    )
    for block in (pages, CloudflareWorkerDeploy()):
        with pytest.raises(WorkflowError, match="ship stage"):
            block.compile(Stage.BUILD, tmp_path)
    source = Path("examples/cloudflare-deploy/workflow.py").resolve()
    pipeline = runpy.run_path(str(source))["pipeline"]
    config = compile_pipeline(pipeline, source)
    generated = GitHubActionsGenerator(load_plugins()).generate(
        config, source, source.parent / "omniship.yaml", pipeline
    )
    ship = next(item.content for item in generated if item.path.name == "ship.yml")
    assert "wrangler@4.145.0" in ship
    assert "${{ secrets.CLOUDFLARE_API_TOKEN }}" in ship
    assert "deployment_url" in ship
