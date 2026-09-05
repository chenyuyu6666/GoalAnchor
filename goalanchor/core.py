"""GoalAnchor stage-1 contract and lifecycle primitives.

The stage-1 model deliberately stays small.  It records the goal and the
decision-relevant work item fields, preserves claim classification, and makes
plan changes observable.  Evidence strength, experiment quality, and artifact
provenance are intentionally left to later stages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Sequence


class GoalAnchorError(ValueError):
    """Base error for invalid GoalAnchor model data."""


class ContractValidationError(GoalAnchorError):
    """Raised when a contract, work item, or claim violates its schema."""


class InvalidTransitionError(GoalAnchorError):
    """Raised when a lifecycle transition is not allowed."""


class LifecycleState(str, Enum):
    """Small lifecycle state set used by a :class:`GoalContract`."""

    PLAN = "PLAN"
    VERIFY = "VERIFY"
    EXECUTE = "EXECUTE"
    BLOCKED = "BLOCKED"
    REPLAN = "REPLAN"
    CLOSED = "CLOSED"


class WorkItemStatus(str, Enum):
    """Execution status for one bounded work item."""

    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    VERIFYING = "VERIFYING"
    DONE = "DONE"


class ClaimKind(str, Enum):
    """Classification boundary for statements used as input evidence."""

    OBSERVATION = "Observation"
    FACT = "Fact"
    HYPOTHESIS = "Hypothesis"
    CONSTRAINT = "Constraint"


class PlanChangeField(str, Enum):
    """A contract field whose semantic change invalidates prior plan proof."""

    OBJECTIVE = "objective"
    REQUIREMENTS = "requirements"
    CONSTRAINTS = "constraints"
    CORE_ASSUMPTIONS = "core_assumptions"
    MAIN_STRATEGY = "main_strategy"
    ACCEPTANCE = "acceptance_criteria"
    EVIDENCE_OBLIGATIONS = "evidence_obligations"
    DELIVERABLE_SCOPE = "deliverables"


_MISSING = object()


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractValidationError(f"{field_name} must be a string or null")
    normalized = value.strip()
    if not normalized or normalized.upper() == "NONE":
        return None
    return normalized


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{field_name} must be an array of strings")
    return tuple(_require_text(item, f"{field_name}[]") for item in value)


def _required_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    result = _string_tuple(value, field_name)
    if not result:
        raise ContractValidationError(f"{field_name} must contain at least one item")
    return result


def _enum_value(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
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


@dataclass(frozen=True)
class Claim:
    """A statement whose classification must be explicit.

    An observation is not converted to a fact, and a hypothesis is not
    converted to a constraint, by deserialization or any other implicit path.
    Promotion is an explicit ``reclassify(..., basis=...)`` operation.  Facts
    and constraints always carry the basis that justified the classification;
    the later Evidence model will define how strong that basis is.
    """

    text: str
    kind: ClaimKind
    source: str
    basis: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _require_text(self.text, "text"))
        object.__setattr__(self, "kind", _enum_value(self.kind, ClaimKind, "kind"))
        object.__setattr__(self, "source", _require_text(self.source, "source"))
        basis = _optional_text(self.basis, "basis")
        if self.kind in (ClaimKind.FACT, ClaimKind.CONSTRAINT) and basis is None:
            raise ContractValidationError(
                f"{self.kind.value} claims require an explicit basis"
            )
        object.__setattr__(self, "basis", basis)

    def reclassify(self, kind: ClaimKind, *, basis: str) -> "Claim":
        """Return a deliberately reclassified copy with an explicit basis."""

        target = _enum_value(kind, ClaimKind, "kind")
        if target in (ClaimKind.FACT, ClaimKind.CONSTRAINT) and (
            not isinstance(basis, str) or not basis.strip()
        ):
            raise ContractValidationError(
                f"{target.value} claims require an explicit basis"
            )
        return replace(self, kind=target, basis=_require_text(basis, "basis"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind.value,
            "source": self.source,
            "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Claim":
        data = _mapping(payload, "Claim")
        _reject_unknown(data, {"text", "kind", "source", "basis"}, "Claim")
        missing = {"text", "kind", "source"} - set(data)
        if missing:
            raise ContractValidationError(
                f"Claim missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            text=data["text"],
            kind=data["kind"],
            source=data["source"],
            basis=data.get("basis"),
        )

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, payload: str) -> "Claim":
        try:
            decoded = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractValidationError("Claim JSON is invalid") from exc
        return cls.from_dict(decoded)


@dataclass(frozen=True)
class WorkItem:
    """One bounded action and the decision it is expected to affect."""

    item_id: str
    issue: str
    input_evidence: tuple[str, ...]
    action: str
    expected_decision_impact: str
    exit_criterion: str
    status: WorkItemStatus = WorkItemStatus.TODO
    expected_result: str | None = None
    decision_link: str | None = None
    assumptions: tuple[str, ...] = ()
    assumptions_verified: bool = False
    unverified_patch_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _require_text(self.item_id, "item_id"))
        object.__setattr__(self, "issue", _require_text(self.issue, "issue"))
        object.__setattr__(
            self, "input_evidence", _string_tuple(self.input_evidence, "input_evidence")
        )
        object.__setattr__(self, "action", _require_text(self.action, "action"))
        object.__setattr__(
            self,
            "expected_decision_impact",
            _require_text(self.expected_decision_impact, "expected_decision_impact"),
        )
        object.__setattr__(
            self, "exit_criterion", _require_text(self.exit_criterion, "exit_criterion")
        )
        object.__setattr__(
            self, "status", _enum_value(self.status, WorkItemStatus, "status")
        )
        object.__setattr__(
            self, "expected_result", _optional_text(self.expected_result, "expected_result")
        )
        object.__setattr__(
            self, "decision_link", _optional_text(self.decision_link, "decision_link")
        )
        object.__setattr__(self, "assumptions", _string_tuple(self.assumptions, "assumptions"))
        if not isinstance(self.assumptions_verified, bool):
            raise ContractValidationError("assumptions_verified must be a boolean")
        object.__setattr__(
            self,
            "unverified_patch_refs",
            _string_tuple(self.unverified_patch_refs, "unverified_patch_refs"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "issue": self.issue,
            "input_evidence": list(self.input_evidence),
            "action": self.action,
            "expected_decision_impact": self.expected_decision_impact,
            "exit_criterion": self.exit_criterion,
            "status": self.status.value,
            "expected_result": self.expected_result,
            "decision_link": self.decision_link,
            "assumptions": list(self.assumptions),
            "assumptions_verified": self.assumptions_verified,
            "unverified_patch_refs": list(self.unverified_patch_refs),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkItem":
        data = _mapping(payload, "WorkItem")
        allowed = {
            "item_id",
            "issue",
            "input_evidence",
            "action",
            "expected_decision_impact",
            "exit_criterion",
            "status",
            "expected_result",
            "decision_link",
            "assumptions",
            "assumptions_verified",
            "unverified_patch_refs",
        }
        _reject_unknown(data, allowed, "WorkItem")
        missing = allowed - {"status"} - set(data)
        if missing:
            raise ContractValidationError(
                f"WorkItem missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            item_id=data["item_id"],
            issue=data["issue"],
            input_evidence=data["input_evidence"],
            action=data["action"],
            expected_decision_impact=data["expected_decision_impact"],
            exit_criterion=data["exit_criterion"],
            status=data.get("status", WorkItemStatus.TODO),
            expected_result=data.get("expected_result"),
            decision_link=data.get("decision_link"),
            assumptions=data.get("assumptions", ()),
            assumptions_verified=data.get("assumptions_verified", False),
            unverified_patch_refs=data.get("unverified_patch_refs", ()),
        )

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, payload: str) -> "WorkItem":
        try:
            decoded = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractValidationError("WorkItem JSON is invalid") from exc
        return cls.from_dict(decoded)


@dataclass(frozen=True)
class GoalContract:
    """Serializable contract that anchors all later work decisions."""

    task_objective: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    current_blocking_uncertainty: str | None
    stage: LifecycleState
    deliverables: tuple[str, ...]
    requirements: tuple[str, ...] = ()
    core_assumptions: tuple[str, ...] = ()
    main_strategy: str | None = None
    evidence_obligations: tuple[str, ...] = ()
    plan_revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "task_objective", _require_text(self.task_objective, "task_objective")
        )
        object.__setattr__(self, "constraints", _string_tuple(self.constraints, "constraints"))
        object.__setattr__(
            self,
            "acceptance_criteria",
            _required_string_tuple(self.acceptance_criteria, "acceptance_criteria"),
        )
        object.__setattr__(
            self,
            "current_blocking_uncertainty",
            _optional_text(
                self.current_blocking_uncertainty, "current_blocking_uncertainty"
            ),
        )
        object.__setattr__(self, "stage", _enum_value(self.stage, LifecycleState, "stage"))
        object.__setattr__(self, "deliverables", _required_string_tuple(self.deliverables, "deliverables"))
        object.__setattr__(self, "requirements", _string_tuple(self.requirements, "requirements"))
        object.__setattr__(
            self, "core_assumptions", _string_tuple(self.core_assumptions, "core_assumptions")
        )
        object.__setattr__(self, "main_strategy", _optional_text(self.main_strategy, "main_strategy"))
        object.__setattr__(
            self,
            "evidence_obligations",
            _string_tuple(self.evidence_obligations, "evidence_obligations"),
        )
        if isinstance(self.plan_revision, bool) or not isinstance(self.plan_revision, int):
            raise ContractValidationError("plan_revision must be a non-negative integer")
        if self.plan_revision < 0:
            raise ContractValidationError("plan_revision must be a non-negative integer")
        if self.stage == LifecycleState.BLOCKED and self.current_blocking_uncertainty is None:
            raise ContractValidationError("BLOCKED stage requires current_blocking_uncertainty")
        if self.stage == LifecycleState.CLOSED and self.current_blocking_uncertainty is not None:
            raise ContractValidationError("CLOSED stage cannot retain a blocking uncertainty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_objective": self.task_objective,
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "current_blocking_uncertainty": self.current_blocking_uncertainty,
            "stage": self.stage.value,
            "deliverables": list(self.deliverables),
            "requirements": list(self.requirements),
            "core_assumptions": list(self.core_assumptions),
            "main_strategy": self.main_strategy,
            "evidence_obligations": list(self.evidence_obligations),
            "plan_revision": self.plan_revision,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GoalContract":
        data = _mapping(payload, "GoalContract")
        required = {
            "task_objective",
            "constraints",
            "acceptance_criteria",
            "current_blocking_uncertainty",
            "stage",
            "deliverables",
        }
        optional = {
            "requirements",
            "core_assumptions",
            "main_strategy",
            "evidence_obligations",
            "plan_revision",
        }
        _reject_unknown(data, required | optional, "GoalContract")
        missing = required - set(data)
        if missing:
            raise ContractValidationError(
                f"GoalContract missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            task_objective=data["task_objective"],
            constraints=data["constraints"],
            acceptance_criteria=data["acceptance_criteria"],
            current_blocking_uncertainty=data["current_blocking_uncertainty"],
            stage=data["stage"],
            deliverables=data["deliverables"],
            requirements=data.get("requirements", ()),
            core_assumptions=data.get("core_assumptions", ()),
            main_strategy=data.get("main_strategy"),
            evidence_obligations=data.get("evidence_obligations", ()),
            plan_revision=data.get("plan_revision", 0),
        )

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, payload: str) -> "GoalContract":
        try:
            decoded = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractValidationError("GoalContract JSON is invalid") from exc
        return cls.from_dict(decoded)


@dataclass(frozen=True)
class PlanChange:
    """Semantic diff between two contracts.

    ``invalidates_previous_validation`` intentionally mirrors the GoalAnchor
    rule: a semantic plan change expires a previous pass, even if the new
    contract is otherwise valid.
    """

    changed_fields: tuple[PlanChangeField, ...]

    @property
    def changed(self) -> bool:
        return bool(self.changed_fields)

    @property
    def invalidates_previous_validation(self) -> bool:
        return self.changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "changed_fields": [field.value for field in self.changed_fields],
            "invalidates_previous_validation": self.invalidates_previous_validation,
        }


_PLAN_FIELD_COMPARISONS: tuple[tuple[PlanChangeField, str], ...] = (
    (PlanChangeField.OBJECTIVE, "task_objective"),
    (PlanChangeField.REQUIREMENTS, "requirements"),
    (PlanChangeField.CONSTRAINTS, "constraints"),
    (PlanChangeField.CORE_ASSUMPTIONS, "core_assumptions"),
    (PlanChangeField.MAIN_STRATEGY, "main_strategy"),
    (PlanChangeField.ACCEPTANCE, "acceptance_criteria"),
    (PlanChangeField.EVIDENCE_OBLIGATIONS, "evidence_obligations"),
    (PlanChangeField.DELIVERABLE_SCOPE, "deliverables"),
)


_ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.PLAN: frozenset({LifecycleState.VERIFY, LifecycleState.BLOCKED, LifecycleState.REPLAN}),
    LifecycleState.VERIFY: frozenset({LifecycleState.EXECUTE, LifecycleState.BLOCKED, LifecycleState.REPLAN}),
    LifecycleState.EXECUTE: frozenset({LifecycleState.CLOSED, LifecycleState.BLOCKED, LifecycleState.REPLAN}),
    LifecycleState.BLOCKED: frozenset({LifecycleState.REPLAN}),
    LifecycleState.REPLAN: frozenset({LifecycleState.PLAN}),
    LifecycleState.CLOSED: frozenset(),
}

_STATE_ORDER: tuple[LifecycleState, ...] = (
    LifecycleState.PLAN,
    LifecycleState.VERIFY,
    LifecycleState.EXECUTE,
    LifecycleState.BLOCKED,
    LifecycleState.REPLAN,
    LifecycleState.CLOSED,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compare_contracts(before: GoalContract, after: GoalContract) -> PlanChange:
    """Return semantic plan changes; blocker and lifecycle stage are not plan fields."""

    if not isinstance(before, GoalContract) or not isinstance(after, GoalContract):
        raise ContractValidationError("compare_contracts expects two GoalContract values")
    changed: list[PlanChangeField] = []
    for field, attribute in _PLAN_FIELD_COMPARISONS:
        if getattr(before, attribute) != getattr(after, attribute):
            changed.append(field)
    return PlanChange(tuple(changed))


def plan_changed(before: GoalContract, after: GoalContract) -> bool:
    """Convenience predicate for the ``PLAN_CHANGED`` rule."""

    return compare_contracts(before, after).changed


def can_transition(current: LifecycleState, target: LifecycleState) -> bool:
    current_state = _enum_value(current, LifecycleState, "current")
    target_state = _enum_value(target, LifecycleState, "target")
    return target_state in _ALLOWED_TRANSITIONS[current_state]


def transition(
    contract: GoalContract,
    target: LifecycleState,
    *,
    blocking_uncertainty: str | None | object = _MISSING,
) -> GoalContract:
    """Apply one allowed lifecycle transition and revalidate the contract."""

    if not isinstance(contract, GoalContract):
        raise ContractValidationError("transition expects a GoalContract")
    target_state = _enum_value(target, LifecycleState, "target")
    if not can_transition(contract.stage, target_state):
        raise InvalidTransitionError(
            f"cannot transition from {contract.stage.value} to {target_state.value}"
        )
    blocker = (
        contract.current_blocking_uncertainty
        if blocking_uncertainty is _MISSING
        else _optional_text(blocking_uncertainty, "blocking_uncertainty")
    )
    return replace(contract, stage=target_state, current_blocking_uncertainty=blocker)


def validate_contract(contract: GoalContract) -> None:
    """Validate an already constructed contract (useful at API boundaries)."""

    if not isinstance(contract, GoalContract):
        raise ContractValidationError("value is not a GoalContract")
    # Construction performs the full validation; this function is intentionally
    # explicit so callers can use one stable validation entry point.
    contract.__post_init__()


def validate_work_item(work_item: WorkItem) -> None:
    if not isinstance(work_item, WorkItem):
        raise ContractValidationError("value is not a WorkItem")
    work_item.__post_init__()


def allowed_transitions() -> dict[str, tuple[str, ...]]:
    """Return a JSON-friendly copy of the lifecycle transition table."""

    return {
        state.value: tuple(
            target.value for target in sorted(targets, key=_STATE_ORDER.index)
        )
        for state, targets in _ALLOWED_TRANSITIONS.items()
    }
