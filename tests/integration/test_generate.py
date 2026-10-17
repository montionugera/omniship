from importlib.metadata import version
from pathlib import Path

import yaml
from click.testing import CliRunner

from omniship.cli import runner as cli_runner
from omniship.cli.app import cli
from omniship.core.artifact import Artifact, ArtifactSet
from omniship.core.artifact_bundle import export_artifacts, import_artifacts
from omniship.core.stage import Stage


def test_generate_check_detects_changed_plugin_package_pin(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    definition = (
        "from omniship import Pipeline, PluginPackage\n"
        "from omniship.plugins.github import GitHubActions\n"
        "pipeline = Pipeline(\n"
        "    plugins=[PluginPackage('omniship-acme', '1.2.3')],\n"
        "    targets=[GitHubActions()],\n"
        ")\n"
        "@pipeline.check\n"
        "def check(stage):\n"
        "    @stage.task\n"
        "    def verify(ctx):\n"
        "        pass\n"
    )
    source.write_text(definition, encoding="utf-8")
    runner = CliRunner()
    generated = runner.invoke(cli, ["generate", "-f", str(source), "-o", str(output)])
    assert generated.exit_code == 0, generated.output

    source.write_text(definition.replace("1.2.3", "1.2.4"), encoding="utf-8")
    stale = runner.invoke(
        cli, ["generate", "-f", str(source), "-o", str(output), "--check"]
    )
    assert stale.exit_code != 0
    assert "omniship.lock" in stale.output


def test_external_workflow_local_run_fails_clearly(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import GitHubActions, GitHubExternalWorkflow\n"
        "pipeline = Pipeline(targets=[GitHubActions()])\n"
        "@pipeline.check\n"
        "def check(stage):\n"
        "    stage.task(GitHubExternalWorkflow(\n"
        "        'octacity-org/ci/security.yml', ref='v1', name='security'\n"
        "    ))\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    generated = runner.invoke(cli, ["generate", "-f", str(source), "-o", str(output)])
    assert generated.exit_code == 0, generated.output

    executed = runner.invoke(
        cli,
        ["run-node", "--stage", "check", "--node", "security", "--config", str(output)],
    )
    assert executed.exit_code != 0
    assert "require GitHub Actions" in executed.output


WORKFLOW = """\
from omniship import Pipeline
from omniship.plugins.python import Ruff, Wheel
from omniship.plugins.github import GitHubActions, GitHubRelease, GitHubRunner

github = GitHubActions(default_runner=GitHubRunner.UBUNTU_24_04)
pipeline = Pipeline(targets=[github])

@pipeline.check
def check(stage):
    lint = stage.task(
        Ruff(),
        execution=github.job(runners=[GitHubRunner.UBUNTU_SLIM]),
    )

    @stage.task(
        name="tests",
        after=[lint],
        execution=github.job(
            runners=[
                GitHubRunner.UBUNTU_24_04,
                GitHubRunner.MACOS_15,
                GitHubRunner.WINDOWS_2025,
            ],
        ),
    )
    def tests(ctx):
        pass

@pipeline.build
def build(stage):
    stage.task(
        Wheel(),
        execution=github.job(runners=[GitHubRunner.MACOS_15]),
    )

@pipeline.ship
def ship(stage):
    stage.task(
        GitHubRelease(repository="octacity/demo", tag="v1.0.0"),
    )
"""


def test_run_node_uses_reviewed_source_revision_for_imports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "omniship.yaml"
    imports = tmp_path / "imports"
    imports.mkdir()
    config.write_text(
        "version: 1\ncheck:\n  verify:\n    uses: core/noop\n",
        encoding="utf-8",
    )
    revisions: list[str | None] = []

    def import_bundles(path: Path, *, expected_revision: str | None):
        revisions.append(expected_revision)
        return ArtifactSet()

    monkeypatch.setattr(cli_runner, "import_artifact_bundles", import_bundles)
    monkeypatch.setenv("OMNISHIP_REVISION", "current-revision")
    monkeypatch.setenv(
        "OMNISHIP_EXPECTED_ARTIFACT_REVISION",
        "source-revision",
    )

    result = cli_runner.run_node(
        Stage.CHECK,
        "verify",
        config_path=str(config),
        artifact_import_root=str(imports),
    )

    assert result == 0
    assert revisions == ["source-revision"]


def test_run_node_exports_only_artifacts_it_produced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OMNISHIP_REVISION", raising=False)
    monkeypatch.delenv("OMNISHIP_EXPECTED_ARTIFACT_REVISION", raising=False)
    upstream = tmp_path / "app.whl"
    upstream.write_text("wheel", encoding="utf-8")
    export_artifacts(
        ArtifactSet([Artifact.from_path(upstream)]),
        tmp_path / "imports" / "omniship-build-wheel",
    )
    upstream.unlink()
    config = tmp_path / "omniship.yaml"
    config.write_text(
        "version: 1\n"
        "build:\n"
        "  checksums:\n"
        "    uses: core/command\n"
        "    with:\n"
        "      run: echo sum > SHA256SUMS\n"
        "      artifacts: [SHA256SUMS]\n",
        encoding="utf-8",
    )

    result = cli_runner.run_node(
        Stage.BUILD,
        "checksums",
        config_path=str(config),
        artifact_import_root=str(tmp_path / "imports"),
        artifact_export_path=str(tmp_path / "handoff"),
    )

    assert result == 0
    exported = import_artifacts(tmp_path / "handoff")
    assert [artifact.name for artifact in exported] == ["SHA256SUMS"]


def test_generate_writes_valid_yaml_and_check_detects_drift(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(WORKFLOW, encoding="utf-8")
    runner = CliRunner()

    generated = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 0, generated.output
    assert output.read_text(encoding="utf-8").startswith("# Generated by OmniShip")
    workflow_root = tmp_path / ".github" / "workflows"
    check_output = workflow_root / "check.yml"
    build_output = workflow_root / "build.yml"
    ship_output = workflow_root / "ship.yml"
    assert check_output.is_file()
    assert build_output.is_file()
    assert ship_output.is_file()
    lock_output = tmp_path / "omniship.lock"
    assert lock_output.is_file()
    assert "[github.actions.checkout]" in lock_output.read_text(encoding="utf-8")
    check_actions = yaml.load(
        check_output.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    build_actions = yaml.load(
        build_output.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    ship_actions = yaml.load(
        ship_output.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert check_actions["permissions"] == {"contents": "read"}
    assert build_actions["permissions"] == {"contents": "read"}
    assert ship_actions["permissions"] == {"contents": "read"}
    prepare_job = check_actions["jobs"]["prepare"]
    lint_job = check_actions["jobs"]["check-ruff"]
    test_job = check_actions["jobs"]["check-tests"]
    check_barrier = check_actions["jobs"]["check-complete"]
    build_job = build_actions["jobs"]["build-wheel"]
    build_barrier = build_actions["jobs"]["build-complete"]
    ship_job = ship_actions["jobs"]["ship-github-release"]
    assert lint_job["name"] == "Check · Ruff"
    assert ship_job["name"] == "Ship · GitHub Release"
    assert prepare_job["runs-on"] == "ubuntu-24.04"
    assert lint_job["runs-on"] == "ubuntu-slim"
    assert lint_job["needs"] == "prepare"
    assert test_job["runs-on"] == "${{ matrix.runner }}"
    assert test_job["needs"] == "check-ruff"
    assert test_job["strategy"] == {
        "fail-fast": "false",
        "matrix": {"runner": ["ubuntu-24.04", "macos-15", "windows-2025"]},
    }
    assert check_barrier["needs"] == "check-tests"
    assert build_job["runs-on"] == "macos-15"
    assert build_actions["jobs"]["check"] == {
        "name": "Check",
        "uses": "./.github/workflows/check.yml",
    }
    assert build_job["needs"] == "check"
    assert build_barrier["needs"] == "build-wheel"
    assert ship_job["runs-on"] == "ubuntu-24.04"
    assert ship_job["needs"] == "build"
    assert ship_job["permissions"] == {"contents": "write"}
    assert "env" not in ship_job
    steps = prepare_job["steps"]
    assert steps[0] == {
        "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    }
    assert steps[1] == {
        "uses": "astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4"
    }
    assert {"run": f"uvx --from omniship=={version('omniship')} omniship generate --check"} in steps
    assert {
        "name": "Run Ruff",
        "run": (
            f"uvx --from omniship=={version('omniship')} omniship run-node "
            "--stage check --node ruff --config omniship.yaml"
        ),
    } in lint_job["steps"]
    assert {
        "name": "Run Tests",
        "run": (
            f"uvx --from omniship=={version('omniship')} omniship run-node "
            "--stage check --node tests --config omniship.yaml"
        ),
    } in test_job["steps"]
    assert {
        "name": "Run Wheel",
        "run": (
            f"uvx --from omniship=={version('omniship')} omniship run-node "
            "--stage build --node wheel "
            "--config omniship.yaml --export-artifacts .omniship/handoff/build-wheel"
        ),
        "env": {"OMNISHIP_REVISION": "${{ github.sha }}"},
    } in build_job["steps"]
    assert {
        "uses": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "with": {
            "name": "omniship-build-build-wheel",
            "path": ".omniship/handoff/build-wheel",
            "include-hidden-files": "true",
        },
    } in build_job["steps"]
    assert {
        "uses": "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
        "with": {
            "pattern": "omniship-build-*",
            "path": ".omniship/imports",
        },
    } in ship_job["steps"]
    assert {
        "name": "Run GitHub Release",
        "run": (
            f"uvx --from omniship=={version('omniship')} omniship run-node "
            "--stage ship --node github-release "
            "--config omniship.yaml --import-artifacts-root .omniship/imports"
        ),
        "env": {
            "OMNISHIP_REVISION": "${{ github.sha }}",
            "GITHUB_TOKEN": "${{ github.token }}",
        },
    } in ship_job["steps"]
    current = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output), "--check"],
    )
    assert current.exit_code == 0, current.output

    ship_output.write_text("changed\n", encoding="utf-8")
    drifted_actions = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output), "--check"],
    )
    assert drifted_actions.exit_code == 1
    assert str(ship_output) in drifted_actions.output

    generated = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output), "--force"],
    )
    assert generated.exit_code == 0, generated.output

    output.write_text("changed\n", encoding="utf-8")
    drifted = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output), "--check"],
    )
    assert drifted.exit_code == 1
    assert "out of date" in drifted.output


