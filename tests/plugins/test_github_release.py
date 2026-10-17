import hashlib
import io
import json
import urllib.error
from pathlib import Path

import pytest

from omniship.core.artifact import Artifact
from omniship.plugins.github import GitHub
from omniship.plugins.github import runtime as github_runtime
from omniship.runtime import TaskContext, TaskFailure

API = "https://api.github.com/repos/octacity-org/demo"
UPLOADS = "https://uploads.github.com/repos/octacity-org/demo/releases/7/assets"


class FakeGitHub:
    def __init__(self, existing: dict | None = None) -> None:
        self.existing = existing
        self.requests: list[tuple[str, str, bytes | None]] = []

    def urlopen(self, request, timeout=None):
        method = request.get_method()
        url = request.full_url
        self.requests.append((method, url, request.data))
        if method == "GET" and url == f"{API}/releases/tags/v1.0.0":
            if self.existing is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return self._response(self.existing)
        if method == "POST" and url == f"{API}/releases":
            return self._response(
                {
                    "html_url": "https://github.com/octacity-org/demo/releases/v1.0.0",
                    "upload_url": f"{UPLOADS}{{?name,label}}",
                    "assets": [],
                }
            )
        if method == "POST" and url.startswith(UPLOADS):
            return self._response({})
        raise AssertionError(f"unexpected request {method} {url}")

    @staticmethod
    def _response(document: dict):
        return io.BytesIO(json.dumps(document).encode("utf-8"))


@pytest.fixture
def api(monkeypatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github_runtime.urllib.request, "urlopen", fake.urlopen)
    return fake


def _context(tmp_path: Path, *artifacts: Artifact, **env: str) -> TaskContext:
    return TaskContext(
        tmp_path,
        {"GITHUB_TOKEN": "token", "GITHUB_SHA": "abc123", **env},
        artifacts=artifacts,
    )


def _wheel(tmp_path: Path, name: str = "demo 1.0+local.whl") -> Artifact:
    path = tmp_path / name
    path.write_bytes(b"wheel")
    return Artifact.from_path(path)


def test_release_targets_built_commit_and_encodes_asset_names(
    tmp_path: Path,
    api: FakeGitHub,
) -> None:
    GitHub(_context(tmp_path, _wheel(tmp_path))).release("octacity-org/demo", "v1.0.0")

    created = next(
        data for method, url, data in api.requests if url == f"{API}/releases"
    )
    assert json.loads(created)["target_commitish"] == "abc123"
    uploads = [url for method, url, _ in api.requests if url.startswith(UPLOADS)]
    assert uploads == [f"{UPLOADS}?name=demo%201.0%2Blocal.whl"]


def test_release_rerun_reuses_release_and_skips_identical_assets(
    tmp_path: Path,
    api: FakeGitHub,
) -> None:
    uploaded = _wheel(tmp_path, "uploaded.whl")
    missing = _wheel(tmp_path, "missing.whl")
    api.existing = {
        "html_url": "https://github.com/octacity-org/demo/releases/v1.0.0",
        "upload_url": f"{UPLOADS}{{?name,label}}",
        "assets": [
            {
                "name": "uploaded.whl",
                "digest": "sha256:" + hashlib.sha256(b"wheel").hexdigest(),
            }
        ],
    }

    result = GitHub(_context(tmp_path, uploaded, missing)).release(
        "octacity-org/demo", "v1.0.0"
    )

    assert result.release_url.endswith("/releases/v1.0.0")
    assert not any(url == f"{API}/releases" for _, url, _ in api.requests)
    uploads = [url for method, url, _ in api.requests if url.startswith(UPLOADS)]
    assert uploads == [f"{UPLOADS}?name=missing.whl"]


def test_release_rerun_rejects_conflicting_existing_asset(
    tmp_path: Path,
    api: FakeGitHub,
) -> None:
    api.existing = {
        "html_url": "https://github.com/octacity-org/demo/releases/v1.0.0",
        "upload_url": f"{UPLOADS}{{?name,label}}",
        "assets": [{"name": "app.whl", "digest": "sha256:" + "0" * 64}],
    }

    with pytest.raises(TaskFailure, match="app.whl already exists"):
        GitHub(_context(tmp_path, _wheel(tmp_path, "app.whl"))).release(
            "octacity-org/demo", "v1.0.0"
        )


def test_release_rejects_tag_different_from_pushed_tag(
    tmp_path: Path,
    api: FakeGitHub,
) -> None:
    context = _context(tmp_path, GITHUB_REF_TYPE="tag", GITHUB_REF_NAME="v1.0.1")

    with pytest.raises(TaskFailure, match="does not match the pushed tag 'v1.0.1'"):
        GitHub(context).release("octacity-org/demo", "v1.0.0")
    assert api.requests == []


def test_release_rejects_explicit_directory_assets(tmp_path: Path, api) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("site", encoding="utf-8")

    with pytest.raises(TaskFailure, match="must be a file"):
        GitHub(_context(tmp_path, Artifact.from_path(site))).release(
            "octacity-org/demo", "v1.0.0", files=["site"]
        )


def test_release_reports_skipped_directory_artifacts(tmp_path: Path, api) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("site", encoding="utf-8")
    context = _context(tmp_path, Artifact.from_path(site), _wheel(tmp_path, "a.whl"))

    result = GitHub(context).release("octacity-org/demo", "v1.0.0")

    assert result.uploaded_count == 1
    assert any(
        "Skipping directory artifact 'site'" in line for line in context.log.lines
    )


def test_release_file_references_match_whole_path_segments(
    tmp_path: Path,
    api: FakeGitHub,
) -> None:
    context = _context(
        tmp_path, _wheel(tmp_path, "myapp.whl"), _wheel(tmp_path, "app.whl")
    )

    GitHub(context).release("octacity-org/demo", "v1.0.0", files=["app.whl"])

    uploads = [url for method, url, _ in api.requests if url.startswith(UPLOADS)]
    assert uploads == [f"{UPLOADS}?name=app.whl"]
