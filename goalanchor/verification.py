"""Verification outcomes and controlled lifecycle routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from .core import ContractValidationError, GoalContract, LifecycleState, transition


class VerificationOutcome(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{field_name} must be an array of strings")
    return tuple(_text(item, f"{field_name}[]") for item in value)


@dataclass(frozen=True)
class VerificationResult:
    """A bounded verification conclusion independent from lifecycle state."""

    outcome: VerificationOutcome
    acceptance_criteria: tuple[str, ...]
    completed_criteria: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    reason: str
    remaining_risks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        outcome = self.outcome
        if not isinstance(outcome, VerificationOutcome):
            try:
                outcome = VerificationOutcome(outcome)
            except (TypeError, ValueError) as exc:
                raise ContractValidationError("outcome is invalid") from exc
            object.__setattr__(self, "outcome", outcome)
        criteria = _strings(self.acceptance_criteria, "acceptance_criteria")
        completed = _strings(self.completed_criteria, "completed_criteria")
        evidence = _strings(self.evidence_refs, "evidence_refs")
        if not criteria:
            raise ContractValidationError("acceptance_criteria must contain at least one item")
        if any(item not in criteria for item in completed):
            raise ContractValidationError("completed_criteria must be drawn from acceptance_criteria")
        if outcome is VerificationOutcome.PASS and set(completed) != set(criteria):
            raise ContractValidationError("PASS requires every acceptance criterion to be completed")
        if outcome is not VerificationOutcome.PASS and not evidence:
            raise ContractValidationError("WARN and FAIL require evidence_refs")
        if outcome is VerificationOutcome.WARN and not _strings(self.remaining_risks, "remaining_risks"):
            raise ContractValidationError("WARN requires remaining_risks")
        object.__setattr__(self, "acceptance_criteria", criteria)
        object.__setattr__(self, "completed_criteria", completed)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(
            self, "remaining_risks", _strings(self.remaining_risks, "remaining_risks")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "acceptance_criteria": list(self.acceptance_criteria),
            "completed_criteria": list(self.completed_criteria),
            "evidence_refs": list(self.evidence_refs),
            "reason": self.reason,
            "remaining_risks": list(self.remaining_risks),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VerificationResult":
        return cls(**dict(payload))


def apply_verification_transition(
    contract: GoalContract, result: VerificationResult
) -> GoalContract:
    """Route a verification result without inventing a completion state."""

    if not isinstance(contract, GoalContract):
        raise ContractValidationError("contract must be a GoalContract")
    if not isinstance(result, VerificationResult):
        raise ContractValidationError("result must be a VerificationResult")
    if result.outcome is VerificationOutcome.PASS:
        if contract.stage is not LifecycleState.EXECUTE:
            raise ContractValidationError("PASS requires an EXECUTE contract stage")
        return transition(contract, LifecycleState.CLOSED)
    if result.outcome is VerificationOutcome.FAIL:
        if contract.stage not in {LifecycleState.PLAN, LifecycleState.VERIFY, LifecycleState.EXECUTE}:
            raise ContractValidationError("FAIL requires a replannable contract stage")
        return transition(
            contract,
            LifecycleState.REPLAN,
            blocking_uncertainty=result.reason,
        )
    return contract


__all__ = [
    "VerificationOutcome",
    "VerificationResult",
    "apply_verification_transition",
]
