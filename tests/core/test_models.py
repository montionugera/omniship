import json
from pathlib import Path

import pytest

from omniship.core.artifact import (
    Artifact,
    ArtifactDigest,
    ArtifactProvenance,
    ArtifactSet,
)
from omniship.core.artifact_bundle import (
    export_artifacts,
    import_artifact_bundles,
    import_artifacts,
)
from omniship.core.context import ExecutionContext
from omniship.core.execution import Architecture, ExecutionHost, OperatingSystem
from omniship.core.result import NodeResult, NodeStatus
from omniship.core.stage import Stage


def test_stage_enum():
    assert Stage.CHECK == "check"
    assert Stage.BUILD == "build"
    assert Stage.SHIP == "ship"


def test_artifact_and_set(tmp_path: Path):
    f1 = tmp_path / "bin1"
    f1.write_text("dummy1")
    f2 = tmp_path / "bin2"
    f2.write_text("dummy2")

    a1 = Artifact.from_path(f1, name="dist/bin1", metadata={"arch": "x86_64"})
    a2 = Artifact.from_path(f2, name="dist/bin2")

    aset = ArtifactSet()
    aset.add(a1)
    aset.add(a2)

    assert len(aset) == 2
    assert "dist/bin1" in aset
    assert aset.get("dist/bin1") == a1
    assert aset.get("dist/bin2") == a2
    assert len(aset.to_list()) == 2


def test_artifact_set_matches_path_references_by_whole_segments(
    tmp_path: Path,
) -> None:
    (tmp_path / "dist").mkdir()
    other = tmp_path / "dist" / "myapp.whl"
    other.write_text("other", encoding="utf-8")
    wanted = tmp_path / "dist" / "app.whl"
    wanted.write_text("wanted", encoding="utf-8")
    artifacts = ArtifactSet(
        [Artifact.from_path(other, name="other"), Artifact.from_path(wanted, name="x")]
    )

    assert artifacts.get("app.whl").path == wanted
    assert artifacts.get("dist/app.whl").path == wanted
    assert artifacts.get("p.whl") is None


def test_artifact_digest_rejects_untrusted_manifest_values() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        ArtifactDigest("sha256", "not-a-digest")
    with pytest.raises(ValueError, match="algorithm"):
        ArtifactDigest("md5", "0" * 32)


def test_artifact_bundle_round_trips_files_and_metadata(tmp_path: Path) -> None:
    wheel = tmp_path / "dist" / "demo.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel")
    artifacts = ArtifactSet()
    provenance = ArtifactProvenance(
        stage=Stage.BUILD,
        task="wheel",
        revision="abc123",
        host=ExecutionHost(OperatingSystem.LINUX, Architecture.X86_64),
    )
    artifacts.add(
        Artifact.from_path(
            wheel,
            name="python-wheel",
            metadata={"kind": "wheel"},
            provenance=provenance,
        )
    )
    bundle = tmp_path / "handoff"

    export_artifacts(artifacts, bundle)
    wheel.unlink()
    restored = import_artifacts(bundle)

    artifact = restored.get("python-wheel")
    assert artifact is not None
    assert artifact.path.read_bytes() == b"wheel"
    assert artifact.metadata == {"kind": "wheel"}
    assert artifact.digest is not None
    assert artifact.digest.algorithm == "sha256"
    assert artifact.provenance == provenance

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert manifest["artifacts"][0]["digest"] == {
        "algorithm": "sha256",
        "value": artifact.digest.value,
    }


def test_artifact_bundle_rejects_tampered_content(tmp_path: Path) -> None:
    source = tmp_path / "binary"
    source.write_bytes(b"trusted")
    artifacts = ArtifactSet()
    artifacts.add(Artifact.from_path(source))
    bundle = tmp_path / "handoff"
    export_artifacts(artifacts, bundle)
    stored = next((bundle / "items").rglob("binary"))
    stored.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="digest mismatch"):
        import_artifacts(bundle)


@pytest.mark.parametrize(
    "entry_update",
    [
        {"path": 42},
        {"name": 42},
        {"metadata": []},
        {"metadata": {"kind": 42}},
        {"provenance": {"task": 42}},
    ],
)
def test_artifact_bundle_rejects_malformed_manifest_fields(
    tmp_path: Path,
    entry_update: dict[str, object],
) -> None:
    source = tmp_path / "binary"
    source.write_bytes(b"trusted")
    bundle = tmp_path / "handoff"
    artifacts = ArtifactSet()
    artifacts.add(Artifact.from_path(source))
    export_artifacts(artifacts, bundle)
    manifest_path = bundle / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["artifacts"][0].update(entry_update)
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact manifest|provenance"):
        import_artifacts(bundle)


