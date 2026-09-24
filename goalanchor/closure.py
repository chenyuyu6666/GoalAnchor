"""Delivery closure and source-to-device provenance checks for stage 4."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .core import ContractValidationError
from .gates import GateFinding, GateId, GateResult

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractValidationError(f"{field_name} must be a string or null")
    normalized = value.strip()
    return normalized or None


def _strings(value: Any, field_name: str, *, required: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{field_name} must be an array of strings")
    result = tuple(_text(item, f"{field_name}[]") for item in value)
    if required and not result:
        raise ContractValidationError(f"{field_name} must contain at least one item")
    return result


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be a boolean")
    return value


def _enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise ContractValidationError(f"{field_name} must be a string")
    for member in enum_type:
        if value == member.value or value == member.name:
            return member
    allowed = ", ".join(member.value for member in enum_type)
    raise ContractValidationError(f"{field_name} must be one of: {allowed}")


def _mapping(value: Any, type_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{type_name} must be a JSON object")
    return value


def _reject_unknown(payload: Mapping[str, Any], allowed: set[str], type_name: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ContractValidationError(
            f"{type_name} contains unknown field(s): {', '.join(unknown)}"
        )


def _sha256(value: Any, field_name: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip()):
        raise ContractValidationError(f"{field_name} must be a 64-character SHA-256 hex string")
    return value.strip().lower()


def _timestamp(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name)
    candidate = normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ContractValidationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    return normalized


class ClosureLevel(str, Enum):
    SYMPTOM_SUPPRESSED = "Symptom Suppressed"
    CONTAINMENT = "Containment"
    LOCAL_FIX = "Local Fix"
    ROOT_CAUSE_IDENTIFIED = "Root Cause Identified"
    ROOT_FIX = "Root Fix"
    BUILD_SUCCESS = "Build Success"
    ARTIFACT_VERIFIED = "Artifact Verified"
    SYSTEM_VALIDATION = "System Validation"
    PRODUCT_CLOSURE = "Product Closure"


class ArtifactKind(str, Enum):
    OBJECT = "Object"
    ELF = "ELF"
    BIN = "BIN"
    FW = "FW"


class ProvenanceKind(str, Enum):
    SOURCE = "Source"
    OBJECT = "Object"
    ELF = "ELF"
    BIN_FW = "BIN/FW"
    FLASHED_ARTIFACT = "Flashed Artifact"
    DEVICE_UNDER_TEST = "Device Under Test"


@dataclass(frozen=True)
class ArtifactManifest:
    """Identity and build evidence for one generated artifact."""

    manifest_id: str
    artifact_id: str
    source_revision: str
    change_summary: str
    build_command: str
    built_at: str
    artifact_kind: ArtifactKind
    artifact_path: str
    artifact_sha256: str
    test_object: str | None = None
    build_success: bool = False
    hash_verified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_id", _text(self.manifest_id, "manifest_id"))
        object.__setattr__(self, "artifact_id", _text(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "source_revision", _text(self.source_revision, "source_revision"))
        object.__setattr__(self, "change_summary", _text(self.change_summary, "change_summary"))
        object.__setattr__(self, "build_command", _text(self.build_command, "build_command"))
        object.__setattr__(self, "built_at", _timestamp(self.built_at, "built_at"))
        object.__setattr__(
            self, "artifact_kind", _enum(self.artifact_kind, ArtifactKind, "artifact_kind")
        )
        object.__setattr__(self, "artifact_path", _text(self.artifact_path, "artifact_path"))
        object.__setattr__(
            self, "artifact_sha256", _sha256(self.artifact_sha256, "artifact_sha256")
        )
        if self.test_object is not None:
            object.__setattr__(self, "test_object", _text(self.test_object, "test_object"))
        object.__setattr__(self, "build_success", _bool(self.build_success, "build_success"))
        object.__setattr__(self, "hash_verified", _bool(self.hash_verified, "hash_verified"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "artifact_id": self.artifact_id,
            "source_revision": self.source_revision,
            "change_summary": self.change_summary,
            "build_command": self.build_command,
            "built_at": self.built_at,
            "artifact_kind": self.artifact_kind.value,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "test_object": self.test_object,
            "build_success": self.build_success,
            "hash_verified": self.hash_verified,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ArtifactManifest:
        data = _mapping(payload, "ArtifactManifest")
        allowed = {
            "manifest_id",
            "artifact_id",
            "source_revision",
            "change_summary",
            "build_command",
            "built_at",
            "artifact_kind",
            "artifact_path",
            "artifact_sha256",
            "test_object",
            "build_success",
            "hash_verified",
        }
        _reject_unknown(data, allowed, "ArtifactManifest")
        required = {
            "manifest_id",
            "artifact_id",
            "source_revision",
            "change_summary",
            "build_command",
            "built_at",
            "artifact_kind",
            "artifact_path",
            "artifact_sha256",
        }
        missing = required - set(data)
        if missing:
            raise ContractValidationError(
                f"ArtifactManifest missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            manifest_id=data["manifest_id"],
            artifact_id=data["artifact_id"],
            source_revision=data["source_revision"],
            change_summary=data["change_summary"],
            build_command=data["build_command"],
            built_at=data["built_at"],
            artifact_kind=data["artifact_kind"],
            artifact_path=data["artifact_path"],
            artifact_sha256=data["artifact_sha256"],
            test_object=data.get("test_object"),
            build_success=data.get("build_success", False),
            hash_verified=data.get("hash_verified", False),
        )


@dataclass(frozen=True)
class ProvenanceNode:
    """One node in the source-to-device chain."""

    node_id: str
    kind: ProvenanceKind
    identifier: str
    sha256: str | None = None
    revision: str | None = None
    available: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _text(self.node_id, "node_id"))
        object.__setattr__(self, "kind", _enum(self.kind, ProvenanceKind, "kind"))
        object.__setattr__(self, "identifier", _text(self.identifier, "identifier"))
        required_hash = self.kind is not ProvenanceKind.DEVICE_UNDER_TEST
        object.__setattr__(
            self, "sha256", _sha256(self.sha256, "sha256", required=required_hash)
        )
        if self.revision is not None:
            object.__setattr__(self, "revision", _text(self.revision, "revision"))
        object.__setattr__(self, "available", _bool(self.available, "available"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "identifier": self.identifier,
            "sha256": self.sha256,
            "revision": self.revision,
            "available": self.available,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProvenanceNode:
        data = _mapping(payload, "ProvenanceNode")
        allowed = {"node_id", "kind", "identifier", "sha256", "revision", "available"}
        _reject_unknown(data, allowed, "ProvenanceNode")
        required = {"node_id", "kind", "identifier"}
        missing = required - set(data)
        if missing:
            raise ContractValidationError(
                f"ProvenanceNode missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            node_id=data["node_id"],
            kind=data["kind"],
            identifier=data["identifier"],
            sha256=data.get("sha256"),
            revision=data.get("revision"),
            available=data.get("available", True),
        )


@dataclass(frozen=True)
class ProvenanceLink:
    source_id: str
    target_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _text(self.source_id, "source_id"))
        object.__setattr__(self, "target_id", _text(self.target_id, "target_id"))

    def to_dict(self) -> dict[str, str]:
        return {"source_id": self.source_id, "target_id": self.target_id}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProvenanceLink:
        data = _mapping(payload, "ProvenanceLink")
        _reject_unknown(data, {"source_id", "target_id"}, "ProvenanceLink")
        if {"source_id", "target_id"} - set(data):
            raise ContractValidationError("ProvenanceLink requires source_id and target_id")
        return cls(source_id=data["source_id"], target_id=data["target_id"])


@dataclass(frozen=True)
class ProvenanceChain:
    nodes: tuple[ProvenanceNode, ...]
    links: tuple[ProvenanceLink, ...]

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        links = tuple(self.links)
        if any(not isinstance(item, ProvenanceNode) for item in nodes):
            raise ContractValidationError("nodes must contain ProvenanceNode values")
        if any(not isinstance(item, ProvenanceLink) for item in links):
            raise ContractValidationError("links must contain ProvenanceLink values")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "links", links)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [item.to_dict() for item in self.nodes],
            "links": [item.to_dict() for item in self.links],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProvenanceChain:
        data = _mapping(payload, "ProvenanceChain")
        _reject_unknown(data, {"nodes", "links"}, "ProvenanceChain")
        if "nodes" not in data or "links" not in data:
            raise ContractValidationError("ProvenanceChain requires nodes and links")
        return cls(
            nodes=tuple(ProvenanceNode.from_dict(item) for item in data["nodes"]),
            links=tuple(ProvenanceLink.from_dict(item) for item in data["links"]),
        )


@dataclass(frozen=True)
class ClosureRecord:
    """A requested closure claim and the evidence/prerequisites behind it."""

    requested_level: ClosureLevel
    evidence_refs: tuple[str, ...] = ()
    containment_scope: str | None = None
    root_cause_proven: bool = False
    fix_change_ref: str | None = None
    build_success: bool = False
    artifact_verified: bool = False
    system_validation_passed: bool = False
    acceptance_complete: bool = False
    artifact_manifest: ArtifactManifest | None = None
    provenance: ProvenanceChain | None = None
    test_object: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requested_level", _enum(self.requested_level, ClosureLevel, "requested_level")
        )
        object.__setattr__(self, "evidence_refs", _strings(self.evidence_refs, "evidence_refs"))
        object.__setattr__(
            self, "containment_scope", _optional_text(self.containment_scope, "containment_scope")
        )
        object.__setattr__(
            self, "root_cause_proven", _bool(self.root_cause_proven, "root_cause_proven")
        )
        object.__setattr__(self, "fix_change_ref", _optional_text(self.fix_change_ref, "fix_change_ref"))
        for field_name in (
            "build_success",
            "artifact_verified",
            "system_validation_passed",
            "acceptance_complete",
        ):
            object.__setattr__(self, field_name, _bool(getattr(self, field_name), field_name))
        if self.artifact_manifest is not None and not isinstance(
            self.artifact_manifest, ArtifactManifest
        ):
            raise ContractValidationError("artifact_manifest must be an ArtifactManifest")
        if self.provenance is not None and not isinstance(self.provenance, ProvenanceChain):
            raise ContractValidationError("provenance must be a ProvenanceChain")
        object.__setattr__(self, "test_object", _optional_text(self.test_object, "test_object"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_level": self.requested_level.value,
            "evidence_refs": list(self.evidence_refs),
            "containment_scope": self.containment_scope,
            "root_cause_proven": self.root_cause_proven,
            "fix_change_ref": self.fix_change_ref,
            "build_success": self.build_success,
            "artifact_verified": self.artifact_verified,
            "system_validation_passed": self.system_validation_passed,
            "acceptance_complete": self.acceptance_complete,
            "artifact_manifest": (
                None if self.artifact_manifest is None else self.artifact_manifest.to_dict()
            ),
            "provenance": None if self.provenance is None else self.provenance.to_dict(),
            "test_object": self.test_object,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ClosureRecord:
        data = _mapping(payload, "ClosureRecord")
        allowed = {
            "requested_level",
            "evidence_refs",
            "containment_scope",
            "root_cause_proven",
            "fix_change_ref",
            "build_success",
            "artifact_verified",
            "system_validation_passed",
            "acceptance_complete",
            "artifact_manifest",
            "provenance",
            "test_object",
        }
        _reject_unknown(data, allowed, "ClosureRecord")
        if "requested_level" not in data:
            raise ContractValidationError("ClosureRecord requires requested_level")
        manifest_data = data.get("artifact_manifest")
        provenance_data = data.get("provenance")
        return cls(
            requested_level=data["requested_level"],
            evidence_refs=data.get("evidence_refs", ()),
            containment_scope=data.get("containment_scope"),
            root_cause_proven=data.get("root_cause_proven", False),
            fix_change_ref=data.get("fix_change_ref"),
            build_success=data.get("build_success", False),
            artifact_verified=data.get("artifact_verified", False),
            system_validation_passed=data.get("system_validation_passed", False),
            acceptance_complete=data.get("acceptance_complete", False),
            artifact_manifest=(
                None if manifest_data is None else ArtifactManifest.from_dict(manifest_data)
            ),
            provenance=(
                None if provenance_data is None else ProvenanceChain.from_dict(provenance_data)
            ),
            test_object=data.get("test_object"),
        )


def _failed(
    gate_id: GateId,
    code: str,
    message: str,
    *,
    field: str | None = None,
    recommended_level: ClosureLevel | None = None,
) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        passed=False,
        findings=(GateFinding(code=code, message=message, field=field),),
        recommended_level=(None if recommended_level is None else recommended_level.value),
    )


def _passed(gate_id: GateId) -> GateResult:
    return GateResult(gate_id=gate_id, passed=True)


def compute_sha256(path: str | Path) -> str:
    """Compute a file hash without mutating the file or its directory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_artifact_manifest(manifest: ArtifactManifest | None) -> GateResult:
    """Require a successful build and an explicit artifact hash verification."""

    if manifest is None:
        return _failed(
            GateId.ARTIFACT,
            "ARTIFACT_MANIFEST_REQUIRED",
            "artifact manifest is required",
            field="artifact_manifest",
        )
    if not manifest.build_success:
        return _failed(
            GateId.ARTIFACT,
            "BUILD_NOT_SUCCESS",
            "artifact verification cannot start before a successful build",
            field="build_success",
            recommended_level=ClosureLevel.SYMPTOM_SUPPRESSED,
        )
    if not manifest.hash_verified:
        return _failed(
            GateId.ARTIFACT,
            "ARTIFACT_HASH_NOT_VERIFIED",
            "Build Success does not prove that this artifact is the tested artifact",
            field="hash_verified",
            recommended_level=ClosureLevel.BUILD_SUCCESS,
        )
    return _passed(GateId.ARTIFACT)


