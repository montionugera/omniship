from calendar import Day
from dataclasses import dataclass
from datetime import UTC, time
from importlib.metadata import version
from pathlib import Path
from subprocess import CompletedProcess
from zoneinfo import available_timezones

import pytest
import yaml
from click.testing import CliRunner

from omniship.cli.app import cli
from omniship.config.models import NodeConfig, OmniShipConfig
from omniship.core.execution import CacheSpec, SecretRef
from omniship.plugins.github import (
    GitHubActions,
    GitHubActionStep,
    GitHubBooleanInput,
    GitHubBootstrap,
    GitHubContainer,
    GitHubCron,
    GitHubExternalWorkflow,
    GitHubMatrix,
    GitHubPermission,
    GitHubPermissions,
    GitHubPullRequest,
    GitHubPush,
    GitHubRunner,
    GitHubSchedule,
    GitHubShell,
    GitHubStringInput,
    GitHubTimezone,
    GitHubWorkflow,
    GitHubWorkflowArtifacts,
    GitHubWorkflowDispatch,
)
from omniship.plugins.github.actions import GitHubActionsGenerator
from omniship.plugins.github.dependencies import GitHubActionLock
from omniship.plugins.registry import PluginRegistry
from omniship.runtime import TaskContext
from omniship.workflow.compiler import compile_pipeline
from omniship.workflow.model import (
    GitPluginPackage,
    Pipeline,
    PluginIndex,
    PluginPackage,
)


def _generated_check_document(
    tmp_path: Path,
    actions: GitHubActions,
) -> dict[str, object]:
    config = OmniShipConfig(
        version=1,
        check={"verify": NodeConfig(uses="core/noop")},
    )
    generated = GitHubActionsGenerator(PluginRegistry()).generate(
        config,
        tmp_path / "workflow.py",
        tmp_path / "omniship.yaml",
        Pipeline(targets=[actions]),
    )
    check_file = next(item for item in generated if item.path.name == "check.yml")
    return yaml.safe_load(check_file.content)


def test_external_workflow_is_one_dependent_github_job(tmp_path: Path) -> None:
    github = GitHubActions()
    pipeline = Pipeline(targets=[github])

    @pipeline.check
    def check(stage):
        first = stage.task(
            GitHubExternalWorkflow(
                "octacity-org/ci/security.yml",
                ref="v1",
                inputs={"level": "strict"},
                secrets={"token": SecretRef("SECURITY_TOKEN")},
                permissions=GitHubPermissions(contents=GitHubPermission.READ),
                name="security",
            )
        )
        stage.task(
            GitHubExternalWorkflow("octacity-org/ci/audit.yml", ref="v2", name="audit"),
            after=[first],
        )

    config = compile_pipeline(pipeline, tmp_path / "workflow.py")
    generated = GitHubActionsGenerator(PluginRegistry()).generate(
        config, tmp_path / "workflow.py", tmp_path / "omniship.yaml", pipeline
    )
    document = yaml.safe_load(
        next(item.content for item in generated if item.path.name == "check.yml")
    )
    security = document["jobs"]["check-security"]
    audit = document["jobs"]["check-audit"]

    assert config.check["audit"].needs == ["security"]
    assert security == {
        "name": "Check · Security",
        "needs": "prepare",
        "uses": "octacity-org/ci/.github/workflows/security.yml@v1",
        "with": {"level": "strict"},
        "secrets": {"token": "${{ secrets.SECURITY_TOKEN }}"},
        "permissions": {"contents": "read"},
    }
    assert audit["needs"] == "check-security"
    assert audit["uses"] == "octacity-org/ci/.github/workflows/audit.yml@v2"
    assert "runs-on" not in audit and "steps" not in audit
    assert document["jobs"]["check-complete"]["needs"] == "check-audit"


def test_external_workflow_rejects_invalid_reference_and_runner_placement(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="owner/repo/workflow"):
        GitHubExternalWorkflow("octacity-org/ci/.github/workflows/check.yml", ref="v1")
    with pytest.raises(ValueError, match="ref"):
        GitHubExternalWorkflow("octacity-org/ci/check.yml", ref="")

    github = GitHubActions()
    pipeline = Pipeline(targets=[github])

    @pipeline.check
    def check(stage):
        stage.task(
            GitHubExternalWorkflow("octacity-org/ci/check.yml", ref="v1"),
            execution=github.job(),
        )

    config = compile_pipeline(pipeline, tmp_path / "workflow.py")
    with pytest.raises(ValueError, match="cannot have runner placement"):
        GitHubActionsGenerator(PluginRegistry()).generate(
            config, tmp_path / "workflow.py", tmp_path / "omniship.yaml", pipeline
        )


def test_external_build_does_not_claim_omniship_artifacts(tmp_path: Path) -> None:
    github = GitHubActions()
    pipeline = Pipeline(targets=[github])

    @pipeline.build
    def build(stage):
        stage.task(
            GitHubExternalWorkflow(
                "octacity-org/ci/build.yml", ref="v1", name="external-build"
            )
        )

    @pipeline.ship
    def ship(stage):
        @stage.task
        def publish(ctx):
            pass

    config = compile_pipeline(pipeline, tmp_path / "workflow.py")
    generated = GitHubActionsGenerator(PluginRegistry()).generate(
        config, tmp_path / "workflow.py", tmp_path / "omniship.yaml", pipeline
    )
    build_document = yaml.safe_load(
        next(item.content for item in generated if item.path.name == "build.yml")
    )
    ship_document = yaml.safe_load(
        next(item.content for item in generated if item.path.name == "ship.yml")
    )

    assert "steps" not in build_document["jobs"]["build-external-build"]
    assert not any(
        "download-artifact" in step.get("uses", "")
        for step in ship_document["jobs"]["ship-publish"]["steps"]
    )