def test_artifact_bundle_round_trips_directories(tmp_path: Path) -> None:
    site = tmp_path / "build"
    (site / "assets").mkdir(parents=True)
    (site / "index.html").write_text("<h1>OmniShip</h1>", encoding="utf-8")
    (site / "assets" / "app.css").write_text("body {}", encoding="utf-8")
    artifacts = ArtifactSet()
    artifacts.add(Artifact.from_path(site, name="docs-site"))
    bundle = tmp_path / "handoff"

    export_artifacts(artifacts, bundle)
    export_artifacts(artifacts, bundle)
    restored = import_artifacts(bundle)

    artifact = restored.get("docs-site")
    assert artifact is not None
    assert artifact.path.is_dir()
    assert (artifact.path / "index.html").read_text(encoding="utf-8") == (
        "<h1>OmniShip</h1>"
    )
    assert (artifact.path / "assets" / "app.css").read_text(encoding="utf-8") == (
        "body {}"
    )


def test_artifact_bundle_root_merges_parallel_build_outputs(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    for name in ("linux.whl", "macos.whl"):
        source = tmp_path / name
        source.write_text(name, encoding="utf-8")
        artifacts = ArtifactSet()
        artifacts.add(Artifact.from_path(source))
        export_artifacts(artifacts, imports / f"build-{name}")

    restored = import_artifact_bundles(imports)

    assert {artifact.name for artifact in restored} == {"linux.whl", "macos.whl"}


def test_artifact_bundle_import_rejects_mixed_or_unexpected_revisions(
    tmp_path: Path,
) -> None:
    imports = tmp_path / "imports"
    for name, revision in (("linux.whl", "abc123"), ("macos.whl", "other")):
        source = tmp_path / name
        source.write_text(name, encoding="utf-8")
        artifacts = ArtifactSet()
        artifacts.add(
            Artifact.from_path(
                source,
                provenance=ArtifactProvenance(revision=revision),
            )
        )
        export_artifacts(artifacts, imports / name)

    with pytest.raises(ValueError, match="mixed producing revisions"):
        import_artifact_bundles(imports)
    with pytest.raises(ValueError, match="expected revision 'expected'"):
        import_artifacts(imports / "linux.whl", expected_revision="expected")


def test_artifact_bundle_root_ignores_manifest_files_inside_artifacts(
    tmp_path: Path,
) -> None:
    site = tmp_path / "dist"
    site.mkdir()
    (site / "manifest.json").write_text('{"name": "PWA"}', encoding="utf-8")
    (site / "index.html").write_text("<h1>site</h1>", encoding="utf-8")
    artifacts = ArtifactSet()
    artifacts.add(Artifact.from_path(site))
    imports = tmp_path / "imports"
    export_artifacts(artifacts, imports / "omniship-build-site")

    restored = import_artifact_bundles(imports)

    artifact = restored.get("dist")
    assert artifact is not None
    assert (artifact.path / "manifest.json").is_file()


def test_artifact_bundle_restores_executable_files(tmp_path: Path) -> None:
    binary = tmp_path / "tool"
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    package = tmp_path / "package"
    (package / "bin").mkdir(parents=True)
    (package / "bin" / "tool").write_bytes(b"#!/bin/sh\n")
    (package / "bin" / "tool").chmod(0o755)
    (package / "README").write_text("docs", encoding="utf-8")
    artifacts = ArtifactSet()
    artifacts.add(Artifact.from_path(binary))
    artifacts.add(Artifact.from_path(package))
    bundle = tmp_path / "handoff"
    export_artifacts(artifacts, bundle)
    # upload-artifact does not preserve file modes.
    for stored in (bundle / "items").rglob("*"):
        if stored.is_file():
            stored.chmod(0o644)

    restored = import_artifacts(bundle)

    assert restored.get("tool").path.stat().st_mode & 0o111
    restored_package = restored.get("package").path
    assert (restored_package / "bin" / "tool").stat().st_mode & 0o111
    assert not (restored_package / "README").stat().st_mode & 0o111


def test_artifact_bundle_rejects_escaping_executable_entries(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "README").write_text("docs", encoding="utf-8")
    artifacts = ArtifactSet()
    artifacts.add(Artifact.from_path(package))
    bundle = tmp_path / "handoff"
    export_artifacts(artifacts, bundle)
    manifest_path = bundle / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["artifacts"][0]["executables"] = ["../../manifest.json"]
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="executable"):
        import_artifacts(bundle)