def verify_artifact_file(manifest: ArtifactManifest | None) -> GateResult:
    """Verify the recorded artifact path has the recorded hash."""

    if manifest is None:
        return _failed(
            GateId.ARTIFACT,
            "ARTIFACT_MANIFEST_REQUIRED",
            "artifact manifest is required",
            field="artifact_manifest",
        )
    if not manifest.build_success:
        return _failed(
            GateId.ARTIFACT,
            "BUILD_NOT_SUCCESS",
            "artifact hash cannot be verified before a successful build",
            field="build_success",
            recommended_level=ClosureLevel.SYMPTOM_SUPPRESSED,
        )
    artifact = Path(manifest.artifact_path).expanduser()
    if not artifact.is_file():
        return _failed(
            GateId.ARTIFACT,
            "ARTIFACT_FILE_MISSING",
            f"artifact file does not exist: {artifact}",
            field="artifact_path",
            recommended_level=ClosureLevel.BUILD_SUCCESS,
        )
    actual = compute_sha256(artifact)
    if actual != manifest.artifact_sha256:
        return _failed(
            GateId.ARTIFACT,
            "ARTIFACT_HASH_MISMATCH",
            "artifact file hash does not match the manifest",
            field="artifact_sha256",
            recommended_level=ClosureLevel.BUILD_SUCCESS,
        )
    return _passed(GateId.ARTIFACT)