def test_github_actions_bootstraps_omniship_as_an_isolated_tool_by_default(
    tmp_path: Path,
) -> None:
    document = _generated_check_document(tmp_path, GitHubActions())

    prepare_steps = document["jobs"]["prepare"]["steps"]
    task_steps = document["jobs"]["check-verify"]["steps"]

    assert all(
        step.get("run") != "uv sync --all-groups --locked" for step in task_steps
    )
    assert prepare_steps[-1]["run"].startswith(
        f"uvx --from omniship=={version('omniship')} omniship generate"
    )
    assert task_steps[-1]["run"].startswith(
        f"uvx --from omniship=={version('omniship')} omniship run-node"
    )


def test_isolated_bootstrap_installs_declared_plugins_for_prepare_and_tasks(
    tmp_path: Path,
) -> None:
    pipeline = Pipeline(
        targets=[GitHubActions()],
        plugins=[PluginPackage("omniship-acme", "1.2.3")],
    )
    config = OmniShipConfig(check={"verify": NodeConfig(uses="core/noop")})
    generated = GitHubActionsGenerator(PluginRegistry()).generate(
        config, tmp_path / "workflow.py", tmp_path / "omniship.yaml", pipeline
    )
    document = yaml.safe_load(
        next(item.content for item in generated if item.path.name == "check.yml")
    )
    lock = next(item.content for item in generated if item.path.name == "omniship.lock")
    expected = f"uvx --from omniship=={version('omniship')} --with omniship-acme==1.2.3 omniship"

    assert document["jobs"]["prepare"]["steps"][-1]["run"].startswith(
        expected + " generate"
    )
    assert document["jobs"]["check-verify"]["steps"][-1]["run"].startswith(
        expected + " run-node"
    )
    assert '[plugins]\n"omniship-acme" = "1.2.3"' in lock
    lock_path = tmp_path / "omniship.lock"
    lock_path.write_text(lock, encoding="utf-8")
    assert GitHubActionLock.load(lock_path).plugins == pipeline.plugins


def test_plugin_package_rejects_non_exact_versions_and_duplicates() -> None:
    with pytest.raises(ValueError, match="exact"):
        PluginPackage("omniship-acme", ">=1.2")
    with pytest.raises(ValueError, match="duplicate"):
        Pipeline(
            plugins=[
                PluginPackage("acme-plugin", "1.0.0"),
                PluginPackage("acme_plugin", "2.0.0"),
            ]
        )


def test_private_index_plugin_uses_secret_env_in_prepare_and_task(
    tmp_path: Path,
) -> None:
    plugin = PluginPackage(
        "omniship-acme",
        "1.2.3",
        index=PluginIndex(
            "acme",
            "https://packages.example.com/simple/",
            password=SecretRef("ACME_INDEX_TOKEN"),
        ),
    )
    pipeline = Pipeline(targets=[GitHubActions()], plugins=[plugin])
    config = OmniShipConfig(check={"verify": NodeConfig(uses="core/noop")})
    generated = GitHubActionsGenerator(PluginRegistry()).generate(
        config, tmp_path / "workflow.py", tmp_path / "omniship.yaml", pipeline
    )
    document = yaml.safe_load(
        next(x.content for x in generated if x.path.name == "check.yml")
    )
    for step in (
        document["jobs"]["prepare"]["steps"][-1],
        document["jobs"]["check-verify"]["steps"][-1],
    ):
        assert "--index acme=https://packages.example.com/simple/" in step["run"]
        assert "--with omniship-acme==1.2.3" in step["run"]
        assert (
            step["env"]["UV_INDEX_ACME_PASSWORD"] == "${{ secrets.ACME_INDEX_TOKEN }}"
        )
        assert step["env"]["UV_INDEX_ACME_USERNAME"] == "__token__"
        assert "ACME_INDEX_TOKEN" not in step["run"]
    lock = next(x.content for x in generated if x.path.name == "omniship.lock")
    assert "ACME_INDEX_TOKEN" in lock and "${{" not in lock
    lock_path = tmp_path / "omniship.lock"
    lock_path.write_text(lock, encoding="utf-8")
    assert GitHubActionLock.load(lock_path).plugins == (plugin,)


