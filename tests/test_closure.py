from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from goalanchor import (
    ArtifactKind,
    ArtifactManifest,
    ClosureLevel,
    ClosureRecord,
    ProvenanceChain,
    ProvenanceKind,
    ProvenanceLink,
    ProvenanceNode,
    check_artifact_manifest,
    check_closure,
    check_provenance,
    compute_sha256,
    highest_supported_level,
    verify_artifact_file,
)
from goalanchor.compat import main
from goalanchor.core import ContractValidationError


def make_manifest(**overrides) -> ArtifactManifest:
    values = {
        "manifest_id": "M1",
        "artifact_id": "app-fw-001",
        "source_revision": "commit-abc",
        "change_summary": "修复恢复路径竞争",
        "build_command": "build firmware",
        "built_at": "2026-09-04T12:00:00Z",
        "artifact_kind": ArtifactKind.FW,
        "artifact_path": "build/app.fw",
        "artifact_sha256": "a" * 64,
        "test_object": "device-01",
        "build_success": True,
        "hash_verified": True,
    }
    values.update(overrides)
    return ArtifactManifest(**values)


def make_chain(manifest: ArtifactManifest | None = None, **overrides) -> ProvenanceChain:
    manifest = manifest or make_manifest()
    nodes = (
        ProvenanceNode("source", ProvenanceKind.SOURCE, "src-tree", "1" * 64, manifest.source_revision),
        ProvenanceNode("object", ProvenanceKind.OBJECT, "app.o", "2" * 64, manifest.source_revision),
        ProvenanceNode("elf", ProvenanceKind.ELF, "app.elf", "3" * 64, manifest.source_revision),
        ProvenanceNode(
            "bin",
            ProvenanceKind.BIN_FW,
            manifest.artifact_id,
            manifest.artifact_sha256,
            manifest.source_revision,
        ),
        ProvenanceNode("flashed", ProvenanceKind.FLASHED_ARTIFACT, "flash-001", "5" * 64),
        ProvenanceNode("dut", ProvenanceKind.DEVICE_UNDER_TEST, "device-01"),
    )
    links = tuple(
        ProvenanceLink(nodes[index].node_id, nodes[index + 1].node_id)
        for index in range(len(nodes) - 1)
    )
    values = {"nodes": nodes, "links": links}
    values.update(overrides)
    return ProvenanceChain(**values)


def make_closure(**overrides) -> ClosureRecord:
    values = {
        "requested_level": ClosureLevel.PRODUCT_CLOSURE,
        "evidence_refs": ("E-root", "E-system"),
        "containment_scope": "all supported devices",
        "root_cause_proven": True,
        "fix_change_ref": "commit-fix-001",
        "build_success": True,
        "artifact_verified": True,
        "system_validation_passed": True,
        "acceptance_complete": True,
        "artifact_manifest": make_manifest(),
        "provenance": make_chain(),
        "test_object": "device-01",
    }
    values.update(overrides)
    return ClosureRecord(**values)


def test_manifest_round_trip_and_hash_shape() -> None:
    manifest = make_manifest()
    assert ArtifactManifest.from_dict(manifest.to_dict()) == manifest
    assert len(manifest.artifact_sha256) == 64


def test_manifest_rejects_invalid_hash_and_timestamp() -> None:
    with pytest.raises(ContractValidationError, match="SHA-256"):
        make_manifest(artifact_sha256="not-a-hash")
    with pytest.raises(ContractValidationError, match="ISO-8601"):
        make_manifest(built_at="tomorrow")


def test_artifact_gate_distinguishes_build_and_hash_verification() -> None:
    assert check_artifact_manifest(make_manifest()).passed
    build_only = check_artifact_manifest(make_manifest(hash_verified=False))
    assert not build_only.passed
    assert build_only.findings[0].code == "ARTIFACT_HASH_NOT_VERIFIED"
    assert build_only.recommended_level == ClosureLevel.BUILD_SUCCESS.value


def test_verify_artifact_file_checks_actual_bytes(tmp_path: Path) -> None:
    artifact = tmp_path / "app.fw"
    artifact.write_bytes(b"fresh artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest = make_manifest(
        artifact_path=str(artifact), artifact_sha256=digest, hash_verified=False
    )
    assert compute_sha256(artifact) == digest
    assert verify_artifact_file(manifest).passed
    assert not verify_artifact_file(
        replace(manifest, artifact_sha256="b" * 64)
    ).passed
    assert verify_artifact_file(replace(manifest, artifact_path=str(tmp_path / "missing"))).findings[0].code == (
        "ARTIFACT_FILE_MISSING"
    )


def test_provenance_round_trip_has_exact_six_nodes() -> None:
    chain = make_chain()
    assert ProvenanceChain.from_dict(chain.to_dict()) == chain
    assert [node.kind.value for node in chain.nodes] == [
        "Source",
        "Object",
        "ELF",
        "BIN/FW",
        "Flashed Artifact",
        "Device Under Test",
    ]


def test_provenance_matches_manifest() -> None:
    manifest = make_manifest()
    assert check_provenance(make_chain(manifest), manifest).passed
    mismatch = replace(manifest, source_revision="commit-other")
    result = check_provenance(make_chain(manifest), mismatch)
    assert not result.passed
    assert result.findings[0].code == "SOURCE_REVISION_MISMATCH"