_PROVENANCE_ORDER = (
    ProvenanceKind.SOURCE,
    ProvenanceKind.OBJECT,
    ProvenanceKind.ELF,
    ProvenanceKind.BIN_FW,
    ProvenanceKind.FLASHED_ARTIFACT,
    ProvenanceKind.DEVICE_UNDER_TEST,
)


def check_provenance(
    chain: ProvenanceChain | None, manifest: ArtifactManifest | None = None
) -> GateResult:
    """Validate all six nodes and their exact sequential links."""

    if chain is None:
        return _failed(
            GateId.PROVENANCE,
            "PROVENANCE_REQUIRED",
            "complete Source-to-Device provenance is required",
            field="provenance",
        )
    if len(chain.nodes) != len(_PROVENANCE_ORDER) or len(chain.links) != len(_PROVENANCE_ORDER) - 1:
        return _failed(
            GateId.PROVENANCE,
            "PROVENANCE_CHAIN_INCOMPLETE",
            "provenance must contain exactly six ordered nodes and five links",
            field="provenance",
        )
    node_ids = [node.node_id for node in chain.nodes]
    if len(node_ids) != len(set(node_ids)):
        return _failed(
            GateId.PROVENANCE,
            "PROVENANCE_DUPLICATE_NODE",
            "provenance node IDs must be unique",
            field="provenance.nodes",
        )
    kinds = [node.kind for node in chain.nodes]
    if tuple(kinds) != _PROVENANCE_ORDER:
        return _failed(
            GateId.PROVENANCE,
            "PROVENANCE_ORDER_INVALID",
            "provenance order must be Source → Object → ELF → BIN/FW → Flashed Artifact → Device Under Test",
            field="provenance.nodes",
        )
    for node in chain.nodes:
        if not node.available:
            return _failed(
                GateId.PROVENANCE,
                "PROVENANCE_NODE_UNAVAILABLE",
                f"provenance node is unavailable: {node.node_id}",
                field=f"provenance.nodes.{node.node_id}.available",
            )
    expected_links = tuple(
        ProvenanceLink(source_id=chain.nodes[index].node_id, target_id=chain.nodes[index + 1].node_id)
        for index in range(len(chain.nodes) - 1)
    )
    if chain.links != expected_links:
        return _failed(
            GateId.PROVENANCE,
            "PROVENANCE_LINK_INVALID",
            "provenance links must connect each adjacent node exactly once",
            field="provenance.links",
        )
    if manifest is not None:
        source = chain.nodes[0]
        bin_fw = chain.nodes[3]
        if source.revision != manifest.source_revision:
            return _failed(
                GateId.PROVENANCE,
                "SOURCE_REVISION_MISMATCH",
                "source revision does not match the artifact manifest",
                field="source.revision",
            )
        if bin_fw.identifier != manifest.artifact_id or bin_fw.sha256 != manifest.artifact_sha256:
            return _failed(
                GateId.PROVENANCE,
                "ARTIFACT_PROVENANCE_MISMATCH",
                "BIN/FW provenance node does not match the artifact manifest",
                field="provenance.nodes.BIN/FW",
            )
    return _passed(GateId.PROVENANCE)


