from pathlib import Path

import pytest
from pydantic import ValidationError

from omniship.core.artifact import Artifact
from omniship.core.context import ExecutionContext
from omniship.core.node import NodeInputs
from omniship.core.stage import Stage
from omniship.plugins.github import GitHub
from omniship.plugins.github.operations import (
    GithubPagesOperation,
    GithubReleaseConfig,
    GithubReleaseOperation,
    GithubTagOperation,
)
from omniship.plugins.github.runtime import (
    GitHubPagesResult,
    GitHubReleaseResult,
    GitHubTagResult,
)
from omniship.plugins.python import operations as python_operations
from omniship.plugins.python.operations import WheelOperation
from omniship.plugins.python.runtime import PythonTools
from omniship.runtime import TaskContext


@pytest.mark.asyncio
async def test_wheel_operation_reports_an_overwritten_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "dist"
    output.mkdir()
    existing = output / "demo-1.0-py3-none-any.whl"
    existing.write_bytes(b"old wheel")

    async def build_wheel(
        context: ExecutionContext,
        arguments: list[str],
    ) -> tuple[int, str, str]:
        build_output = Path(arguments[arguments.index("--out-dir") + 1])
        build_output.mkdir(parents=True, exist_ok=True)
        (build_output / existing.name).write_bytes(b"new wheel")
        return 0, "built", ""

    monkeypatch.setattr(python_operations, "_execute_tool", build_wheel)

    result = await WheelOperation().execute(
        ExecutionContext(workspace_root=tmp_path, stage=Stage.BUILD),
        NodeInputs(),
    )

    assert result.is_success
    assert [artifact.path for artifact in result.artifacts] == [existing]
    assert existing.read_bytes() == b"new wheel"


def test_imperative_wheel_build_reports_an_overwritten_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "dist"
    output.mkdir()
    existing = output / "demo-1.0-py3-none-any.whl"
    existing.write_bytes(b"old wheel")
    tools = PythonTools(tmp_path, {})

    def build_wheel(self: PythonTools, arguments: list[str]):
        build_output = Path(arguments[arguments.index("--out-dir") + 1])
        build_output.mkdir(parents=True, exist_ok=True)
        (build_output / existing.name).write_bytes(b"new wheel")

    monkeypatch.setattr(PythonTools, "_run", build_wheel)

    assert tools.build_wheels() == [existing]
    assert existing.read_bytes() == b"new wheel"


def test_github_release_configuration_rejects_inline_tokens() -> None:
    with pytest.raises(ValidationError):
        GithubReleaseConfig.model_validate(
            {
                "repository": "octacity-org/omniship",
                "tag": "v1.0.0",
                "token": "must-not-be-accepted",
            }
        )


def test_imperative_github_release_uses_context_artifacts(tmp_path: Path) -> None:
    wheel = tmp_path / "demo.whl"
    wheel.write_bytes(b"wheel")
    context = TaskContext(
        tmp_path,
        {},
        artifacts=(Artifact.from_path(wheel, name="python-wheel"),),
    )

    result = GitHub(context).release(
        repository="octacity-org/omniship",
        tag="v1.0.0",
        dry_run=True,
    )

    assert result == GitHubReleaseResult(
        repository="octacity-org/omniship",
        tag="v1.0.0",
        release_url="https://github.com/octacity-org/omniship/releases/tag/v1.0.0",
        uploaded_count=1,
    )
    assert "Simulated release" in "\n".join(context.log.lines)
    assert "python-wheel" in "\n".join(context.log.lines)


def test_imperative_github_tag_supports_dry_run(tmp_path: Path) -> None:
    context = TaskContext(tmp_path, {"GITHUB_SHA": "abc123"})

    result = GitHub(context).tag(
        repository="octacity-org/omniship",
        tag="v1.0.0",
        dry_run=True,
    )

    assert result == GitHubTagResult(
        repository="octacity-org/omniship",
        tag="v1.0.0",
        target="abc123",
    )
    assert "Simulated tag" in "\n".join(context.log.lines)