def test_artifact_bundle_v1_rejects_symlinks_in_directories(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "README").write_text("docs", encoding="utf-8")
    artifacts = ArtifactSet()
    artifacts.add(Artifact.from_path(package))
    bundle = tmp_path / "handoff"
    export_artifacts(artifacts, bundle)
    stored = next((bundle / "items").glob("*/package"))
    (stored / "secret").symlink_to(tmp_path / "outside")
    manifest_path = bundle / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["version"] = 1
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="symlink"):
        import_artifacts(bundle)


def test_node_result():
    res_success = NodeResult(status=NodeStatus.SUCCESS, duration=1.5)
    assert res_success.is_success
    assert not res_success.is_failed
    assert not res_success.is_skipped

    res_fail = NodeResult(status=NodeStatus.FAILED, error_message="boom")
    assert res_fail.is_failed

    res_skip = NodeResult(status=NodeStatus.SKIPPED, error_message="dependency failed")
    assert res_skip.is_skipped


def test_execution_context(tmp_path: Path):
    ctx = ExecutionContext(workspace_root=tmp_path, stage=Stage.CHECK)
    f = tmp_path / "file"
    f.write_text("test")
    art = Artifact.from_path(f)
    res = NodeResult(status=NodeStatus.SUCCESS, artifacts=(art,))
    ctx.record_result("node1", res)

    assert ctx.results["node1"].status == res.status
    recorded_artifact = ctx.get_artifact(art.name)
    assert recorded_artifact is not None
    assert recorded_artifact.path == art.path
    assert recorded_artifact.provenance is not None


def test_execution_context_adds_missing_artifact_provenance(tmp_path: Path) -> None:
    host = ExecutionHost(OperatingSystem.MACOS, Architecture.ARM64)
    context = ExecutionContext(
        workspace_root=tmp_path,
        stage=Stage.BUILD,
        host=host,
        revision="abc123",
    )
    binary = tmp_path / "tool"
    binary.write_bytes(b"binary")

    context.record_result(
        "package",
        NodeResult(
            status=NodeStatus.SUCCESS,
            artifacts=(Artifact.from_path(binary),),
        ),
    )

    artifact = context.get_artifact("tool")
    assert artifact is not None
    assert artifact.provenance == ArtifactProvenance(
        stage=Stage.BUILD,
        task="package",
        revision="abc123",
        host=host,
    )


def test_artifact_set_requires_an_exact_named_set(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.touch()
    second.touch()
    artifacts = ArtifactSet()
    artifacts.add(Artifact.from_path(first, name="linux"))
    artifacts.add(Artifact.from_path(second, name="macos"))

    selected = artifacts.require(["linux", "macos"], exact=True)

    assert tuple(artifact.name for artifact in selected) == ("linux", "macos")
    with pytest.raises(ValueError, match="Missing required artifacts: windows"):
        artifacts.require(["linux", "windows"])
    with pytest.raises(ValueError, match="Unexpected artifacts: macos"):
        artifacts.require(["linux"], exact=True)

    duplicate = tmp_path / "duplicate"
    duplicate.touch()
    artifacts.add(Artifact.from_path(duplicate, name="linux"))
    with pytest.raises(ValueError, match="Duplicate artifacts: linux"):
        artifacts.require(["linux", "macos"], exact=True)


def test_artifact_set_requires_consistent_producing_revision(tmp_path: Path) -> None:
    artifacts = ArtifactSet()
    for name, revision in (("linux", "abc123"), ("macos", "other")):
        path = tmp_path / name
        path.touch()
        artifacts.add(
            Artifact.from_path(
                path,
                name=name,
                provenance=ArtifactProvenance(revision=revision),
            )
        )

    with pytest.raises(ValueError, match="mixed producing revisions"):
        artifacts.require(["linux", "macos"], consistent_revision=True)
    with pytest.raises(ValueError, match="expected revision 'abc123'"):
        artifacts.require(["macos"], revision="abc123")
