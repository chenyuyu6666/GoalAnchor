"""Adapters that keep external review and graph output inside GoalAnchor bounds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .core import Claim, ClaimKind, ContractValidationError
from .gates import GateFinding, GateId, GateResult


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _strings(value: Any, field_name: str, *, required: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{field_name} must be an array of strings")
    result = tuple(_text(item, f"{field_name}[]") for item in value)
    if required and not result:
        raise ContractValidationError(f"{field_name} must contain at least one item")
    return result


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


def _first(data: Mapping[str, Any], field_name: str, aliases: tuple[str, ...] = ()) -> Any:
    for key in (field_name, *aliases):
        if key in data:
            return data[key]
    raise ContractValidationError(f"missing required field: {field_name}")


class GraphConfidence(str, Enum):
    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class ReviewFinding:
    """Normalized Review Agent output with a decision link and uncertainty."""

    finding_id: str
    issue: str
    evidence_refs: tuple[str, ...]
    decision_impact: str
    remaining_uncertainty: str
    source_refs: tuple[str, ...] = ()
    status: str = "OPEN"

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", _text(self.finding_id, "finding_id"))
        object.__setattr__(self, "issue", _text(self.issue, "issue"))
        object.__setattr__(
            self, "evidence_refs", _strings(self.evidence_refs, "evidence_refs", required=True)
        )
        object.__setattr__(
            self, "decision_impact", _text(self.decision_impact, "decision_impact")
        )
        object.__setattr__(
            self,
            "remaining_uncertainty",
            _text(self.remaining_uncertainty, "remaining_uncertainty"),
        )
        object.__setattr__(self, "source_refs", _strings(self.source_refs, "source_refs"))
        object.__setattr__(self, "status", _text(self.status, "status"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "issue": self.issue,
            "evidence_refs": list(self.evidence_refs),
            "decision_impact": self.decision_impact,
            "remaining_uncertainty": self.remaining_uncertainty,
            "source_refs": list(self.source_refs),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ReviewFinding:
        data = _mapping(payload, "ReviewFinding")
        allowed = {
            "finding_id",
            "issue",
            "problem",
            "evidence_refs",
            "evidence",
            "decision_impact",
            "decision_link",
            "remaining_uncertainty",
            "uncertainty",
            "source_refs",
            "status",
        }
        _reject_unknown(data, allowed, "ReviewFinding")
        return cls(
            finding_id=_first(data, "finding_id"),
            issue=_first(data, "issue", ("problem",)),
            evidence_refs=_first(data, "evidence_refs", ("evidence",)),
            decision_impact=_first(data, "decision_impact", ("decision_link",)),
            remaining_uncertainty=_first(
                data, "remaining_uncertainty", ("uncertainty",)
            ),
            source_refs=data.get("source_refs", ()),
            status=data.get("status", "OPEN"),
        )


@dataclass(frozen=True)
class GraphEvidenceRef:
    """A Graphify node retained as location-bound investigation input."""

    node_id: str
    label: str
    source_file: str
    source_location: str
    confidence: GraphConfidence
    relation: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _text(self.node_id, "node_id"))
        object.__setattr__(self, "label", _text(self.label, "label"))
        object.__setattr__(self, "source_file", _text(self.source_file, "source_file"))
        object.__setattr__(
            self, "source_location", _text(self.source_location, "source_location")
        )
        confidence = self.confidence
        if not isinstance(confidence, GraphConfidence):
            try:
                confidence = GraphConfidence(confidence)
            except (TypeError, ValueError) as exc:
                raise ContractValidationError("confidence is invalid") from exc
            object.__setattr__(self, "confidence", confidence)
        if self.relation is not None:
            object.__setattr__(self, "relation", _text(self.relation, "relation"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "source_file": self.source_file,
            "source_location": self.source_location,
            "confidence": self.confidence.value,
            "relation": self.relation,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> GraphEvidenceRef:
        data = _mapping(payload, "GraphEvidenceRef")
        allowed = {
            "node_id",
            "id",
            "label",
            "source_file",
            "source_location",
            "confidence",
            "relation",
        }
        _reject_unknown(data, allowed, "GraphEvidenceRef")
        return cls(
            node_id=_first(data, "node_id", ("id",)),
            label=_first(data, "label"),
            source_file=_first(data, "source_file"),
            source_location=_first(data, "source_location"),
            confidence=_first(data, "confidence"),
            relation=data.get("relation"),
        )

    def as_claim(self) -> Claim:
        """Convert a graph result to Observation, never Fact or RootCause."""

        return Claim(
            text=self.label,
            kind=ClaimKind.OBSERVATION,
            source=f"{self.source_file}:{self.source_location}",
        )


def _failed(gate_id: GateId, code: str, message: str, field: str | None = None) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        passed=False,
        findings=(GateFinding(code=code, message=message, field=field),),
    )


def _passed(gate_id: GateId) -> GateResult:
    return GateResult(gate_id=gate_id, passed=True)


def check_review_finding(finding: ReviewFinding) -> GateResult:
    if not isinstance(finding, ReviewFinding):
        return _failed(
            GateId.REVIEW_INTEGRATION,
            "INVALID_REVIEW_FINDING",
            "ReviewFinding is required",
        )
    return _passed(GateId.REVIEW_INTEGRATION)


def check_review_output(payload: Mapping[str, Any]) -> GateResult:
    """Require every Review Agent finding to expose four decision fields."""

    try:
        data = _mapping(payload, "ReviewOutput")
        findings_data = _first(data, "findings")
        if isinstance(findings_data, (str, bytes)) or not isinstance(findings_data, Sequence):
            raise ContractValidationError("findings must be an array")
        findings = tuple(ReviewFinding.from_dict(item) for item in findings_data)
    except ContractValidationError as exc:
        return _failed(GateId.REVIEW_INTEGRATION, "REVIEW_OUTPUT_INVALID", str(exc), "findings")
    if not findings:
        return _failed(
            GateId.REVIEW_INTEGRATION,
            "REVIEW_FINDINGS_EMPTY",
            "Review Agent output must contain at least one finding",
            "findings",
        )
    return _passed(GateId.REVIEW_INTEGRATION)


def normalize_review_output(payload: Mapping[str, Any]) -> tuple[ReviewFinding, ...]:
    """Return canonical findings or fail without inventing missing fields."""

    data = _mapping(payload, "ReviewOutput")
    findings_data = _first(data, "findings")
    if isinstance(findings_data, (str, bytes)) or not isinstance(findings_data, Sequence):
        raise ContractValidationError("findings must be an array")
    findings = tuple(ReviewFinding.from_dict(item) for item in findings_data)
    if not findings:
        raise ContractValidationError("findings must contain at least one item")
    return findings


def check_graphify_refs(refs: Sequence[GraphEvidenceRef]) -> GateResult:
    """Require source locations and preserve Graphify confidence labels."""

    values = tuple(refs)
    if not values:
        return _failed(
            GateId.GRAPHIFY_INTEGRATION,
            "GRAPHIFY_REFS_EMPTY",
            "Graphify output must contain at least one node reference",
            "nodes",
        )
    for ref in values:
        if not isinstance(ref, GraphEvidenceRef):
            return _failed(
                GateId.GRAPHIFY_INTEGRATION,
                "INVALID_GRAPHIFY_REF",
                "nodes must contain GraphEvidenceRef values",
            )
    return _passed(GateId.GRAPHIFY_INTEGRATION)


def check_graphify_output(payload: Mapping[str, Any]) -> GateResult:
    """Check a Graphify result without treating it as a causal conclusion."""

    try:
        data = _mapping(payload, "GraphifyOutput")
        nodes_data = _first(data, "nodes")
        if isinstance(nodes_data, (str, bytes)) or not isinstance(nodes_data, Sequence):
            raise ContractValidationError("nodes must be an array")
        refs = tuple(GraphEvidenceRef.from_dict(item) for item in nodes_data)
    except ContractValidationError as exc:
        return _failed(GateId.GRAPHIFY_INTEGRATION, "GRAPHIFY_OUTPUT_INVALID", str(exc), "nodes")
    return check_graphify_refs(refs)


def normalize_graphify_output(payload: Mapping[str, Any]) -> tuple[GraphEvidenceRef, ...]:
    """Return location-bound graph references for VERIFY input."""

    data = _mapping(payload, "GraphifyOutput")
    nodes_data = _first(data, "nodes")
    if isinstance(nodes_data, (str, bytes)) or not isinstance(nodes_data, Sequence):
        raise ContractValidationError("nodes must be an array")
    refs = tuple(GraphEvidenceRef.from_dict(item) for item in nodes_data)
    result = check_graphify_refs(refs)
    if not result.passed:
        raise ContractValidationError(result.findings[0].message)
    return refs


def graphify_claims_as_observations(
    payload: Mapping[str, Any]
) -> tuple[Claim, ...]:
    """Create only Observation claims from a validated graph result."""

    return tuple(ref.as_claim() for ref in normalize_graphify_output(payload))


__all__ = [
    "GraphConfidence",
    "GraphEvidenceRef",
    "ReviewFinding",
    "check_graphify_output",
    "check_graphify_refs",
    "check_review_finding",
    "check_review_output",
    "graphify_claims_as_observations",
    "normalize_graphify_output",
    "normalize_review_output",
]
