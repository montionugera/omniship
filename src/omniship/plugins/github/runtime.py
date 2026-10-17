from __future__ import annotations

import hashlib
import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from omniship.core.artifact import Artifact
from omniship.runtime import TaskContext, TaskFailure

PAGES_STAGING_PATH = Path(".omniship/pages/site")


@dataclass(frozen=True, slots=True)
class GitHubReleaseResult:
    repository: str
    tag: str
    release_url: str
    uploaded_count: int


@dataclass(frozen=True, slots=True)
class GitHubPagesResult:
    artifact: str
    path: Path


@dataclass(frozen=True, slots=True)
class GitHubTagResult:
    repository: str
    tag: str
    target: str


class GitHub:
    """Typed GitHub capability facade for an imperative task context."""

    def __init__(self, context: TaskContext) -> None:
        self.context = context

    def release(
        self,
        repository: str,
        tag: str,
        *,
        notes: str | None = "auto",
        title: str | None = None,
        draft: bool = False,
        prerelease: bool = False,
        files: Iterable[str] = (),
        dry_run: bool = False,
    ) -> GitHubReleaseResult:
        token = self.context.env.get("GITHUB_TOKEN")
        if not dry_run and not token:
            raise TaskFailure(
                "GITHUB_TOKEN is required for github/release when dry_run is false"
            )

        pushed_tag = self.context.env.get("GITHUB_REF_NAME")
        if (
            not dry_run
            and self.context.env.get("GITHUB_REF_TYPE") == "tag"
            and pushed_tag
            and pushed_tag != tag
        ):
            raise TaskFailure(
                f"Release tag '{tag}' does not match the pushed tag '{pushed_tag}'; "
                "update the project version and regenerate the workflow"
            )

        artifacts = self._select_artifacts(tuple(files))
        release_url = f"https://github.com/{repository}/releases/tag/{tag}"
        if dry_run:
            self._log_dry_run(repository, tag, title, artifacts)
            return GitHubReleaseResult(
                repository=repository,
                tag=tag,
                release_url=release_url,
                uploaded_count=len(artifacts),
            )

        try:
            # A re-run reuses the release created by an earlier attempt.
            data = self._existing_release(token, repository, tag)
            if data is None:
                document: dict[str, object] = {
                    "tag_name": tag,
                    "name": title or tag,
                    "body": "" if notes in (None, "auto") else notes,
                    "generate_release_notes": notes == "auto",
                    "draft": draft,
                    "prerelease": prerelease,
                }
                # Used only when the tag does not exist yet.
                if target := self.context.env.get("GITHUB_SHA"):
                    document["target_commitish"] = target
                request = urllib.request.Request(
                    f"https://api.github.com/repos/{repository}/releases",
                    data=json.dumps(document).encode("utf-8"),
                    headers=self._api_headers(token),
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    data = json.loads(response.read().decode("utf-8"))
            else:
                self.context.log.info(f"Reusing existing release {tag}")
            release_url = data.get("html_url", "")
            self._upload_artifacts(token, data, artifacts)
        except TaskFailure:
            raise
        except Exception as exc:
            raise TaskFailure(f"GitHub release creation failed: {exc}") from exc

        self.context.log.info(
            f"Successfully published release {tag} to {release_url}"
        )
        return GitHubReleaseResult(
            repository=repository,
            tag=tag,
            release_url=release_url,
            uploaded_count=len(artifacts),
        )

    def tag(
        self,
        repository: str,
        tag: str,
        *,
        target: str | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> GitHubTagResult:
        target = target or self.context.env.get("GITHUB_SHA")
        if not target:
            raise TaskFailure(
                "GitHub tag target is required outside GitHub Actions"
            )
        token = self.context.env.get("GITHUB_TOKEN")
        if not dry_run and not token:
            raise TaskFailure(
                "GITHUB_TOKEN is required for github/tag when dry_run is false"
            )
        if dry_run:
            self.context.log.info("[github/tag] Simulated tag creation (dry run)")
            self.context.log.info(f"[github/tag] Target repository: {repository}")
            self.context.log.info(f"[github/tag] Tag: {tag} -> {target}")
            return GitHubTagResult(repository, tag, target)

        ref = f"tags/{tag}"
        try:
            if force:
                request = urllib.request.Request(
                    f"https://api.github.com/repos/{repository}/git/refs/{ref}",
                    data=json.dumps({"sha": target, "force": True}).encode("utf-8"),
                    headers=self._api_headers(token),
                    method="PATCH",
                )
            else:
                request = urllib.request.Request(
                    f"https://api.github.com/repos/{repository}/git/refs",
                    data=json.dumps(
                        {"ref": f"refs/tags/{tag}", "sha": target}
                    ).encode("utf-8"),
                    headers=self._api_headers(token),
                    method="POST",
                )
            with urllib.request.urlopen(request, timeout=15):
                pass
        except Exception as exc:
            raise TaskFailure(f"GitHub tag creation failed: {exc}") from exc
        self.context.log.info(f"Created GitHub tag {tag} at {target}")
        return GitHubTagResult(repository, tag, target)

    def pages(self, *, artifact: str = "site") -> GitHubPagesResult:
        selected = self.context.artifacts.get(artifact)
        if selected is None:
            raise TaskFailure(f"GitHub Pages artifact '{artifact}' was not found")
        if not selected.path.is_dir():
            raise TaskFailure(
                f"GitHub Pages artifact '{artifact}' must be a directory"
            )

        symlink = next(
            (candidate for candidate in selected.path.rglob("*") if candidate.is_symlink()),
            None,
        )
        if symlink is not None:
            raise TaskFailure(
                f"GitHub Pages artifact '{artifact}' contains a symlink: {symlink}"
            )
        if not any(candidate.is_file() for candidate in selected.path.rglob("*")):
            raise TaskFailure(f"GitHub Pages artifact '{artifact}' is empty")

        destination = (self.context.workspace / PAGES_STAGING_PATH).resolve()
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(selected.path, destination)
        self.context.log.info(
            f"Prepared GitHub Pages artifact '{artifact}' at {destination}"
        )
        return GitHubPagesResult(artifact=artifact, path=destination)

    def _select_artifacts(self, files: tuple[str, ...]) -> list[Artifact]:
        available = tuple(self.context.artifacts)
        if not files:
            selected = []
            for artifact in available:
                if artifact.path.is_dir():
                    self.context.log.info(
                        f"Skipping directory artifact '{artifact.name}'; "
                        "release assets must be files"
                    )
                else:
                    selected.append(artifact)
            return selected

        selected = []
        for file_ref in files:
            artifact = next((item for item in available if item.matches(file_ref)), None)
            if artifact is not None:
                selected.append(artifact)
                continue
            path = (self.context.workspace / file_ref).resolve()
            if not path.exists():
                raise TaskFailure(
                    f"Artifact '{file_ref}' not found in context or workspace"
                )
            selected.append(Artifact.from_path(path))
        for artifact in selected:
            if not artifact.path.is_file():
                raise TaskFailure(
                    f"Release asset '{artifact.name}' must be a file; "
                    "archive directories before releasing them"
                )
        return selected

    def _log_dry_run(
        self,
        repository: str,
        tag: str,
        title: str | None,
        artifacts: list[Artifact],
    ) -> None:
        self.context.log.info("[github/release] Simulated release (dry run)")
        self.context.log.info(f"[github/release] Target repository: {repository}")
        self.context.log.info(f"[github/release] Tag: {tag}")
        self.context.log.info(f"[github/release] Title: {title or tag}")
        self.context.log.info(
            f"[github/release] Artifacts to attach ({len(artifacts)}):"
        )
        for artifact in artifacts:
            self.context.log.info(f"  - {artifact.name} ({artifact.path})")

    @staticmethod
    def _api_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "OmniShip-v0.1",
        }

    @classmethod
    def _existing_release(
        cls,
        token: str,
        repository: str,
        tag: str,
    ) -> dict | None:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}/releases/tags/"
            f"{urllib.parse.quote(tag, safe='')}",
            headers=cls._api_headers(token),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def _upload_artifacts(
        self,
        token: str,
        release: dict,
        artifacts: list[Artifact],
    ) -> None:
        upload_url = release.get("upload_url", "")
        if not upload_url or "{" not in upload_url:
            return
        base_upload_url = upload_url.split("{")[0]
        existing = {
            asset.get("name"): asset.get("digest") for asset in release.get("assets", [])
        }
        for artifact in artifacts:
            content = artifact.path.read_bytes()
            if artifact.name in existing:
                digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
                if existing[artifact.name] != digest:
                    reason = (
                        "different content"
                        if existing[artifact.name]
                        else "content that cannot be verified"
                    )
                    raise TaskFailure(
                        f"Release asset {artifact.name} already exists with {reason}"
                    )
                self.context.log.info(f"Release asset {artifact.name} already uploaded")
                continue
            request = urllib.request.Request(
                f"{base_upload_url}?name={urllib.parse.quote(artifact.name, safe='')}",
                data=content,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "User-Agent": "OmniShip-v0.1",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30):
                pass