def test_generate_compiles_job_controls_secrets_and_caches(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import GitHubActions, GitHubShell\n"
        "github = GitHubActions()\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.build\n"
        "def build(stage):\n"
        "    @stage.task(execution=github.job(\n"
        "        timeout_minutes=30,\n"
        "        environment='quality',\n"
        "        working_directory='packages/cli',\n"
        "        shell=GitHubShell.BASH,\n"
        "        env={'MODE': 'strict', 'API_TOKEN': github.secret('API_TOKEN')},\n"
        "        caches=[github.cache(\n"
        "            paths=['~/.cache/demo', 'build'],\n"
        "            key=['demo', github.runner_os, github.hash_files('**/lock')],\n"
        "            restore_prefixes=['demo-${{ runner.os }}-', 'demo-'],\n"
        "        )],\n"
        "    ))\n"
        "    def verify(ctx):\n"
        "        pass\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 0, generated.output
    document = yaml.load(
        (tmp_path / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    job = document["jobs"]["build-verify"]
    assert job["timeout-minutes"] == "30"
    assert job["environment"] == "quality"
    cache_step = job["steps"][2]
    assert cache_step == {
        "name": "Restore cache 1",
        "uses": "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
        "with": {
            "path": "~/.cache/demo\nbuild",
            "key": "demo-${{ runner.os }}-${{ hashFiles('**/lock') }}",
            "restore-keys": "demo-${{ runner.os }}-\ndemo-",
        },
    }
    run_step = job["steps"][3]
    assert "--working-directory packages/cli" in run_step["run"]
    assert "working-directory" not in run_step
    assert run_step["shell"] == "bash"
    assert run_step["env"] == {
        "MODE": "strict",
        "API_TOKEN": "${{ secrets.API_TOKEN }}",
        "OMNISHIP_REVISION": "${{ github.sha }}",
    }


def test_generate_compiles_external_checkout_requirements(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import GitHubActions\n"
        "github = GitHubActions()\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.check\n"
        "def check(stage):\n"
        "    @stage.task(requires=[github.checkout(\n"
        "        repository='ata-sesli/zova',\n"
        "        ref='0123456789abcdef',\n"
        "        path='.deps/zova',\n"
        "        token=github.secret('ZOVA_TOKEN'),\n"
        "    )])\n"
        "    def verify(ctx):\n"
        "        pass\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 0, generated.output
    document = yaml.load(
        (tmp_path / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    checkout = document["jobs"]["check-verify"]["steps"][2]
    assert checkout == {
        "name": "Check out ata-sesli/zova",
        "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "with": {
            "repository": "ata-sesli/zova",
            "ref": "0123456789abcdef",
            "path": ".deps/zova",
            "persist-credentials": "false",
            "fetch-depth": "1",
            "submodules": "false",
            "token": "${{ secrets.ZOVA_TOKEN }}",
        },
    }


def test_generate_compiles_os_specific_system_packages(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import GitHubActions, GitHubRunner\n"
        "from omniship.plugins.system import SystemPackages\n"
        "github = GitHubActions()\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.check\n"
        "def check(stage):\n"
        "    @stage.task(\n"
        "        requires=[SystemPackages(\n"
        "            ubuntu=['zlib1g-dev'], macos=['zlib'], windows=['zlib'],\n"
        "        )],\n"
        "        execution=github.job(runners=[\n"
        "            GitHubRunner.UBUNTU_24_04,\n"
        "            GitHubRunner.MACOS_15,\n"
        "            GitHubRunner.WINDOWS_2025,\n"
        "        ]),\n"
        "    )\n"
        "    def native(ctx):\n"
        "        pass\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 0, generated.output
    document = yaml.load(
        (tmp_path / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    steps = document["jobs"]["check-native"]["steps"]
    assert steps[2:5] == [
        {
            "name": "Install Ubuntu system packages",
            "if": "runner.os == 'Linux'",
            "run": (
                "sudo apt-get update && sudo apt-get install -y "
                "--no-install-recommends zlib1g-dev"
            ),
        },
        {
            "name": "Install macOS system packages",
            "if": "runner.os == 'macOS'",
            "run": "brew install zlib",
        },
        {
            "name": "Install Windows system packages",
            "if": "runner.os == 'Windows'",
            "run": "choco install zlib -y --no-progress",
        },
    ]


def test_generate_protects_hand_authored_yaml(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(WORKFLOW, encoding="utf-8")
    output.write_text("version: 1\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert result.exit_code == 1
    assert "Refusing to overwrite" in result.output
    assert output.read_text(encoding="utf-8") == "version: 1\n"


def test_generated_imperative_pipeline_executes_with_existing_runner(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "pipeline = Pipeline()\n"
        "@pipeline.build\n"
        "def build(stage):\n"
        "    @stage.task\n"
        "    def package(ctx):\n"
        "        artifact = ctx.workspace / 'package.txt'\n"
        "        artifact.write_text('built')\n"
        "        ctx.artifacts.add(artifact)\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    generated = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )
    assert generated.exit_code == 0, generated.output

    executed = runner.invoke(
        cli,
        ["build", "--skip-check", "-c", str(output)],
    )

    assert executed.exit_code == 0, executed.output
    assert (tmp_path / "package.txt").read_text(encoding="utf-8") == "built"


def test_cli_exports_build_artifacts_and_imports_them_for_ship(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    bundle = tmp_path / ".omniship" / "handoff"
    source.write_text(
        "from omniship import Pipeline\n"
        "pipeline = Pipeline()\n"
        "@pipeline.build\n"
        "def build(stage):\n"
        "    @stage.task\n"
        "    def package(ctx):\n"
        "        artifact = ctx.workspace / 'package.txt'\n"
        "        artifact.write_text('built')\n"
        "        ctx.artifacts.add(artifact)\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    @stage.task\n"
        "    def publish(ctx):\n"
        "        artifact = ctx.artifacts.get('package.txt')\n"
        "        if artifact is None:\n"
        "            ctx.fail('package.txt was not handed off')\n"
        "        (ctx.workspace / 'consumed.txt').write_text(artifact.path.read_text())\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    generated = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )
    assert generated.exit_code == 0, generated.output

    built = runner.invoke(
        cli,
        [
            "build",
            "--skip-check",
            "--config",
            str(output),
            "--export-artifacts",
            str(bundle),
        ],
    )
    assert built.exit_code == 0, built.output
    (tmp_path / "package.txt").unlink()

    shipped = runner.invoke(
        cli,
        [
            "ship",
            "--skip-check",
            "--skip-build",
            "--config",
            str(output),
            "--import-artifacts",
            str(bundle),
        ],
    )

    assert shipped.exit_code == 0, shipped.output
    assert (tmp_path / "consumed.txt").read_text(encoding="utf-8") == "built"


def test_explicit_github_target_generates_actions_for_imperative_ship(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import (\n"
        "    GitHub, GitHubActions, GitHubPermission, GitHubPermissions,\n"
        ")\n"
        "github = GitHubActions()\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    @stage.task(execution=github.job(\n"
        "        permissions=GitHubPermissions(contents=GitHubPermission.WRITE),\n"
        "    ))\n"
        "    def release(ctx):\n"
        "        GitHub(ctx).release(\n"
        "            repository='octacity-org/omniship',\n"
        "            tag='v1.0.0',\n"
        "            dry_run=True,\n"
        "        )\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 0, generated.output
    actions_path = tmp_path / ".github" / "workflows" / "ship.yml"
    assert actions_path.is_file()
    actions = yaml.load(
        actions_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    assert actions["permissions"] == {"contents": "read"}
    assert actions["jobs"]["ship-release"]["permissions"] == {"contents": "write"}


def test_generate_rejects_release_job_with_insufficient_permissions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import (\n"
        "    GitHubActions, GitHubPermission, GitHubPermissions, GitHubRelease,\n"
        ")\n"
        "github = GitHubActions()\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    stage.task(\n"
        "        GitHubRelease(repository='octacity-org/omniship', tag='v1.0.0'),\n"
        "        execution=github.job(permissions=GitHubPermissions(\n"
        "            contents=GitHubPermission.READ,\n"
        "        )),\n"
        "    )\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 1
    assert "requires 'contents: write'" in generated.output


def test_dry_run_release_does_not_receive_write_permission(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import GitHubRelease\n"
        "pipeline = Pipeline()\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    stage.task(GitHubRelease(\n"
        "        repository='octacity-org/omniship', tag='v1.0.0', dry_run=True,\n"
        "    ))\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )
    assert generated.exit_code == 0, generated.output
    actions = yaml.load(
        (tmp_path / ".github" / "workflows" / "ship.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert actions["permissions"] == {"contents": "read"}
    assert "permissions" not in actions["jobs"]["ship-github-release"]


def test_ship_task_without_github_permissions_does_not_receive_token(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import GitHubActions\n"
        "github = GitHubActions()\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    @stage.task\n"
        "    def publish(ctx):\n"
        "        pass\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )
    assert generated.exit_code == 0, generated.output
    actions = yaml.load(
        (tmp_path / ".github" / "workflows" / "ship.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    run_step = actions["jobs"]["ship-publish"]["steps"][-1]

    assert run_step["env"] == {"OMNISHIP_REVISION": "${{ github.sha }}"}


def test_run_node_executes_only_the_selected_imperative_task(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "pipeline = Pipeline()\n"
        "@pipeline.check\n"
        "def check(stage):\n"
        "    @stage.task\n"
        "    def first(ctx):\n"
        "        (ctx.workspace / 'first.txt').write_text('first')\n"
        "    @stage.task\n"
        "    def second(ctx):\n"
        "        (ctx.workspace / 'second.txt').write_text('second')\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    generated = runner.invoke(cli, ["generate", "-f", str(source), "-o", str(output)])
    assert generated.exit_code == 0, generated.output

    executed = runner.invoke(
        cli,
        [
            "run-node",
            "--stage",
            "check",
            "--node",
            "first",
            "--config",
            str(output),
        ],
    )

    assert executed.exit_code == 0, executed.output
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first"
    assert not (tmp_path / "second.txt").exists()


def test_run_node_scopes_the_task_workspace_to_a_subdirectory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    package = tmp_path / "packages" / "cli"
    package.mkdir(parents=True)
    source.write_text(
        "from omniship import Pipeline\n"
        "pipeline = Pipeline()\n"
        "@pipeline.check\n"
        "def check(stage):\n"
        "    @stage.task\n"
        "    def locate(ctx):\n"
        "        (ctx.workspace / 'cwd.txt').write_text(str(ctx.workspace))\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    generated = runner.invoke(cli, ["generate", "-f", str(source), "-o", str(output)])
    assert generated.exit_code == 0, generated.output

    executed = runner.invoke(
        cli,
        [
            "run-node",
            "--stage",
            "check",
            "--node",
            "locate",
            "--config",
            str(output),
            "--working-directory",
            "packages/cli",
        ],
    )

    assert executed.exit_code == 0, executed.output
    assert (package / "cwd.txt").read_text(encoding="utf-8") == str(package)


def test_build_dependency_download_does_not_match_prefixed_jobs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import GitHubActions\n"
        "github = GitHubActions()\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.build\n"
        "def build(stage):\n"
        "    @stage.task()\n"
        "    def app(ctx):\n"
        "        pass\n"
        "    @stage.task()\n"
        "    def app_docs(ctx):\n"
        "        pass\n"
        "    @stage.task(after=[app])\n"
        "    def package(ctx):\n"
        "        pass\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )
    assert generated.exit_code == 0, generated.output
    actions = yaml.load(
        (tmp_path / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert "build-app-docs" in actions["jobs"]
    downloads = [
        step["with"]["pattern"]
        for step in actions["jobs"]["build-package"]["steps"]
        if "download-artifact" in step.get("uses", "")
    ]
    assert downloads == ["omniship-build-build-app"]


def test_build_node_dependencies_download_predecessor_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import GitHubActions, GitHubRunner\n"
        "github = GitHubActions()\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.build\n"
        "def build(stage):\n"
        "    @stage.task(execution=github.job(runners=[GitHubRunner.UBUNTU_24_04, GitHubRunner.MACOS_15]))\n"
        "    def compile(ctx):\n"
        "        pass\n"
        "    @stage.task(after=[compile])\n"
        "    def package(ctx):\n"
        "        pass\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )
    assert generated.exit_code == 0, generated.output
    actions = yaml.load(
        (tmp_path / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    compile_job = actions["jobs"]["build-compile"]
    package_job = actions["jobs"]["build-package"]
    assert compile_job["strategy"]["matrix"]["runner"] == [
        "ubuntu-24.04",
        "macos-15",
    ]
    assert compile_job["steps"][-1]["with"]["name"] == (
        "omniship-build-build-compile.${{ matrix.runner }}"
    )
    assert package_job["needs"] == "build-compile"
    assert {
        "uses": "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
        "with": {
            "pattern": "omniship-build-build-compile.*",
            "path": ".omniship/imports/build-compile",
        },
    } in package_job["steps"]
    assert {
        "name": "Run Package",
        "run": (
            f"uvx --from omniship=={version('omniship')} omniship run-node "
            "--stage build --node package "
            "--config omniship.yaml --import-artifacts-root .omniship/imports "
            "--export-artifacts .omniship/handoff/build-package"
        ),
        "env": {"OMNISHIP_REVISION": "${{ github.sha }}"},
    } in package_job["steps"]


def test_generate_writes_three_renameable_stage_workflows(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import (\n"
        "    GitHubActions, GitHubRelease, GitHubWorkflow,\n"
        ")\n"
        "from omniship.plugins.python import Ruff, Wheel\n"
        "github = GitHubActions(\n"
        "    check=GitHubWorkflow(file='ci.yml', name='CI'),\n"
        "    build=GitHubWorkflow(file='package.yml', name='Package'),\n"
        "    ship=GitHubWorkflow(file='publish.yml', name='Publish'),\n"
        ")\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.check\n"
        "def check(stage):\n"
        "    stage.task(Ruff())\n"
        "@pipeline.build\n"
        "def build(stage):\n"
        "    stage.task(Wheel())\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    stage.task(GitHubRelease(\n"
        "        repository='octacity-org/omniship', tag='v1.0.0',\n"
        "    ))\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 0, generated.output
    workflow_root = tmp_path / ".github" / "workflows"
    check = yaml.load(
        (workflow_root / "ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    build = yaml.load(
        (workflow_root / "package.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    ship = yaml.load(
        (workflow_root / "publish.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert check["name"] == "CI"
    assert set(check["on"]) == {"pull_request", "push", "workflow_call"}
    assert "check-ruff" in check["jobs"]
    assert "build-wheel" not in check["jobs"]

    assert build["name"] == "Package"
    assert set(build["on"]) == {"workflow_call"}
    assert build["jobs"]["check"] == {
        "name": "CI",
        "uses": "./.github/workflows/ci.yml",
    }
    assert "build-wheel" in build["jobs"]
    assert "check-ruff" not in build["jobs"]

    assert ship["name"] == "Publish"
    assert ship["on"] == {"push": {"tags": ["v*"]}}
    assert ship["jobs"]["build"] == {
        "name": "Package",
        "uses": "./.github/workflows/package.yml",
    }
    assert "check" not in ship["jobs"]
    assert ship["jobs"]["ship-github-release"]["needs"] == "build"


def test_generate_writes_typed_custom_workflow_triggers(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import (\n"
        "    GitHubActions, GitHubPullRequest, GitHubPush,\n"
        "    GitHubWorkflow, GitHubWorkflowDispatch,\n"
        ")\n"
        "github = GitHubActions(\n"
        "    check=GitHubWorkflow(\n"
        "        file='ci.yml', name='CI',\n"
        "        triggers=[\n"
        "            GitHubPullRequest(\n"
        "                branches=['main', 'develop'],\n"
        "                paths=['src/**', 'tests/**'],\n"
        "            ),\n"
        "            GitHubPush(\n"
        "                branches=['main', 'develop'],\n"
        "                paths_ignore=['docs/**'],\n"
        "            ),\n"
        "            GitHubWorkflowDispatch(),\n"
        "        ],\n"
        "    ),\n"
        "    build=GitHubWorkflow(\n"
        "        file='package.yml', name='Package', triggers=[],\n"
        "    ),\n"
        "    ship=GitHubWorkflow(\n"
        "        file='publish.yml', name='Publish',\n"
        "        triggers=[GitHubPush(tags=['release-*'])],\n"
        "    ),\n"
        ")\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.check\n"
        "def check(stage):\n"
        "    @stage.task\n"
        "    def lint(ctx):\n"
        "        pass\n"
        "@pipeline.build\n"
        "def build(stage):\n"
        "    @stage.task\n"
        "    def package(ctx):\n"
        "        pass\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    @stage.task\n"
        "    def publish(ctx):\n"
        "        pass\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 0, generated.output
    workflow_root = tmp_path / ".github" / "workflows"
    check = yaml.load(
        (workflow_root / "ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    build = yaml.load(
        (workflow_root / "package.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    ship = yaml.load(
        (workflow_root / "publish.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert check["on"] == {
        "pull_request": {
            "branches": ["main", "develop"],
            "paths": ["src/**", "tests/**"],
        },
        "push": {
            "branches": ["main", "develop"],
            "paths-ignore": ["docs/**"],
        },
        "workflow_dispatch": {},
        "workflow_call": {},
    }
    assert build["on"] == {"workflow_call": {}}
    assert build["jobs"]["check"] == {
        "name": "CI",
        "uses": "./.github/workflows/ci.yml",
    }
    assert build["jobs"]["build-package"]["needs"] == "check"
    assert ship["on"] == {"push": {"tags": ["release-*"]}}
    assert ship["jobs"]["build"] == {
        "name": "Package",
        "uses": "./.github/workflows/package.yml",
    }
    assert "check" not in ship["jobs"]


def test_manual_inputs_reach_typed_and_imperative_ship_tasks(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import (\n"
        "    GitHubActions, GitHubBooleanInput, GitHubRelease,\n"
        "    GitHubStringInput, GitHubWorkflow, GitHubWorkflowDispatch,\n"
        ")\n"
        "tag = GitHubStringInput(\n"
        "    'tag', description='Tag to release', required=True,\n"
        ")\n"
        "prerelease = GitHubBooleanInput(\n"
        "    'prerelease', description='Create a prerelease', default=False,\n"
        ")\n"
        "github = GitHubActions(\n"
        "    ship=GitHubWorkflow(\n"
        "        file='ship.yml', name='Ship',\n"
        "        triggers=[GitHubWorkflowDispatch(\n"
        "            inputs=[tag, prerelease],\n"
        "        )],\n"
        "    ),\n"
        ")\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    stage.task(GitHubRelease(\n"
        "        repository='octacity-org/omniship',\n"
        "        tag=tag, prerelease=prerelease, dry_run=True,\n"
        "    ))\n"
        "    @stage.task\n"
        "    def record_input(ctx):\n"
        "        (ctx.workspace / 'selected-tag.txt').write_text(\n"
        "            ctx.inputs['tag'], encoding='utf-8',\n"
        "        )\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    generated = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 0, generated.output
    actions = yaml.load(
        (tmp_path / ".github" / "workflows" / "ship.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert actions["on"]["workflow_dispatch"]["inputs"] == {
        "tag": {
            "description": "Tag to release",
            "required": "true",
            "type": "string",
        },
        "prerelease": {
            "description": "Create a prerelease",
            "default": "false",
            "type": "boolean",
        },
    }
    release_step = actions["jobs"]["ship-github-release"]["steps"][-1]
    assert release_step["env"]["OMNISHIP_INPUTS"] == "${{ toJSON(inputs) }}"

    compiled = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert compiled["ship"]["github-release"]["with"]["tag"] == {
        "$omniship_input": "tag"
    }
    assert compiled["ship"]["github-release"]["with"]["prerelease"] == {
        "$omniship_input": "prerelease"
    }

    missing = runner.invoke(
        cli,
        [
            "run-node",
            "--stage",
            "ship",
            "--node",
            "github-release",
            "--config",
            str(output),
        ],
        env={"OMNISHIP_INPUTS": ""},
    )

    assert missing.exit_code == 1
    assert "Required workflow input 'tag' was not provided" in missing.output

    released = runner.invoke(
        cli,
        [
            "run-node",
            "--stage",
            "ship",
            "--node",
            "github-release",
            "--config",
            str(output),
        ],
        env={"OMNISHIP_INPUTS": '{"tag":"v2.0.0","prerelease":true}'},
    )

    assert released.exit_code == 0, released.output

    executed = runner.invoke(
        cli,
        [
            "run-node",
            "--stage",
            "ship",
            "--node",
            "record-input",
            "--config",
            str(output),
        ],
        env={"OMNISHIP_INPUTS": '{"tag":"v2.0.0","prerelease":true}'},
    )

    assert executed.exit_code == 0, executed.output
    assert (tmp_path / "selected-tag.txt").read_text(encoding="utf-8") == "v2.0.0"


def test_generate_github_pages_deployment(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import GitHubPages\n"
        "pipeline = Pipeline()\n"
        "@pipeline.build\n"
        "def build(stage):\n"
        "    @stage.task\n"
        "    def docs(ctx):\n"
        "        site = ctx.workspace / 'build'\n"
        "        site.mkdir()\n"
        "        (site / 'index.html').write_text('docs')\n"
        "        ctx.artifacts.add(site, name='docs-site')\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    stage.task(GitHubPages(artifact='docs-site'))\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 0, generated.output
    ship = yaml.load(
        (tmp_path / ".github" / "workflows" / "ship.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    job = ship["jobs"]["ship-github-pages"]
    assert ship["concurrency"] == {
        "group": "pages",
        "cancel-in-progress": "true",
    }
    assert job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
        "pages": "write",
    }
    assert job["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    assert {
        "uses": "actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b",
        "with": {"path": ".omniship/pages/site"},
    } in job["steps"]
    assert {
        "name": "Deploy GitHub Pages",
        "id": "deployment",
        "uses": "actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346",
    } in job["steps"]


def test_generate_uses_action_versions_from_omniship_lock(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(WORKFLOW, encoding="utf-8")
    runner = CliRunner()
    initial = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )
    assert initial.exit_code == 0, initial.output
    lock_path = tmp_path / "omniship.lock"
    custom_sha = "a" * 40
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(
            "3d3c42e5aac5ba805825da76410c181273ba90b1",
            custom_sha,
        ),
        encoding="utf-8",
    )

    regenerated = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert regenerated.exit_code == 0, regenerated.output
    check = yaml.load(
        (tmp_path / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert check["jobs"]["prepare"]["steps"][0] == {
        "uses": f"actions/checkout@{custom_sha}"
    }


def test_generate_uses_omniship_version_from_lock(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(WORKFLOW, encoding="utf-8")
    runner = CliRunner()
    initial = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )
    assert initial.exit_code == 0, initial.output
    lock_path = tmp_path / "omniship.lock"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(
            f'[omniship]\nversion = "{version("omniship")}"',
            '[omniship]\nversion = "9.8.7"',
        ),
        encoding="utf-8",
    )

    regenerated = runner.invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert regenerated.exit_code == 0, regenerated.output
    check = yaml.load(
        (tmp_path / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert check["jobs"]["prepare"]["steps"][-1]["run"].startswith(
        "uvx --from omniship==9.8.7 omniship generate"
    )


def test_generate_rejects_github_pages_matrix_deployment(tmp_path: Path) -> None:
    source = tmp_path / "workflow.py"
    output = tmp_path / "omniship.yaml"
    source.write_text(
        "from omniship import Pipeline\n"
        "from omniship.plugins.github import (\n"
        "    GitHubActions, GitHubPages, GitHubRunner,\n"
        ")\n"
        "github = GitHubActions()\n"
        "pipeline = Pipeline(targets=[github])\n"
        "@pipeline.ship\n"
        "def ship(stage):\n"
        "    stage.task(\n"
        "        GitHubPages(),\n"
        "        execution=github.job(runners=[\n"
        "            GitHubRunner.UBUNTU_24_04, GitHubRunner.MACOS_15,\n"
        "        ]),\n"
        "    )\n",
        encoding="utf-8",
    )

    generated = CliRunner().invoke(
        cli,
        ["generate", "-f", str(source), "-o", str(output)],
    )

    assert generated.exit_code == 1
    assert "GitHub Pages deployment requires exactly one runner" in generated.output