def _has_at_least(record: ClosureRecord, level: ClosureLevel) -> bool:
    """Check the prerequisite claims needed by one closure level."""

    if level is ClosureLevel.SYMPTOM_SUPPRESSED:
        return bool(record.evidence_refs)
    if level is ClosureLevel.CONTAINMENT:
        return bool(record.evidence_refs and record.containment_scope)
    if level is ClosureLevel.LOCAL_FIX:
        return bool(record.evidence_refs and record.fix_change_ref)
    if level is ClosureLevel.ROOT_CAUSE_IDENTIFIED:
        return bool(record.evidence_refs and record.root_cause_proven)
    if level is ClosureLevel.ROOT_FIX:
        return bool(record.evidence_refs and record.root_cause_proven and record.fix_change_ref)
    if level is ClosureLevel.BUILD_SUCCESS:
        return bool(record.build_success and record.artifact_manifest and record.artifact_manifest.build_success)
    if level is ClosureLevel.ARTIFACT_VERIFIED:
        return bool(
            _has_at_least(record, ClosureLevel.BUILD_SUCCESS)
            and record.artifact_verified
            and record.artifact_manifest
            and record.artifact_manifest.hash_verified
        )
    if level is ClosureLevel.SYSTEM_VALIDATION:
        test_object = record.test_object or (
            None if record.artifact_manifest is None else record.artifact_manifest.test_object
        )
        return bool(
            _has_at_least(record, ClosureLevel.ARTIFACT_VERIFIED)
            and record.system_validation_passed
            and test_object
        )
    if level is ClosureLevel.PRODUCT_CLOSURE:
        return bool(
            _has_at_least(record, ClosureLevel.ROOT_FIX)
            and _has_at_least(record, ClosureLevel.SYSTEM_VALIDATION)
            and record.acceptance_complete
            and record.provenance is not None
        )
    return False