def test_git_plugin_is_full_sha_pinned_in_prepare_and_task(tmp_path: Path) -> None:
    sha = "a" * 40
    plugin = GitPluginPackage(
        "omniship-acme", "https://github.com/acme/omniship-acme.git", sha
    )
    pipeline = Pipeline(targets=[GitHubActions()], plugins=[plugin])
    config = OmniShipConfig(check={"verify": NodeConfig(uses="core/noop")})
    generated = GitHubActionsGenerator(PluginRegistry()).generate(
        config, tmp_path / "workflow.py", tmp_path / "omniship.yaml", pipeline
    )
    document = yaml.safe_load(
        next(x.content for x in generated if x.path.name == "check.yml")
    )
    for step in (
        document["jobs"]["prepare"]["steps"][-1],
        document["jobs"]["check-verify"]["steps"][-1],
    ):
        assert (
            f"--with 'omniship-acme @ git+https://github.com/acme/omniship-acme.git@{sha}'"
            in step["run"]
        )
    lock = next(x.content for x in generated if x.path.name == "omniship.lock")
    lock_path = tmp_path / "omniship.lock"
    lock_path.write_text(lock, encoding="utf-8")
    assert GitHubActionLock.load(lock_path).plugins == (plugin,)


def test_git_plugin_version_resolves_tag_once_and_uses_locked_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = "https://github.com/acme/omniship-acme.git"
    plugin = GitPluginPackage("omniship-acme", repository, version="v1.2.3")
    pipeline = Pipeline(targets=[GitHubActions()], plugins=[plugin])
    config = OmniShipConfig(check={"verify": NodeConfig(uses="core/noop")})
    commit = "b" * 40
    calls = []

    def ls_remote(command, **kwargs):
        calls.append(command)
        return CompletedProcess(
            command,
            0,
            stdout=f"{'a' * 40}\trefs/tags/v1.2.3\n{commit}\trefs/tags/v1.2.3^{{}}\n",
            stderr="",
        )

    monkeypatch.setattr(
        "omniship.plugins.github.dependencies.subprocess.run", ls_remote
    )
    generator = GitHubActionsGenerator(PluginRegistry())
    generated = generator.generate(
        config, tmp_path / "workflow.py", tmp_path / "omniship.yaml", pipeline
    )
    lock = next(x.content for x in generated if x.path.name == "omniship.lock")
    check = yaml.safe_load(
        next(x.content for x in generated if x.path.name == "check.yml")
    )
    assert 'version = "v1.2.3"' in lock
    assert f'commit = "{commit}"' in lock
    assert f"@{commit}" in check["jobs"]["prepare"]["steps"][-1]["run"]
    assert "@v1.2.3" not in check["jobs"]["prepare"]["steps"][-1]["run"]
    assert len(calls) == 1

    (tmp_path / "omniship.lock").write_text(lock, encoding="utf-8")
    generator.generate(
        config, tmp_path / "workflow.py", tmp_path / "omniship.yaml", pipeline
    )
    assert len(calls) == 1


def test_update_plugins_refreshes_git_version_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = GitPluginPackage(
        "omniship-acme",
        "https://github.com/acme/omniship-acme.git",
        "a" * 40,
        version="v1.2.3",
    )
    lock_path = tmp_path / "omniship.lock"
    lock_path.write_text(
        GitHubActionLock.defaults().with_plugins((plugin,)).render(), encoding="utf-8"
    )

    def ls_remote(command, **kwargs):
        return CompletedProcess(
            command, 0, stdout=f"{'b' * 40}\trefs/tags/v1.2.3\n", stderr=""
        )

    monkeypatch.setattr(
        "omniship.plugins.github.dependencies.subprocess.run", ls_remote
    )
    result = CliRunner().invoke(
        cli, ["update", "plugins", "--lock-file", str(lock_path)]
    )

    assert result.exit_code == 0, result.output
    assert GitHubActionLock.load(lock_path).plugins == (
        GitPluginPackage(plugin.name, plugin.repository, "b" * 40, version="v1.2.3"),
    )


def test_external_plugin_sources_reject_unpinned_and_credential_urls() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        PluginIndex(
            "acme", "http://packages.example.com/simple/", password=SecretRef("TOKEN")
        )
    with pytest.raises(ValueError, match="credentials"):
        PluginIndex(
            "acme",
            "https://user:pass@packages.example.com/simple/",
            password=SecretRef("TOKEN"),
        )
    with pytest.raises(ValueError, match="40-character"):
        GitPluginPackage("acme", "https://github.com/acme/plugin.git", "main")
    with pytest.raises(ValueError, match="exact version tag"):
        GitPluginPackage("acme", "https://github.com/acme/plugin.git", version="main")
    with pytest.raises(ValueError, match="credentials"):
        GitPluginPackage("acme", "https://token@github.com/acme/plugin.git", "a" * 40)


def test_update_omniship_preserves_external_plugin_sources(tmp_path: Path) -> None:
    plugins = (
        PluginPackage(
            "private-plugin",
            "1.0.0",
            index=PluginIndex(
                "acme",
                "https://packages.example.com/simple/",
                password=SecretRef("ACME_TOKEN"),
            ),
        ),
        GitPluginPackage("git-plugin", "https://github.com/acme/plugin.git", "a" * 40),
    )
    lock_path = tmp_path / "omniship.lock"
    lock_path.write_text(
        GitHubActionLock.defaults().with_plugins(plugins).render(), encoding="utf-8"
    )

    result = CliRunner().invoke(
        cli, ["update", "omniship", "--lock-file", str(lock_path)]
    )

    assert result.exit_code == 0, result.output
    assert GitHubActionLock.load(lock_path).plugins == plugins


