"""Deterministic GoalAnchor stage-2 gate checks.

The gates only validate whether a plan or work item contains the information
needed for the next safe decision.  They do not infer root cause, evidence
strength, or artifact provenance; those are later-stage concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from .core import (
    ContractValidationError,
    GoalContract,
    LifecycleState,
    WorkItem,
    compare_contracts,
)


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{field_name} must be an array of strings")
    result = tuple(_text(item, f"{field_name}[]") for item in value)
    return result


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be a boolean")
    return value


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


class GateId(str, Enum):
    Q1_GOAL = "Q1 Goal"
    Q2_LOGIC = "Q2 Logic"
    Q3_PATH = "Q3 Path"
    Q4_MINIMUM = "Q4 Minimum"
    Q5_STRUCTURE = "Q5 Structure"
    VERIFY = "VERIFY"
    EXECUTE = "EXECUTE"
    PLAN_CHANGED = "PLAN_CHANGED"
    EVIDENCE = "EVIDENCE"
    CONCLUSION = "CONCLUSION"
    EXPERIMENT = "EXPERIMENT"
    DESIGN_NECESSITY = "DESIGN_NECESSITY"
    ARTIFACT = "ARTIFACT"
    PROVENANCE = "PROVENANCE"
    CLOSURE = "CLOSURE"
    REVIEW_INTEGRATION = "REVIEW_INTEGRATION"
    GRAPHIFY_INTEGRATION = "GRAPHIFY_INTEGRATION"


@dataclass(frozen=True)
class GateFinding:
    """One observable failure and the field/decision it concerns."""

    code: str
    message: str
    field: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _text(self.code, "code"))
        object.__setattr__(self, "message", _text(self.message, "message"))
        if self.field is not None:
            object.__setattr__(self, "field", _text(self.field, "field"))

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.field is not None:
            result["field"] = self.field
        return result


@dataclass(frozen=True)
class GateResult:
    """Result of one gate; failed gates carry the smallest next-state hint."""

    gate_id: GateId
    passed: bool
    findings: tuple[GateFinding, ...] = ()
    next_state: LifecycleState | None = None
    recommended_level: str | None = None

    def __post_init__(self) -> None:
        gate = self.gate_id
        if not isinstance(gate, GateId):
            try:
                gate = GateId(gate)
            except (TypeError, ValueError) as exc:
                raise ContractValidationError("gate_id is invalid") from exc
            object.__setattr__(self, "gate_id", gate)
        if not isinstance(self.passed, bool):
            raise ContractValidationError("passed must be a boolean")
        findings = tuple(self.findings)
        if any(not isinstance(item, GateFinding) for item in findings):
            raise ContractValidationError("findings must contain GateFinding values")
        object.__setattr__(self, "findings", findings)
        if self.passed and findings:
            raise ContractValidationError("a passed gate cannot contain findings")
        if not self.passed and not findings:
            raise ContractValidationError("a failed gate must contain findings")
        if self.next_state is not None and not isinstance(self.next_state, LifecycleState):
            try:
                object.__setattr__(self, "next_state", LifecycleState(self.next_state))
            except (TypeError, ValueError) as exc:
                raise ContractValidationError("next_state is invalid") from exc
        if self.recommended_level is not None:
            object.__setattr__(
                self, "recommended_level", _text(self.recommended_level, "recommended_level")
            )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "gate_id": self.gate_id.value,
            "passed": self.passed,
            "findings": [item.to_dict() for item in self.findings],
        }
        if self.next_state is not None:
            result["next_state"] = self.next_state.value
        if self.recommended_level is not None:
            result["recommended_level"] = self.recommended_level
        return result


@dataclass(frozen=True)
class GateReport:
    """Aggregate report for the five PLAN gates."""

    results: tuple[GateResult, ...]

    def __post_init__(self) -> None:
        results = tuple(self.results)
        if any(not isinstance(item, GateResult) for item in results):
            raise ContractValidationError("results must contain GateResult values")
        object.__setattr__(self, "results", results)

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(item.passed for item in self.results)

    @property
    def failed_gate_ids(self) -> tuple[str, ...]:
        return tuple(item.gate_id.value for item in self.results if not item.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed_gate_ids": list(self.failed_gate_ids),
            "results": [item.to_dict() for item in self.results],
        }


@dataclass(frozen=True)
class VerifyRecord:
    """The three pieces required before leaving VERIFY."""

    verification_object: str
    decision_significance: str
    exit_criterion: str
    observable: bool = True
    finite: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "verification_object", _text(self.verification_object, "verification_object")
        )
        object.__setattr__(
            self,
            "decision_significance",
            _text(self.decision_significance, "decision_significance"),
        )
        object.__setattr__(self, "exit_criterion", _text(self.exit_criterion, "exit_criterion"))
        object.__setattr__(self, "observable", _bool(self.observable, "observable"))
        object.__setattr__(self, "finite", _bool(self.finite, "finite"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_object": self.verification_object,
            "decision_significance": self.decision_significance,
            "exit_criterion": self.exit_criterion,
            "observable": self.observable,
            "finite": self.finite,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VerifyRecord":
        data = _mapping(payload, "VerifyRecord")
        allowed = {
            "verification_object",
            "decision_significance",
            "exit_criterion",
            "observable",
            "finite",
        }
        _reject_unknown(data, allowed, "VerifyRecord")
        required = {"verification_object", "decision_significance", "exit_criterion"}
        missing = required - set(data)
        if missing:
            raise ContractValidationError(
                f"VerifyRecord missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            verification_object=data["verification_object"],
            decision_significance=data["decision_significance"],
            exit_criterion=data["exit_criterion"],
            observable=data.get("observable", True),
            finite=data.get("finite", True),
        )


@dataclass(frozen=True)
class ExecutePreflight:
    """Minimum preflight record before a contract can enter EXECUTE."""

    plan_state: LifecycleState
    verify_passed: bool
    constraints_acknowledged: bool
    change_scope: str
    recovery_path: str

    def __post_init__(self) -> None:
        state = self.plan_state
        if not isinstance(state, LifecycleState):
            try:
                state = LifecycleState(state)
            except (TypeError, ValueError) as exc:
                raise ContractValidationError("plan_state is invalid") from exc
            object.__setattr__(self, "plan_state", state)
        object.__setattr__(self, "verify_passed", _bool(self.verify_passed, "verify_passed"))
        object.__setattr__(
            self,
            "constraints_acknowledged",
            _bool(self.constraints_acknowledged, "constraints_acknowledged"),
        )
        object.__setattr__(self, "change_scope", _text(self.change_scope, "change_scope"))
        object.__setattr__(self, "recovery_path", _text(self.recovery_path, "recovery_path"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_state": self.plan_state.value,
            "verify_passed": self.verify_passed,
            "constraints_acknowledged": self.constraints_acknowledged,
            "change_scope": self.change_scope,
            "recovery_path": self.recovery_path,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutePreflight":
        data = _mapping(payload, "ExecutePreflight")
        allowed = {
            "plan_state",
            "verify_passed",
            "constraints_acknowledged",
            "change_scope",
            "recovery_path",
        }
        _reject_unknown(data, allowed, "ExecutePreflight")
        missing = allowed - set(data)
        if missing:
            raise ContractValidationError(
                f"ExecutePreflight missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            plan_state=data["plan_state"],
            verify_passed=data["verify_passed"],
            constraints_acknowledged=data["constraints_acknowledged"],
            change_scope=data["change_scope"],
            recovery_path=data["recovery_path"],
        )


@dataclass(frozen=True)
class StructureReview:
    """Explicit structural observations used by Q5."""

    responsibility_keys: tuple[str, ...] = ()
    coupling_risks: tuple[str, ...] = ()
    public_path_hacks: tuple[str, ...] = ()
    unnecessary_regression_surface: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "responsibility_keys",
            "coupling_risks",
            "public_path_hacks",
            "unnecessary_regression_surface",
        ):
            object.__setattr__(self, field_name, _strings(getattr(self, field_name), field_name))

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "responsibility_keys": list(self.responsibility_keys),
            "coupling_risks": list(self.coupling_risks),
            "public_path_hacks": list(self.public_path_hacks),
            "unnecessary_regression_surface": list(self.unnecessary_regression_surface),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StructureReview":
        data = _mapping(payload, "StructureReview")
        allowed = {
            "responsibility_keys",
            "coupling_risks",
            "public_path_hacks",
            "unnecessary_regression_surface",
        }
        _reject_unknown(data, allowed, "StructureReview")
        return cls(
            responsibility_keys=data.get("responsibility_keys", ()),
            coupling_risks=data.get("coupling_risks", ()),
            public_path_hacks=data.get("public_path_hacks", ()),
            unnecessary_regression_surface=data.get("unnecessary_regression_surface", ()),
        )


def _passed(gate_id: GateId) -> GateResult:
    return GateResult(gate_id=gate_id, passed=True)


def _failed(
    gate_id: GateId,
    code: str,
    message: str,
    *,
    field: str | None = None,
    next_state: LifecycleState | None = LifecycleState.BLOCKED,
) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        passed=False,
        findings=(GateFinding(code=code, message=message, field=field),),
        next_state=next_state,
    )


def check_q1_goal(contract: GoalContract) -> GateResult:
    """Ensure the goal, blocker, constraints, and completion are distinct."""

    if not isinstance(contract, GoalContract):
        return _failed(GateId.Q1_GOAL, "INVALID_CONTRACT", "GoalContract is required")
    blocker = contract.current_blocking_uncertainty
    if blocker is not None and blocker.casefold() == contract.task_objective.casefold():
        return _failed(
            GateId.Q1_GOAL,
            "BLOCKER_REPLACES_GOAL",
            "current_blocking_uncertainty must not replace task_objective",
            field="current_blocking_uncertainty",
        )
    if not contract.acceptance_criteria:
        return _failed(
            GateId.Q1_GOAL,
            "MISSING_ACCEPTANCE",
            "at least one acceptance criterion is required",
            field="acceptance_criteria",
        )
    if not contract.deliverables:
        return _failed(
            GateId.Q1_GOAL,
            "MISSING_DELIVERABLE",
            "at least one deliverable is required",
            field="deliverables",
        )
    return _passed(GateId.Q1_GOAL)


def check_q2_logic(work_items: Sequence[WorkItem]) -> GateResult:
    """Require issue → action → result → decision impact for each action."""

    items = tuple(work_items)
    if not items:
        return _failed(
            GateId.Q2_LOGIC,
            "MISSING_WORK_ITEM",
            "at least one WorkItem is required to establish a causal chain",
            field="work_items",
        )
    for item in items:
        if not isinstance(item, WorkItem):
            return _failed(GateId.Q2_LOGIC, "INVALID_WORK_ITEM", "work_items contains an invalid value")
        if item.expected_result is None:
            return _failed(
                GateId.Q2_LOGIC,
                "MISSING_EXPECTED_RESULT",
                f"{item.item_id} must define the expected observable result",
                field=f"work_items.{item.item_id}.expected_result",
            )
        if not item.expected_decision_impact:
            return _failed(
                GateId.Q2_LOGIC,
                "MISSING_DECISION_IMPACT",
                f"{item.item_id} must state how the result changes a decision",
                field=f"work_items.{item.item_id}.expected_decision_impact",
            )
    return _passed(GateId.Q2_LOGIC)


def check_q3_path(work_items: Sequence[WorkItem]) -> GateResult:
    """Reject unverified premises and stacked unverified patch references."""

    for item in tuple(work_items):
        if not isinstance(item, WorkItem):
            return _failed(GateId.Q3_PATH, "INVALID_WORK_ITEM", "work_items contains an invalid value")
        if item.assumptions and not item.assumptions_verified:
            return _failed(
                GateId.Q3_PATH,
                "UNVERIFIED_ASSUMPTION",
                f"{item.item_id} contains assumptions without confirmation",
                field=f"work_items.{item.item_id}.assumptions",
            )
        if item.unverified_patch_refs:
            return _failed(
                GateId.Q3_PATH,
                "UNVERIFIED_PATCH",
                f"{item.item_id} depends on unverified patch/workaround references",
                field=f"work_items.{item.item_id}.unverified_patch_refs",
            )
    return _passed(GateId.Q3_PATH)


def check_q4_minimum(work_items: Sequence[WorkItem]) -> GateResult:
    """Require every action to name the implementation/acceptance decision it affects."""

    for item in tuple(work_items):
        if not isinstance(item, WorkItem):
            return _failed(GateId.Q4_MINIMUM, "INVALID_WORK_ITEM", "work_items contains an invalid value")
        if item.decision_link is None:
            return _failed(
                GateId.Q4_MINIMUM,
                "MISSING_DECISION_LINK",
                f"{item.item_id} is not linked to an implementation or acceptance decision",
                field=f"work_items.{item.item_id}.decision_link",
            )
    return _passed(GateId.Q4_MINIMUM)


def check_q5_structure(
    work_items: Sequence[WorkItem], structure_review: StructureReview | None
) -> GateResult:
    """Reject explicit duplicate responsibility or structural risk findings."""

    if structure_review is None:
        return _failed(
            GateId.Q5_STRUCTURE,
            "STRUCTURE_REVIEW_REQUIRED",
            "Q5 requires an explicit structural review record",
            field="structure_review",
        )
    items = tuple(work_items)
    item_ids = [item.item_id for item in items if isinstance(item, WorkItem)]
    if len(item_ids) != len(set(item_ids)):
        return _failed(
            GateId.Q5_STRUCTURE,
            "DUPLICATE_WORK_ITEM_ID",
            "work item IDs must be unique",
            field="work_items.item_id",
        )
    seen: set[str] = set()
    duplicate_responsibility = []
    for key in structure_review.responsibility_keys:
        if key in seen:
            duplicate_responsibility.append(key)
        seen.add(key)
    if duplicate_responsibility:
        return _failed(
            GateId.Q5_STRUCTURE,
            "DUPLICATE_RESPONSIBILITY",
            "multiple items claim the same responsibility: "
            + ", ".join(duplicate_responsibility),
            field="structure_review.responsibility_keys",
        )
    for field_name, label in (
        ("coupling_risks", "implicit coupling"),
        ("public_path_hacks", "public-path hack"),
        ("unnecessary_regression_surface", "unnecessary regression surface"),
    ):
        values = getattr(structure_review, field_name)
        if values:
            return _failed(
                GateId.Q5_STRUCTURE,
                "STRUCTURE_RISK",
                f"structural review reports {label}: " + ", ".join(values),
                field=f"structure_review.{field_name}",
            )
    return _passed(GateId.Q5_STRUCTURE)


def evaluate_plan(
    contract: GoalContract,
    work_items: Sequence[WorkItem],
    structure_review: StructureReview | None,
) -> GateReport:
    """Run Q1–Q5 in fixed order."""

    results = (
        check_q1_goal(contract),
        check_q2_logic(work_items),
        check_q3_path(work_items),
        check_q4_minimum(work_items),
        check_q5_structure(work_items, structure_review),
    )
    return GateReport(results)


def check_verify(record: VerifyRecord | None) -> GateResult:
    """Require verification object, decision significance, and finite observability."""

    if record is None:
        return _failed(
            GateId.VERIFY,
            "VERIFY_RECORD_REQUIRED",
            "verification_object, decision_significance, and exit_criterion are required",
            field="verify",
        )
    if not record.observable:
        return _failed(
            GateId.VERIFY,
            "EXIT_NOT_OBSERVABLE",
            "exit_criterion must be observable",
            field="exit_criterion",
        )
    if not record.finite:
        return _failed(
            GateId.VERIFY,
            "EXIT_NOT_FINITE",
            "exit_criterion must be finite",
            field="exit_criterion",
        )
    return _passed(GateId.VERIFY)


def check_execute(
    contract: GoalContract, preflight: ExecutePreflight | None
) -> GateResult:
    """Check state, VERIFY result, constraints, change scope, and recovery path."""

    if not isinstance(contract, GoalContract):
        return _failed(GateId.EXECUTE, "INVALID_CONTRACT", "GoalContract is required")
    if preflight is None:
        return _failed(
            GateId.EXECUTE,
            "EXECUTE_PREFLIGHT_REQUIRED",
            "execute preflight is required",
            field="execute",
        )
    if contract.stage is not LifecycleState.VERIFY or preflight.plan_state is not LifecycleState.VERIFY:
        return _failed(
            GateId.EXECUTE,
            "VERIFY_STATE_REQUIRED",
            "EXECUTE requires the contract and preflight state to be VERIFY",
            field="stage",
        )
    if not preflight.verify_passed:
        return _failed(
            GateId.EXECUTE,
            "VERIFY_NOT_PASSED",
            "EXECUTE requires a passed VERIFY gate",
            field="verify_passed",
        )
    if not preflight.constraints_acknowledged:
        return _failed(
            GateId.EXECUTE,
            "CONSTRAINTS_NOT_ACKNOWLEDGED",
            "all contract constraints must be acknowledged before EXECUTE",
            field="constraints_acknowledged",
        )
    return _passed(GateId.EXECUTE)


def check_plan_change(before: GoalContract, after: GoalContract) -> GateResult:
    """Mark semantic plan changes and require the REPLAN → PLAN path."""

    diff = compare_contracts(before, after)
    if not diff.changed:
        return _passed(GateId.PLAN_CHANGED)
    fields = ", ".join(field.value for field in diff.changed_fields)
    return _failed(
        GateId.PLAN_CHANGED,
        "PLAN_CHANGED",
        f"semantic plan change detected in: {fields}; previous validation is invalid",
        field="changed_fields",
        next_state=LifecycleState.REPLAN,
    )


__all__ = [
    "ExecutePreflight",
    "GateFinding",
    "GateId",
    "GateReport",
    "GateResult",
    "StructureReview",
    "VerifyRecord",
    "check_execute",
    "check_plan_change",
    "check_q1_goal",
    "check_q2_logic",
    "check_q3_path",
    "check_q4_minimum",
    "check_q5_structure",
    "check_verify",
    "evaluate_plan",
]
