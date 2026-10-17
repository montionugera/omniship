"""Portable on-disk bundles used to hand artifacts between CI jobs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
from pathlib import Path
from typing import Protocol

from omniship.core.artifact import (
    Artifact,
    ArtifactDigest,
    ArtifactProvenance,
    ArtifactSet,
)
from omniship.core.execution import Architecture, ExecutionHost, OperatingSystem
from omniship.core.stage import Stage

MANIFEST_NAME = "manifest.json"


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _update_file_digest(digest: _Digest, path: Path) -> None:
    with path.open("rb") as content:
        while chunk := content.read(1024 * 1024):
            digest.update(chunk)


def _digest_path(path: Path) -> ArtifactDigest:
    digest = hashlib.sha256()
    if path.is_symlink():
        raise ValueError(f"Artifact cannot be a symlink: {path}")
    if path.is_file():
        _update_file_digest(digest, path)
    elif path.is_dir():
        for candidate in sorted(path.rglob("*")):
            if candidate.is_symlink():
                raise ValueError(f"Artifact directory contains a symlink: {candidate}")
            relative = candidate.relative_to(path).as_posix().encode()
            digest.update(b"D" if candidate.is_dir() else b"F")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            if candidate.is_file():
                _update_file_digest(digest, candidate)
    else:
        raise ValueError(f"Artifact is not a file or directory: {path}")
    return ArtifactDigest("sha256", digest.hexdigest())


def _executables(path: Path) -> list[str]:
    """Return item-relative paths of executable files.

    CI artifact transfers drop file modes, so bundles record them explicitly.
    """

    if path.is_file():
        return ["."] if path.stat().st_mode & 0o111 else []
    return [
        candidate.relative_to(path).as_posix()
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file() and candidate.stat().st_mode & 0o111
    ]


def _restore_executables(entry: dict, stored: Path, path: str) -> None:
    executables = entry.get("executables", [])
    if not isinstance(executables, list) or any(
        not isinstance(item, str) or not item for item in executables
    ):
        raise ValueError(f"Invalid artifact executables: {path}")
    for item in executables:
        target = stored / item
        resolved = target.resolve()
        if (
            not resolved.is_relative_to(stored)
            or target.is_symlink()
            or not resolved.is_file()
        ):
            raise ValueError(f"Invalid artifact executable path: {path}")
        resolved.chmod(resolved.stat().st_mode | 0o111)


def _provenance_document(
    provenance: ArtifactProvenance | None,
) -> dict[str, object] | None:
    if provenance is None:
        return None
    document: dict[str, object] = {}
    if provenance.stage is not None:
        document["stage"] = provenance.stage.value
    if provenance.task is not None:
        document["task"] = provenance.task
    if provenance.revision is not None:
        document["revision"] = provenance.revision
    if provenance.host is not None:
        document["host"] = {
            "os": provenance.host.os.value,
            "architecture": provenance.host.architecture.value,
        }
    return document


def _load_provenance(document: object) -> ArtifactProvenance | None:
    if document is None:
        return None
    if not isinstance(document, dict):
        raise ValueError("Artifact provenance must be an object")
    host_document = document.get("host")
    host = None
    if host_document is not None:
        if not isinstance(host_document, dict):
            raise ValueError("Artifact provenance host must be an object")
        try:
            host = ExecutionHost(
                OperatingSystem(host_document["os"]),
                Architecture(host_document["architecture"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Artifact provenance host is invalid") from exc
    try:
        stage = Stage(document["stage"]) if "stage" in document else None
    except (ValueError, TypeError) as exc:
        raise ValueError("Artifact provenance stage is invalid") from exc
    task = document.get("task")
    revision = document.get("revision")
    if task is not None and not isinstance(task, str):
        raise ValueError("Artifact provenance task is invalid")
    if revision is not None and not isinstance(revision, str):
        raise ValueError("Artifact provenance revision is invalid")
    return ArtifactProvenance(
        stage=stage,
        task=task,
        revision=revision,
        host=host,
    )


def _load_artifact_entry(
    entry: object,
    manifest_path: Path,
) -> tuple[str, str, dict[str, str]]:
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid artifact entry in manifest: {manifest_path}")
    path = entry.get("path")
    name = entry.get("name")
    metadata = entry.get("metadata", {})
    if not isinstance(path, str) or not path:
        raise ValueError(f"Invalid artifact manifest path: {manifest_path}")
    if not isinstance(name, str) or not name:
        raise ValueError(f"Invalid artifact manifest name: {manifest_path}")
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise ValueError(f"Invalid artifact manifest metadata: {manifest_path}")
    return path, name, metadata


def export_artifacts(artifacts: ArtifactSet, destination: str | Path) -> Path:
    """Copy artifacts into ``destination`` and write a versioned manifest.

    Directory artifacts containing symlinks are rejected so a bundle cannot
    escape its declared tree when transferred or restored on another machine.
    """

    bundle = Path(destination).resolve()
    items = bundle / "items"
    items.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []

    for index, artifact in enumerate(artifacts):
        if not artifact.path.exists():
            raise ValueError(f"Artifact does not exist: {artifact.path}")
        stored = Path("items") / f"{index:04d}" / artifact.path.name
        target = bundle / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if artifact.path.is_dir():
            symlink = next(
                (
                    candidate
                    for candidate in artifact.path.rglob("*")
                    if candidate.is_symlink()
                ),
                None,
            )
            if symlink is not None:
                raise ValueError(f"Artifact directory contains a symlink: {symlink}")
            shutil.copytree(artifact.path, target)
        elif artifact.path.is_file():
            shutil.copy2(artifact.path, target)
        else:
            raise ValueError(f"Artifact is not a file or directory: {artifact.path}")
        digest = _digest_path(target)
        manifest.append(
            {
                "name": artifact.name,
                "path": stored.as_posix(),
                "metadata": dict(artifact.metadata),
                "digest": {
                    "algorithm": digest.algorithm,
                    "value": digest.value,
                },
                "provenance": _provenance_document(artifact.provenance),
                "executables": _executables(target),
            }
        )

    manifest_path = bundle / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps({"version": 2, "artifacts": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _validate_revisions(
    artifacts: ArtifactSet,
    *,
    expected_revision: str | None,
) -> None:
    revisions = {
        artifact.provenance.revision if artifact.provenance is not None else None
        for artifact in artifacts
    }
    if len(revisions) > 1:
        raise ValueError("Artifact bundles have mixed producing revisions")
    if revisions and expected_revision is not None and revisions != {expected_revision}:
        raise ValueError(
            f"Artifacts were not produced by expected revision '{expected_revision}'"
        )


def import_artifacts(
    source: str | Path,
    *,
    expected_revision: str | None = None,
) -> ArtifactSet:
    """Load one artifact bundle, validating its version and stored paths."""

    bundle = Path(source).resolve()
    manifest_path = bundle / MANIFEST_NAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"Unsupported artifact manifest: {manifest_path}")
    version = document.get("version")
    if version not in {1, 2} or not isinstance(document.get("artifacts"), list):
        raise ValueError(f"Unsupported artifact manifest: {manifest_path}")

    artifacts = ArtifactSet()
    for entry in document["artifacts"]:
        path, name, metadata = _load_artifact_entry(entry, manifest_path)
        stored_path = bundle / path
        stored = stored_path.resolve()
        items = (bundle / "items").resolve()
        if (
            not stored.is_relative_to(items)
            or stored == items
            or not stored.exists()
            or stored_path.is_symlink()
        ):
            raise ValueError(f"Invalid artifact path in manifest: {path}")
        digest = None
        if version == 2:
            digest_document = entry.get("digest")
            if not isinstance(digest_document, dict):
                raise ValueError(f"Artifact digest is missing: {path}")
            if digest_document.get("algorithm") != "sha256" or not isinstance(
                digest_document.get("value"), str
            ):
                raise ValueError(f"Artifact digest is invalid: {path}")
            digest = ArtifactDigest("sha256", digest_document["value"])
            actual = _digest_path(stored)
            if not hmac.compare_digest(digest.value, actual.value):
                raise ValueError(f"Artifact digest mismatch: {path}")
        else:
            # Version 1 has no digest; still reject symlinks inside directories.
            _digest_path(stored)
        _restore_executables(entry, stored, path)
        artifacts.add(
            Artifact.from_path(
                stored,
                name=name,
                metadata=metadata,
                digest=digest,
                provenance=_load_provenance(entry.get("provenance")),
            )
        )
    _validate_revisions(artifacts, expected_revision=expected_revision)
    return artifacts


def import_artifact_bundles(
    source: str | Path,
    *,
    expected_revision: str | None = None,
) -> ArtifactSet:
    """Merge every artifact bundle found recursively below ``source``.

    The search stops at each bundle root, so ``manifest.json`` files inside
    artifact contents (web app or extension manifests) are never parsed.
    """

    root = Path(source).resolve()
    manifests = []
    for directory, subdirectories, files in os.walk(root):
        if MANIFEST_NAME in files:
            manifests.append(Path(directory) / MANIFEST_NAME)
            subdirectories.clear()
        else:
            subdirectories.sort()
    manifests.sort()
    if not manifests:
        raise ValueError(f"No artifact manifests found under: {root}")

    artifacts = ArtifactSet()
    for manifest in manifests:
        for artifact in import_artifacts(
            manifest.parent,
            expected_revision=expected_revision,
        ):
            artifacts.add(artifact)
    _validate_revisions(artifacts, expected_revision=expected_revision)
    return artifacts