def test_update_omniship_preserves_declared_plugin_pins(tmp_path: Path) -> None:
    lock_path = tmp_path / "omniship.lock"
    lock_path.write_text(
        GitHubActionLock.defaults()
        .with_plugins((PluginPackage("omniship-acme", "1.2.3"),))
        .render(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli, ["update", "omniship", "--lock-file", str(lock_path)]
    )

    assert result.exit_code == 0, result.output
    assert GitHubActionLock.load(lock_path).plugins == (
        PluginPackage("omniship-acme", "1.2.3"),
    )


def test_github_actions_can_bootstrap_omniship_from_the_workspace(
    tmp_path: Path,
) -> None:
    document = _generated_check_document(
        tmp_path,
        GitHubActions(bootstrap=GitHubBootstrap.WORKSPACE),
    )

    task_steps = document["jobs"]["check-verify"]["steps"]

    assert {"run": "uv sync --all-groups --locked"} in task_steps
    assert task_steps[-1]["run"].startswith("uv run omniship run-node")


def test_github_runner_contains_every_standard_hosted_label() -> None:
    assert {runner.value for runner in GitHubRunner} == {
        "ubuntu-slim",
        "ubuntu-latest",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "ubuntu-26.04",
        "ubuntu-22.04-arm",
        "ubuntu-24.04-arm",
        "ubuntu-26.04-arm",
        "windows-latest",
        "windows-2022",
        "windows-2025",
        "windows-2025-vs2026",
        "windows-11-arm",
        "windows-11-vs2026-arm",
        "macos-latest",
        "macos-14",
        "macos-15",
        "macos-26",
        "macos-15-intel",
        "macos-26-intel",
        "xcode-27",
    }


def test_github_actions_creates_node_placement_with_runner_matrix() -> None:
    actions = GitHubActions(default_runner=GitHubRunner.UBUNTU_24_04)

    placement = actions.job(
        runners=[GitHubRunner.UBUNTU_24_04, GitHubRunner.MACOS_15],
        fail_fast=False,
    )

    assert placement.runners == (
        GitHubRunner.UBUNTU_24_04,
        GitHubRunner.MACOS_15,
    )
    assert placement.fail_fast is False
    assert actions.default_job.runners == (GitHubRunner.UBUNTU_24_04,)


def test_github_actions_creates_a_fully_configured_job() -> None:
    actions = GitHubActions()
    cache = CacheSpec(paths=["~/.cache/demo", "build"], key="demo-cache")

    placement = actions.job(
        timeout_minutes=30,
        environment="release",
        working_directory="packages/cli",
        shell=GitHubShell.BASH,
        env={
            "MODE": "release",
            "NPM_TOKEN": SecretRef("NPM_TOKEN"),
        },
        caches=[cache],
    )

    assert placement.timeout_minutes == 30
    assert placement.environment == "release"
    assert placement.working_directory == "packages/cli"
    assert placement.shell == GitHubShell.BASH
    assert placement.env == {
        "MODE": "release",
        "NPM_TOKEN": SecretRef("NPM_TOKEN"),
    }
    assert placement.caches == (cache,)


def test_github_job_compiles_matrix_container_steps_and_environment(
    tmp_path: Path,
) -> None:
    github = GitHubActions()
    placement = github.job(
        runners=[GitHubRunner.UBUNTU_24_04],
        matrix=GitHubMatrix(
            axes={"go": ["1.25", "1.26"], "variant": ["static", "dynamic"]},
            exclude=[{"go": "1.25", "variant": "dynamic"}],
            include=[{"runner": "ubuntu-24.04", "go": "1.27", "variant": "static"}],
        ),
        max_parallel=2,
        container=GitHubContainer(image="golang:1.26"),
        services={"postgres": GitHubContainer(image="postgres:17", ports=[5432])},
        before_steps=[
            GitHubActionStep(
                "setup-go", "Set up Go", {"go-version": github.matrix("go")}
            )
        ],
        after_steps=[
            GitHubActionStep("cache", "Save cache", {"path": "build", "key": "build"})
        ],
        environment="release",
        environment_url="https://example.com/release",
        env={"GO_VERSION": github.matrix("go")},
    )
    config = OmniShipConfig(
        check={"verify": NodeConfig(uses="core/noop", execution=placement)}
    )
    generated = GitHubActionsGenerator(PluginRegistry()).generate(
        config,
        tmp_path / "workflow.py",
        tmp_path / "omniship.yaml",
        Pipeline(targets=[github]),
    )
    document = yaml.safe_load(
        next(item.content for item in generated if item.path.name == "check.yml")
    )
    job = document["jobs"]["check-verify"]

    assert job["strategy"]["matrix"] == {
        "runner": ["ubuntu-24.04"],
        "go": ["1.25", "1.26"],
        "variant": ["static", "dynamic"],
        "exclude": [{"go": "1.25", "variant": "dynamic"}],
        "include": [{"runner": "ubuntu-24.04", "go": "1.27", "variant": "static"}],
    }
    assert job["runs-on"] == "${{ matrix.runner }}"
    assert job["strategy"]["max-parallel"] == 2
    assert job["container"] == {"image": "golang:1.26"}
    assert job["services"] == {"postgres": {"image": "postgres:17", "ports": [5432]}}
    assert job["environment"] == {
        "name": "release",
        "url": "https://example.com/release",
    }
    assert any(
        step.get("name") == "Set up Go"
        and step.get("with", {}).get("go-version") == "${{ matrix.go }}"
        for step in job["steps"]
    )
    assert any(step.get("name") == "Save cache" for step in job["steps"])
    run_step = next(step for step in job["steps"] if step.get("name") == "Run Verify")
    assert run_step["env"]["GO_VERSION"] == "${{ matrix.go }}"


def test_github_job_outputs_are_available_to_dependent_job(tmp_path: Path) -> None:
    github = GitHubActions()
    producer = github.job(outputs=["version"])
    consumer = github.job(env={"VERSION": github.output("check", "prepare", "version")})
    config = OmniShipConfig(
        check={
            "prepare": NodeConfig(uses="core/noop", execution=producer),
            "consume": NodeConfig(
                uses="core/noop", needs=["prepare"], execution=consumer
            ),
        }
    )
    generated = GitHubActionsGenerator(PluginRegistry()).generate(
        config,
        tmp_path / "workflow.py",
        tmp_path / "omniship.yaml",
        Pipeline(targets=[github]),
    )
    document = yaml.safe_load(
        next(item.content for item in generated if item.path.name == "check.yml")
    )
    assert document["jobs"]["check-prepare"]["outputs"] == {
        "version": "${{ steps.omniship.outputs.version }}"
    }
    assert (
        document["jobs"]["check-prepare"]["steps"][-1]["env"]["OMNISHIP_OUTPUT"]
        == "$GITHUB_OUTPUT"
    )
    assert document["jobs"]["check-consume"]["steps"][-1]["env"]["VERSION"] == (
        "${{ needs.check-prepare.outputs.version }}"
    )


def test_github_output_reference_requires_a_declared_dependency(tmp_path: Path) -> None:
    github = GitHubActions()
    config = OmniShipConfig(
        check={
            "prepare": NodeConfig(
                uses="core/noop", execution=github.job(outputs=["version"])
            ),
            "consume": NodeConfig(
                uses="core/noop",
                execution=github.job(
                    env={"VERSION": github.output("check", "prepare", "version")}
                ),
            ),
        }
    )

    with pytest.raises(ValueError, match="depend"):
        GitHubActionsGenerator(PluginRegistry()).generate(
            config,
            tmp_path / "workflow.py",
            tmp_path / "omniship.yaml",
            Pipeline(targets=[github]),
        )


def test_task_context_writes_declared_omniship_outputs(tmp_path: Path) -> None:
    output_file = tmp_path / "omniship-output"
    context = TaskContext(tmp_path, {"OMNISHIP_OUTPUT": str(output_file)})

    context.outputs.set("version", "1.2.3")
    context.outputs.set("notes", "first\nsecond")

    assert context.outputs.get("version") == "1.2.3"
    content = output_file.read_text(encoding="utf-8")
    assert "version=1.2.3\n" in content
    assert "notes<<" in content
    assert "first\nsecond\n" in content


def test_task_context_prefers_omniship_output_over_github_output(
    tmp_path: Path,
) -> None:
    omniship_output = tmp_path / "omniship-output"
    github_output = tmp_path / "github-output"
    context = TaskContext(
        tmp_path,
        {
            "OMNISHIP_OUTPUT": str(omniship_output),
            "GITHUB_OUTPUT": str(github_output),
        },
    )

    context.outputs.set("key", "val")
    assert "key=val\n" in omniship_output.read_text(encoding="utf-8")
    assert not github_output.exists()


def test_task_context_resolves_github_output_variable(tmp_path: Path) -> None:
    github_output = tmp_path / "github-output"
    context = TaskContext(
        tmp_path,
        {
            "OMNISHIP_OUTPUT": "$GITHUB_OUTPUT",
            "GITHUB_OUTPUT": str(github_output),
        },
    )

    context.outputs.set("key", "val")
    assert "key=val\n" in github_output.read_text(encoding="utf-8")


def test_task_context_resolves_custom_output_variable(tmp_path: Path) -> None:
    custom_output = tmp_path / "custom-output"
    context = TaskContext(
        tmp_path,
        {
            "OMNISHIP_OUTPUT": "${CUSTOM_OUT}",
            "CUSTOM_OUT": str(custom_output),
        },
    )

    context.outputs.set("key", "val")
    assert "key=val\n" in custom_output.read_text(encoding="utf-8")


def test_task_context_ignores_github_output_without_omniship_output(
    tmp_path: Path,
) -> None:
    github_output = tmp_path / "github-output"
    context = TaskContext(tmp_path, {"GITHUB_OUTPUT": str(github_output)})

    context.outputs.set("key", "val")
    assert context.outputs.get("key") == "val"
    assert not github_output.exists()


def test_github_matrix_include_requires_configured_runner() -> None:
    github = GitHubActions()
    with pytest.raises(ValueError, match="runner"):
        github.job(
            matrix=GitHubMatrix(
                axes={"version": ["1", "2"]},
                include=[{"version": "3"}],
            )
        )


def test_github_action_step_accepts_explicit_pinned_action(tmp_path: Path) -> None:
    github = GitHubActions()
    reference = "docker/login-action@" + "a" * 40
    placement = github.job(before_steps=[GitHubActionStep(reference, "Log in")])
    config = OmniShipConfig(
        check={"verify": NodeConfig(uses="core/noop", execution=placement)}
    )

    generated = GitHubActionsGenerator(PluginRegistry()).generate(
        config,
        tmp_path / "workflow.py",
        tmp_path / "omniship.yaml",
        Pipeline(targets=[github]),
    )
    document = yaml.safe_load(
        next(item.content for item in generated if item.path.name == "check.yml")
    )
    assert {"name": "Log in", "uses": reference} in document["jobs"]["check-verify"][
        "steps"
    ]

    with pytest.raises(ValueError, match="SHA"):
        GitHubActionStep("docker/login-action@v3", "Log in")


def test_github_actions_builds_cache_keys_and_secret_references() -> None:
    github = GitHubActions()

    cache = github.cache(
        paths=["~/.cargo/registry", "target"],
        key=[
            "cargo",
            github.runner_os,
            github.runner_arch,
            github.hash_files("Cargo.lock"),
        ],
        restore_prefixes=["cargo-${{ runner.os }}-"],
    )

    assert cache == CacheSpec(
        paths=["~/.cargo/registry", "target"],
        key=(
            "cargo-${{ runner.os }}-${{ runner.arch }}-${{ hashFiles('Cargo.lock') }}"
        ),
        restore_keys=["cargo-${{ runner.os }}-"],
    )
    assert github.secret("NPM_TOKEN") == SecretRef("NPM_TOKEN")


def test_github_actions_declares_a_typed_external_checkout() -> None:
    github = GitHubActions()

    checkout = github.checkout(
        repository="ata-sesli/zova",
        ref="0123456789abcdef",
        path=".deps/zova",
        fetch_depth=1,
        persist_credentials=False,
        submodules=True,
        token=github.secret("ZOVA_TOKEN"),
    )

    assert checkout.name == "github/checkout:.deps/zova"
    assert checkout.repository == "ata-sesli/zova"
    assert checkout.token == SecretRef("ZOVA_TOKEN")


def test_github_actions_declares_reviewed_workflow_artifacts() -> None:
    github = GitHubActions()
    run_id = GitHubStringInput("source_run_id", required=True)

    requirement = github.workflow_artifacts(
        repository="octacity-org/omniship",
        run_id=run_id,
        pattern="omniship-build-*",
        token=github.secret("SOURCE_REPOSITORY_TOKEN"),
    )

    assert requirement.repository == "octacity-org/omniship"
    assert requirement.run_id is run_id
    assert requirement.token == SecretRef("SOURCE_REPOSITORY_TOKEN")


@pytest.mark.parametrize("timeout", [0, -1, 361])
def test_github_job_rejects_invalid_timeout(timeout: int) -> None:
    with pytest.raises(ValueError, match="timeout"):
        GitHubActions().job(timeout_minutes=timeout)


@pytest.mark.parametrize(
    "working_directory", ["../outside", "/tmp/project", "C:\\project", "C:project"]
)
def test_github_job_rejects_working_directory_outside_workspace(
    working_directory: str,
) -> None:
    with pytest.raises(ValueError, match="working_directory"):
        GitHubActions().job(working_directory=working_directory)


def test_github_generator_provisions_task_requirements_before_execution(
    tmp_path: Path,
) -> None:
    @dataclass(frozen=True)
    class Toolchain:
        version: str
        name: str = "example/toolchain"

    registry = PluginRegistry()
    registry.register_requirement_resolver(
        "github/actions",
        Toolchain,
        lambda requirement: {
            "name": "Set up example toolchain",
            "run": f"setup-example {requirement.version}",
        },
    )
    actions = GitHubActions()
    config = OmniShipConfig(
        version=1,
        check={
            "verify": NodeConfig(
                uses="core/noop",
                requirements=(Toolchain("1.2.3"),),
            )
        },
    )

    generated = GitHubActionsGenerator(registry).generate(
        config,
        tmp_path / "workflow.py",
        tmp_path / "omniship.yaml",
        Pipeline(targets=[actions]),
    )
    check_file = next(item for item in generated if item.path.name == "check.yml")
    document = yaml.safe_load(check_file.content)
    steps = document["jobs"]["check-verify"]["steps"]

    assert steps[2] == {
        "name": "Set up example toolchain",
        "run": "setup-example 1.2.3",
    }


def test_github_generator_reviews_and_imports_prior_workflow_artifacts(
    tmp_path: Path,
) -> None:
    registry = PluginRegistry()
    registry.register_requirement_resolver(
        "github/actions",
        GitHubWorkflowArtifacts,
        lambda requirement: requirement,
    )
    github = GitHubActions()
    requirement = github.workflow_artifacts(
        repository="octacity-org/omniship",
        run_id=42,
        pattern="omniship-build-*",
    )
    config = OmniShipConfig(
        check={
            "verify": NodeConfig(
                uses="core/noop",
                requirements=(requirement,),
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
    job = document["jobs"]["check-verify"]

    assert job["permissions"] == {"actions": "read", "contents": "read"}
    assert any(
        step.get("name") == "Review artifacts from run 42" for step in job["steps"]
    )
    assert {
        "name": "Download artifacts from run 42",
        "uses": ("actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"),
        "with": {
            "pattern": "omniship-build-*",
            "path": ".omniship/imports/external-1",
            "github-token": "${{ github.token }}",
            "repository": "octacity-org/omniship",
            "run-id": "42",
        },
    } in job["steps"]
    run_step = next(step for step in job["steps"] if step.get("name") == "Run Verify")
    assert run_step["run"].endswith("--import-artifacts-root .omniship/imports")


def test_github_actions_uses_read_only_defaults_and_job_permission_overrides() -> None:
    actions = GitHubActions(default_runner=GitHubRunner.UBUNTU_24_04)
    permissions = GitHubPermissions(
        contents=GitHubPermission.WRITE,
        packages=GitHubPermission.WRITE,
    )

    placement = actions.job(permissions=permissions)

    assert actions.default_permissions == GitHubPermissions(
        contents=GitHubPermission.READ
    )
    assert placement.runners == (GitHubRunner.UBUNTU_24_04,)
    assert placement.permissions == permissions
    assert permissions.to_document() == {
        "contents": "write",
        "packages": "write",
    }


def test_github_actions_uses_stage_workflow_defaults() -> None:
    actions = GitHubActions()

    assert actions.check == GitHubWorkflow(file="check.yml", name="Check")
    assert actions.build == GitHubWorkflow(file="build.yml", name="Build")
    assert actions.ship == GitHubWorkflow(file="ship.yml", name="Ship")


def test_github_actions_rejects_duplicate_stage_workflow_files() -> None:
    duplicate = GitHubWorkflow(file="pipeline.yml", name="Pipeline")

    with pytest.raises(ValueError, match="must be unique"):
        GitHubActions(check=duplicate, build=duplicate)


@pytest.mark.parametrize("filename", ["workflow", "nested/check.yml", "../check.yml"])
def test_github_workflow_rejects_invalid_filename(filename: str) -> None:
    with pytest.raises(ValueError, match="workflow file"):
        GitHubWorkflow(file=filename, name="Check")


def test_github_workflow_normalizes_typed_triggers() -> None:
    workflow = GitHubWorkflow(
        file="ci.yml",
        name="CI",
        triggers=[
            GitHubPullRequest(branches=["main"]),
            GitHubPush(branches=["main", "develop"]),
            GitHubWorkflowDispatch(),
        ],
    )

    assert workflow.triggers == (
        GitHubPullRequest(branches=("main",)),
        GitHubPush(branches=("main", "develop")),
        GitHubWorkflowDispatch(),
    )


def test_github_workflow_dispatch_renders_typed_inputs() -> None:
    dispatch = GitHubWorkflowDispatch(
        inputs=[
            GitHubStringInput(
                "tag",
                description="Tag to release",
                required=True,
            ),
            GitHubBooleanInput("prerelease", default=False),
        ]
    )

    assert dispatch.to_document() == {
        "inputs": {
            "tag": {
                "description": "Tag to release",
                "required": True,
                "type": "string",
            },
            "prerelease": {
                "default": False,
                "type": "boolean",
            },
        }
    }


def test_github_workflow_dispatch_rejects_duplicate_input_names() -> None:
    with pytest.raises(ValueError, match="unique names"):
        GitHubWorkflowDispatch(
            inputs=[GitHubStringInput("version"), GitHubBooleanInput("version")]
        )


def test_github_workflow_input_rejects_default_of_wrong_type() -> None:
    with pytest.raises(TypeError, match="string input default"):
        GitHubStringInput("tag", default=False)  # type: ignore[arg-type]


def test_github_workflow_rejects_duplicate_trigger_types() -> None:
    with pytest.raises(ValueError, match="duplicate trigger"):
        GitHubWorkflow(
            file="ci.yml",
            name="CI",
            triggers=[GitHubPush(branches=["main"]), GitHubPush(tags=["v*"])],
        )


def test_github_push_rejects_conflicting_filters() -> None:
    with pytest.raises(ValueError, match="branches and branches_ignore"):
        GitHubPush(branches=["main"], branches_ignore=["legacy"])


def test_github_schedule_renders_every_cron_with_shared_timezone(
    tmp_path: Path,
) -> None:
    actions = GitHubActions(
        check=GitHubWorkflow(
            file="check.yml",
            name="Check",
            triggers=[
                GitHubPullRequest(),
                GitHubSchedule(
                    [
                        GitHubCron.weekdays(at=time(3, 0)),
                        GitHubCron.weekly(Day.SUNDAY, at=time(12, 30)),
                    ],
                    timezone=GitHubTimezone.EUROPE_ISTANBUL,
                ),
            ],
        )
    )

    document = _generated_check_document(tmp_path, actions)

    assert document["on"]["schedule"] == [
        {"cron": "0 3 * * 1-5", "timezone": "Europe/Istanbul"},
        {"cron": "30 12 * * 0", "timezone": "Europe/Istanbul"},
    ]


def test_github_schedule_omits_timezone_when_unset() -> None:
    schedule = GitHubSchedule([GitHubCron.daily(at=time(2, 30))])

    assert schedule.to_document() == [{"cron": "30 2 * * *"}]


@pytest.mark.parametrize(
    ("cron", "expression"),
    [
        (GitHubCron.every(minutes=15), "*/15 * * * *"),
        (GitHubCron.hourly(), "0 * * * *"),
        (GitHubCron.hourly(minute=45), "45 * * * *"),
        (GitHubCron.daily(at=time(23, 5)), "5 23 * * *"),
        (GitHubCron.weekdays(at=time(9)), "0 9 * * 1-5"),
        (
            GitHubCron.weekly(Day.WEDNESDAY, Day.MONDAY, Day.SUNDAY, at=time(6)),
            "0 6 * * 0,1,3",
        ),
        (GitHubCron.monthly(day=1, at=time(4)), "0 4 1 * *"),
        (GitHubCron("15 4 1-7 JAN,JUL MON"), "15 4 1-7 JAN,JUL MON"),
    ],
)
def test_github_cron_builders_render_expressions(
    cron: GitHubCron, expression: str
) -> None:
    assert cron.expression == expression


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: GitHubCron.every(minutes=1), "5, 6, 10, 12, 15, 20, or 30"),
        (lambda: GitHubCron.every(minutes=7), "5, 6, 10, 12, 15, 20, or 30"),
        (lambda: GitHubCron.hourly(minute=60), "minute"),
        (lambda: GitHubCron.daily(at=time(3, 0, 30)), "whole minutes"),
        (lambda: GitHubCron.daily(at=time(3, tzinfo=UTC)), "timezone"),
        (lambda: GitHubCron.weekly(at=time(3)), "at least one day"),
        (lambda: GitHubCron.weekly(0, at=time(3)), "calendar.Day"),
        (lambda: GitHubCron.monthly(day=29, at=time(3)), "1 to 28"),
    ],
)
def test_github_cron_builders_reject_invalid_values(build, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        build()


@pytest.mark.parametrize(
    ("cron", "message"),
    [
        ("0 3 * *", "5 fields"),
        ("0 3 * * * *", "5 fields"),
        ("60 3 * * *", "minute"),
        ("0 24 * * *", "hour"),
        ("0 3 0 * *", "day of month"),
        ("0 3 * 13 *", "month"),
        ("0 3 * * 7", "day of week"),
        ("0 3 * * FUN", "day of week"),
        ("*/0 3 * * *", "minute"),
        ("0 17-9 * * *", "hour"),
        ("0 3 1,,2 * *", "day of month"),
        ("@daily", "5 fields"),
    ],
)
def test_github_cron_rejects_invalid_expression(cron: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        GitHubCron(cron)


def test_github_schedule_requires_typed_crons() -> None:
    with pytest.raises(TypeError, match="GitHubCron"):
        GitHubSchedule(["0 3 * * *"])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="at least one cron"):
        GitHubSchedule([])


def test_github_schedule_requires_typed_timezone() -> None:
    with pytest.raises(TypeError, match="GitHubTimezone"):
        GitHubSchedule(
            [GitHubCron.daily(at=time(3))],
            timezone="Europe/Istanbul",  # type: ignore[arg-type]
        )


def test_github_timezones_are_known_iana_zones() -> None:
    zones = available_timezones()
    if not zones:
        pytest.skip("No IANA time zone database is available")

    assert [zone.value for zone in GitHubTimezone if zone.value not in zones] == []


def test_github_schedule_cannot_start_ship_workflow() -> None:
    with pytest.raises(ValueError, match="Ship workflow cannot use a schedule"):
        GitHubActions(
            ship=GitHubWorkflow(
                file="ship.yml",
                name="Ship",
                triggers=[GitHubSchedule([GitHubCron.daily(at=time(3))])],
            )
        )


def test_github_schedule_rejects_dispatch_inputs_in_same_workflow() -> None:
    with pytest.raises(ValueError, match="scheduled runs receive no inputs"):
        GitHubWorkflow(
            file="check.yml",
            name="Check",
            triggers=[
                GitHubSchedule([GitHubCron.daily(at=time(3))]),
                GitHubWorkflowDispatch(
                    inputs=[GitHubBooleanInput("full", default=False)]
                ),
            ],
        )


def test_github_schedule_allows_dispatch_without_inputs() -> None:
    nightly = GitHubSchedule([GitHubCron.daily(at=time(3))])
    workflow = GitHubWorkflow(
        file="check.yml",
        name="Check",
        triggers=[nightly, GitHubWorkflowDispatch()],
    )

    assert workflow.triggers == (nightly, GitHubWorkflowDispatch())


def test_github_actions_rejects_empty_node_runner_list() -> None:
    actions = GitHubActions()

    with pytest.raises(ValueError, match="at least one runner"):
        actions.job(runners=[])


def test_github_actions_rejects_untyped_runner_values() -> None:
    actions = GitHubActions()

    with pytest.raises(TypeError, match="GitHubRunner"):
        actions.job(runners=["ubuntu-latest"])  # type: ignore[list-item]


def test_github_permissions_reject_invalid_access_for_id_token() -> None:
    with pytest.raises(ValueError, match="id_token"):
        GitHubPermissions(id_token=GitHubPermission.READ)


def test_github_permissions_add_missing_typed_block_requirements() -> None:
    requested = GitHubPermissions(packages=GitHubPermission.WRITE)
    required = GitHubPermissions(contents=GitHubPermission.WRITE)

    merged = requested.with_minimum(required, node_name="release")

    assert merged.to_document() == {
        "contents": "write",
        "packages": "write",
    }