def test_provenance_rejects_missing_order_and_unavailable_nodes() -> None:
    chain = make_chain()
    missing = ProvenanceChain(nodes=chain.nodes[:-1], links=chain.links[:-1])
    result = check_provenance(missing)
    assert not result.passed
    assert result.findings[0].code == "PROVENANCE_CHAIN_INCOMPLETE"

    wrong_order = ProvenanceChain(
        nodes=(chain.nodes[1], chain.nodes[0], *chain.nodes[2:]), links=chain.links
    )
    assert check_provenance(wrong_order).findings[0].code == "PROVENANCE_ORDER_INVALID"

    unavailable = ProvenanceNode(
        "source", ProvenanceKind.SOURCE, "src-tree", "1" * 64, "commit-abc", available=False
    )
    unavailable_chain = ProvenanceChain(nodes=(unavailable, *chain.nodes[1:]), links=chain.links)
    assert check_provenance(unavailable_chain).findings[0].code == "PROVENANCE_NODE_UNAVAILABLE"


def test_provenance_rejects_non_adjacent_links_and_artifact_mismatch() -> None:
    chain = make_chain()
    bad_links = (*chain.links[:-1], ProvenanceLink("source", "dut"))
    result = check_provenance(ProvenanceChain(chain.nodes, bad_links))
    assert not result.passed
    assert result.findings[0].code == "PROVENANCE_LINK_INVALID"

    bad_manifest = make_manifest(artifact_id="different-artifact")
    result = check_provenance(chain, bad_manifest)
    assert not result.passed
    assert result.findings[0].code == "ARTIFACT_PROVENANCE_MISMATCH"


def test_closure_levels_keep_intermediate_success_separate() -> None:
    assert check_closure(
        ClosureRecord(ClosureLevel.SYMPTOM_SUPPRESSED, evidence_refs=("E1",))
    ).passed
    assert check_closure(
        ClosureRecord(
            ClosureLevel.CONTAINMENT,
            evidence_refs=("E1",),
            containment_scope="Monitor disabled",
        )
    ).passed
    local = check_closure(
        ClosureRecord(
            ClosureLevel.LOCAL_FIX,
            evidence_refs=("E1",),
            fix_change_ref="commit-local",
        )
    )
    assert local.passed
    root = check_closure(
        ClosureRecord(
            ClosureLevel.ROOT_CAUSE_IDENTIFIED,
            evidence_refs=("E-root",),
            root_cause_proven=True,
        )
    )
    assert root.passed
    root_fix = check_closure(
        ClosureRecord(
            ClosureLevel.ROOT_FIX,
            evidence_refs=("E-root",),
            root_cause_proven=True,
            fix_change_ref="commit-fix",
        )
    )
    assert root_fix.passed


def test_build_artifact_and_system_levels_have_separate_prerequisites() -> None:
    manifest = make_manifest()
    build = ClosureRecord(
        ClosureLevel.BUILD_SUCCESS, build_success=True, artifact_manifest=manifest
    )
    assert check_closure(build).passed

    artifact = ClosureRecord(
        ClosureLevel.ARTIFACT_VERIFIED,
        build_success=True,
        artifact_verified=True,
        artifact_manifest=manifest,
    )
    assert check_closure(artifact).passed

    system = ClosureRecord(
        ClosureLevel.SYSTEM_VALIDATION,
        build_success=True,
        artifact_verified=True,
        system_validation_passed=True,
        artifact_manifest=manifest,
        test_object="device-01",
    )
    assert check_closure(system).passed


def test_product_closure_requires_root_fix_system_validation_acceptance_and_chain() -> None:
    assert check_closure(make_closure()).passed
    missing_chain = check_closure(make_closure(provenance=None))
    assert not missing_chain.passed
    assert missing_chain.recommended_level == ClosureLevel.SYSTEM_VALIDATION.value
    missing_acceptance = check_closure(make_closure(acceptance_complete=False))
    assert not missing_acceptance.passed
    assert highest_supported_level(make_closure(acceptance_complete=False)) == ClosureLevel.SYSTEM_VALIDATION


def test_closure_downgrades_build_success_instead_of_claiming_product_completion() -> None:
    result = check_closure(
        ClosureRecord(
            ClosureLevel.PRODUCT_CLOSURE,
            build_success=True,
            artifact_manifest=make_manifest(),
        )
    )
    assert not result.passed
    assert result.recommended_level == ClosureLevel.BUILD_SUCCESS.value


def test_closure_cli_reports_provenance_and_product_result(tmp_path: Path, capsys) -> None:
    manifest = make_manifest()
    provenance_path = tmp_path / "provenance.json"
    provenance_path.write_text(
        json.dumps({"provenance": make_chain(manifest).to_dict()}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert main(["gate", "--phase", "provenance", "--record", str(provenance_path)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True

    closure_path = tmp_path / "closure.json"
    closure_path.write_text(
        json.dumps({"closure": make_closure().to_dict()}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert main(["gate", "--phase", "closure", "--record", str(closure_path)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