def highest_supported_level(record: ClosureRecord) -> ClosureLevel | None:
    """Return the strongest claim currently supported by the record."""

    if _has_at_least(record, ClosureLevel.PRODUCT_CLOSURE):
        return ClosureLevel.PRODUCT_CLOSURE
    if _has_at_least(record, ClosureLevel.SYSTEM_VALIDATION):
        return ClosureLevel.SYSTEM_VALIDATION
    if _has_at_least(record, ClosureLevel.ARTIFACT_VERIFIED):
        return ClosureLevel.ARTIFACT_VERIFIED
    if _has_at_least(record, ClosureLevel.BUILD_SUCCESS):
        return ClosureLevel.BUILD_SUCCESS
    for level in (
        ClosureLevel.ROOT_FIX,
        ClosureLevel.ROOT_CAUSE_IDENTIFIED,
        ClosureLevel.LOCAL_FIX,
        ClosureLevel.CONTAINMENT,
        ClosureLevel.SYMPTOM_SUPPRESSED,
    ):
        if _has_at_least(record, level):
            return level
    return None


def check_closure(record: ClosureRecord | None) -> GateResult:
    """Validate a closure claim and return a safe downgrade when it fails."""

    if record is None:
        return _failed(
            GateId.CLOSURE,
            "CLOSURE_RECORD_REQUIRED",
            "closure record is required",
            field="closure",
        )
    requested = record.requested_level
    if requested is ClosureLevel.ARTIFACT_VERIFIED:
        artifact_result = check_artifact_manifest(record.artifact_manifest)
        if not artifact_result.passed:
            return artifact_result
    if requested is ClosureLevel.SYSTEM_VALIDATION:
        artifact_result = check_artifact_manifest(record.artifact_manifest)
        if not artifact_result.passed:
            return _failed(
                GateId.CLOSURE,
                artifact_result.findings[0].code,
                artifact_result.findings[0].message,
                field=artifact_result.findings[0].field,
                recommended_level=ClosureLevel.BUILD_SUCCESS,
            )
    if requested is ClosureLevel.PRODUCT_CLOSURE:
        artifact_result = check_artifact_manifest(record.artifact_manifest)
        if not artifact_result.passed:
            return _failed(
                GateId.CLOSURE,
                artifact_result.findings[0].code,
                artifact_result.findings[0].message,
                field=artifact_result.findings[0].field,
                recommended_level=ClosureLevel.BUILD_SUCCESS,
            )
        provenance_result = check_provenance(record.provenance, record.artifact_manifest)
        if not provenance_result.passed:
            return _failed(
                GateId.CLOSURE,
                provenance_result.findings[0].code,
                provenance_result.findings[0].message,
                field=provenance_result.findings[0].field,
                recommended_level=highest_supported_level(record),
            )
    if _has_at_least(record, requested):
        return _passed(GateId.CLOSURE)
    strongest = highest_supported_level(record)
    return _failed(
        GateId.CLOSURE,
        "CLOSURE_PREREQUISITE_MISSING",
        f"prerequisites for {requested.value} are incomplete; do not upgrade an intermediate result",
        field="requested_level",
        recommended_level=strongest,
    )


__all__ = [
    "ArtifactKind",
    "ArtifactManifest",
    "ClosureLevel",
    "ClosureRecord",
    "ProvenanceChain",
    "ProvenanceKind",
    "ProvenanceLink",
    "ProvenanceNode",
    "check_artifact_manifest",
    "check_closure",
    "check_provenance",
    "compute_sha256",
    "highest_supported_level",
    "verify_artifact_file",
]