@pytest.mark.asyncio
async def test_github_tag_operation_delegates_to_imperative_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def tag(self: GitHub, **kwargs: object) -> GitHubTagResult:
        calls.append(kwargs)
        return GitHubTagResult(
            repository=str(kwargs["repository"]),
            tag=str(kwargs["tag"]),
            target="abc123",
        )

    monkeypatch.setattr(GitHub, "tag", tag)
    result = await GithubTagOperation().execute(
        ExecutionContext(workspace_root=tmp_path, stage=Stage.SHIP),
        NodeInputs(
            params={
                "repository": "octacity-org/omniship",
                "tag": "v1.0.0",
                "target": "abc123",
                "force": True,
                "dry_run": True,
            }
        ),
    )

    assert result.is_success
    assert calls == [
        {
            "repository": "octacity-org/omniship",
            "tag": "v1.0.0",
            "target": "abc123",
            "force": True,
            "dry_run": True,
        }
    ]


@pytest.mark.asyncio
async def test_github_release_operation_delegates_to_imperative_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def release(self: GitHub, **kwargs: object) -> GitHubReleaseResult:
        calls.append(kwargs)
        return GitHubReleaseResult(
            repository=str(kwargs["repository"]),
            tag=str(kwargs["tag"]),
            release_url="https://example.test/release",
            uploaded_count=2,
        )

    monkeypatch.setattr(GitHub, "release", release)

    result = await GithubReleaseOperation().execute(
        ExecutionContext(workspace_root=tmp_path, stage=Stage.SHIP),
        NodeInputs(
            params={
                "repository": "octacity-org/omniship",
                "tag": "v1.0.0",
                "generate_notes": True,
                "title": "OmniShip 1.0",
                "draft": True,
                "files": ["dist.whl"],
                "dry_run": True,
            }
        ),
    )

    assert result.is_success
    assert result.outputs["release_url"] == "https://example.test/release"
    assert calls == [
        {
            "repository": "octacity-org/omniship",
            "tag": "v1.0.0",
            "notes": "auto",
            "title": "OmniShip 1.0",
            "draft": True,
            "prerelease": False,
            "files": ("dist.whl",),
            "dry_run": True,
        }
    ]


@pytest.mark.asyncio
async def test_github_release_requires_environment_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    result = await GithubReleaseOperation().execute(
        ExecutionContext(workspace_root=tmp_path, stage=Stage.SHIP),
        NodeInputs(
            params={"repository": "octacity-org/omniship", "tag": "v1.0.0"}
        ),
    )

    assert result.is_failed
    assert result.error_message == (
        "GITHUB_TOKEN is required for github/release when dry_run is false"
    )


@pytest.mark.asyncio
async def test_github_release_dry_run_does_not_require_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    result = await GithubReleaseOperation().execute(
        ExecutionContext(workspace_root=tmp_path, stage=Stage.SHIP),
        NodeInputs(
            params={
                "repository": "octacity-org/omniship",
                "tag": "v1.0.0",
                "dry_run": True,
            }
        ),
    )

    assert result.is_success
    assert "simulated" in result.stdout.lower()


def test_imperative_github_pages_prepares_a_site_artifact(tmp_path: Path) -> None:
    site = tmp_path / "build"
    site.mkdir()
    (site / "index.html").write_text("docs", encoding="utf-8")
    context = TaskContext(
        tmp_path,
        {},
        artifacts=(Artifact.from_path(site, name="docs-site"),),
    )

    result = GitHub(context).pages(artifact="docs-site")

    assert result == GitHubPagesResult(
        artifact="docs-site",
        path=tmp_path / ".omniship" / "pages" / "site",
    )
    assert (result.path / "index.html").read_text(encoding="utf-8") == "docs"
    assert "Prepared GitHub Pages artifact" in "\n".join(context.log.lines)


@pytest.mark.asyncio
async def test_github_pages_operation_delegates_to_imperative_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    destination = tmp_path / ".omniship" / "pages" / "site"

    def pages(self: GitHub, *, artifact: str) -> GitHubPagesResult:
        calls.append(artifact)
        return GitHubPagesResult(artifact=artifact, path=destination)

    monkeypatch.setattr(GitHub, "pages", pages)

    result = await GithubPagesOperation().execute(
        ExecutionContext(workspace_root=tmp_path, stage=Stage.SHIP),
        NodeInputs(params={"artifact": "docs-site"}),
    )

    assert result.is_success
    assert result.outputs == {
        "artifact": "docs-site",
        "path": str(destination),
    }
    assert calls == ["docs-site"]
