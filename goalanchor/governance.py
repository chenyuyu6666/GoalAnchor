"""Compatibility mapping from existing checks to the five governance gates.

This module does not execute checks or reinterpret their findings.  It only
assigns an existing :class:`GateResult` to one primary governance owner and
zero or more secondary consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .core import ContractValidationError, GoalContract, transition
from .gates import GateId, GateReport, GateResult


class GovernanceGate(str, Enum):
    GOAL = "GoalGate"
    EVIDENCE = "EvidenceGate"
    DECISION = "DecisionGate"
    EXECUTION = "ExecutionGate"
    VERIFICATION = "VerificationGate"


@dataclass(frozen=True)
class GovernanceMapping:
    """A non-executing view of one existing result under a governance gate."""

    gate_id: GovernanceGate
    result: GateResult
    source_rules: tuple[GateId, ...]
    primary_owner: bool

    def __post_init__(self) -> None:
        if not isinstance(self.gate_id, GovernanceGate):
            try:
                object.__setattr__(self, "gate_id", GovernanceGate(self.gate_id))
            except (TypeError, ValueError) as exc:
                raise ContractValidationError("governance gate_id is invalid") from exc
        if not isinstance(self.result, GateResult):
            raise ContractValidationError("result must be a GateResult")
        source_rules = tuple(self.source_rules)
        if not source_rules or any(not isinstance(item, GateId) for item in source_rules):
            raise ContractValidationError("source_rules must contain GateId values")
        object.__setattr__(self, "source_rules", source_rules)
        if not isinstance(self.primary_owner, bool):
            raise ContractValidationError("primary_owner must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id.value,
            "result": self.result.to_dict(),
            "source_rules": [item.value for item in self.source_rules],
            "primary_owner": self.primary_owner,
        }


_OWNERS: dict[GateId, tuple[GovernanceGate, tuple[GovernanceGate, ...]]] = {
    GateId.Q1_GOAL: (GovernanceGate.GOAL, ()),
    GateId.Q2_LOGIC: (GovernanceGate.GOAL, (GovernanceGate.EVIDENCE,)),
    GateId.Q3_PATH: (
        GovernanceGate.EVIDENCE,
        (GovernanceGate.GOAL, GovernanceGate.DECISION),
    ),
    GateId.Q4_MINIMUM: (GovernanceGate.DECISION, (GovernanceGate.EXECUTION,)),
    GateId.Q5_STRUCTURE: (GovernanceGate.DECISION, (GovernanceGate.GOAL,)),
    GateId.VERIFY: (GovernanceGate.VERIFICATION, (GovernanceGate.EVIDENCE,)),
    GateId.EXECUTE: (GovernanceGate.EXECUTION, (GovernanceGate.DECISION,)),
    GateId.PLAN_CHANGED: (GovernanceGate.GOAL, (GovernanceGate.DECISION,)),
    GateId.EVIDENCE: (GovernanceGate.EVIDENCE, (GovernanceGate.VERIFICATION,)),
    GateId.CONCLUSION: (GovernanceGate.EVIDENCE, (GovernanceGate.VERIFICATION,)),
    GateId.EXPERIMENT: (GovernanceGate.EVIDENCE, (GovernanceGate.VERIFICATION,)),
    GateId.DESIGN_NECESSITY: (GovernanceGate.DECISION, (GovernanceGate.EVIDENCE,)),
    GateId.ARTIFACT: (GovernanceGate.VERIFICATION, (GovernanceGate.EVIDENCE,)),
    GateId.PROVENANCE: (GovernanceGate.VERIFICATION, (GovernanceGate.EVIDENCE,)),
    GateId.CLOSURE: (GovernanceGate.VERIFICATION, ()),
    GateId.REVIEW_INTEGRATION: (GovernanceGate.EVIDENCE, ()),
    GateId.GRAPHIFY_INTEGRATION: (GovernanceGate.EVIDENCE, ()),
}


def governance_owners(gate_id: GateId) -> tuple[GovernanceGate, tuple[GovernanceGate, ...]]:
    """Return the unique primary owner and read-only secondary consumers."""

    if not isinstance(gate_id, GateId):
        try:
            gate_id = GateId(gate_id)
        except (TypeError, ValueError) as exc:
            raise ContractValidationError("gate_id is invalid") from exc
    return _OWNERS[gate_id]


def map_gate_result(result: GateResult) -> tuple[GovernanceMapping, ...]:
    """Map a result without rerunning or changing its underlying check."""

    if not isinstance(result, GateResult):
        raise ContractValidationError("result must be a GateResult")
    primary, secondary = governance_owners(result.gate_id)
    source = (result.gate_id,)
    return (
        GovernanceMapping(primary, result, source, True),
        *(GovernanceMapping(gate, result, source, False) for gate in secondary),
    )


def map_gate_results(results: Iterable[GateResult]) -> tuple[GovernanceMapping, ...]:
    """Map already-computed results while preserving their input order."""

    mapped: list[GovernanceMapping] = []
    for result in results:
        mapped.extend(map_gate_result(result))
    return tuple(mapped)


def map_gate_report(report: GateReport) -> tuple[GovernanceMapping, ...]:
    if not isinstance(report, GateReport):
        raise ContractValidationError("report must be a GateReport")
    return map_gate_results(report.results)


def apply_suggested_transition(
    contract: GoalContract,
    mapping: GovernanceMapping,
    *,
    blocking_uncertainty: str | None = None,
) -> GoalContract:
    """Apply an explicit existing ``next_state`` through the lifecycle guard.

    Secondary consumers cannot drive lifecycle changes, and this adapter never
    infers a state when the source ``GateResult`` did not provide one.
    """

    if not isinstance(contract, GoalContract):
        raise ContractValidationError("contract must be a GoalContract")
    if not isinstance(mapping, GovernanceMapping):
        raise ContractValidationError("mapping must be a GovernanceMapping")
    if not mapping.primary_owner:
        raise ContractValidationError("secondary governance mappings cannot change lifecycle")
    if mapping.result.next_state is None:
        raise ContractValidationError("GateResult does not provide a next_state")
    return transition(
        contract,
        mapping.result.next_state,
        blocking_uncertainty=blocking_uncertainty,
    )


__all__ = [
    "GovernanceGate",
    "GovernanceMapping",
    "apply_suggested_transition",
    "governance_owners",
    "map_gate_report",
    "map_gate_result",
    "map_gate_results",
]
