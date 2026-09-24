"""Deterministic evidence and experiment checks for GoalAnchor stage 3."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .core import ContractValidationError
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


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be a boolean")
    return value


def _positive_number(value: Any, field_name: str, *, allow_none: bool = True) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(f"{field_name} must be a positive number or null")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ContractValidationError(f"{field_name} must be a positive number or null")
    return number


def _non_negative_integer(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractValidationError(f"{field_name} must be an integer >= {minimum}")
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


class EvidenceLevel(str, Enum):
    OBSERVATION = "Observation"
    FACT = "Fact"
    REPRODUCIBLE_EXPERIMENT = "ReproducibleExperiment"
    EXCLUSIONARY_EVIDENCE = "ExclusionaryEvidence"
    ROOT_CAUSE_EVIDENCE = "RootCauseEvidence"


class ConclusionLevel(str, Enum):
    OBSERVATION = "Observation"
    FACT = "Fact"
    ASSOCIATION = "Association"
    CAUSAL = "Causal"
    ROOT_CAUSE = "RootCause"
    DESIGN_NECESSITY = "DesignNecessity"


class EvidenceStatus(str, Enum):
    VALID = "VALID"
    LIMITED = "LIMITED"
    INSUFFICIENT = "INSUFFICIENT"
    CONTRADICTED = "CONTRADICTED"


class AttributionStatus(str, Enum):
    UNASSESSED = "UNASSESSED"
    ATTRIBUTABLE = "ATTRIBUTABLE"
    NOT_ATTRIBUTABLE = "NOT_ATTRIBUTABLE"


_EVIDENCE_RANK = {
    EvidenceLevel.OBSERVATION: 1,
    EvidenceLevel.FACT: 2,
    EvidenceLevel.REPRODUCIBLE_EXPERIMENT: 3,
    EvidenceLevel.EXCLUSIONARY_EVIDENCE: 4,
    EvidenceLevel.ROOT_CAUSE_EVIDENCE: 5,
}
_CONCLUSION_RANK = {
    ConclusionLevel.OBSERVATION: 1,
    ConclusionLevel.FACT: 2,
    ConclusionLevel.ASSOCIATION: 3,
    ConclusionLevel.CAUSAL: 4,
    ConclusionLevel.ROOT_CAUSE: 5,
    ConclusionLevel.DESIGN_NECESSITY: 6,
}
_MAX_CONCLUSION_BY_EVIDENCE = {
    EvidenceLevel.OBSERVATION: ConclusionLevel.OBSERVATION,
    EvidenceLevel.FACT: ConclusionLevel.FACT,
    EvidenceLevel.REPRODUCIBLE_EXPERIMENT: ConclusionLevel.ASSOCIATION,
    EvidenceLevel.EXCLUSIONARY_EVIDENCE: ConclusionLevel.CAUSAL,
    EvidenceLevel.ROOT_CAUSE_EVIDENCE: ConclusionLevel.ROOT_CAUSE,
}


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


@dataclass(frozen=True)
class EvidenceRecord:
    """An evidence item with explicit scope and support boundary."""

    evidence_id: str
    claim: str
    level: EvidenceLevel
    source_ref: tuple[str, ...]
    scope: str
    observation_window_seconds: float | None = None
    event_period_seconds: float | None = None
    controls: tuple[str, ...] = ()
    repetitions: int = 1
    supports: tuple[ConclusionLevel, ...] = ()
    does_not_support: tuple[ConclusionLevel, ...] = ()
    status: EvidenceStatus = EvidenceStatus.VALID
    measured_object: str | None = None
    measurement_value: str | None = None
    measurement_unit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _text(self.evidence_id, "evidence_id"))
        object.__setattr__(self, "claim", _text(self.claim, "claim"))
        object.__setattr__(self, "level", _enum(self.level, EvidenceLevel, "level"))
        object.__setattr__(self, "source_ref", _strings(self.source_ref, "source_ref", required=True))
        object.__setattr__(self, "scope", _text(self.scope, "scope"))
        object.__setattr__(
            self,
            "observation_window_seconds",
            _positive_number(self.observation_window_seconds, "observation_window_seconds"),
        )
        object.__setattr__(
            self,
            "event_period_seconds",
            _positive_number(self.event_period_seconds, "event_period_seconds"),
        )
        object.__setattr__(self, "controls", _strings(self.controls, "controls"))
        object.__setattr__(
            self, "repetitions", _non_negative_integer(self.repetitions, "repetitions", minimum=1)
        )
        supports = tuple(_enum(item, ConclusionLevel, "supports[]") for item in self.supports)
        does_not_support = tuple(
            _enum(item, ConclusionLevel, "does_not_support[]") for item in self.does_not_support
        )
        if set(supports) & set(does_not_support):
            raise ContractValidationError("supports and does_not_support cannot overlap")
        object.__setattr__(self, "supports", supports)
        object.__setattr__(self, "does_not_support", does_not_support)
        object.__setattr__(self, "status", _enum(self.status, EvidenceStatus, "status"))
        for field_name in ("measured_object", "measurement_value", "measurement_unit"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _text(value, field_name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "claim": self.claim,
            "level": self.level.value,
            "source_ref": list(self.source_ref),
            "scope": self.scope,
            "observation_window_seconds": self.observation_window_seconds,
            "event_period_seconds": self.event_period_seconds,
            "controls": list(self.controls),
            "repetitions": self.repetitions,
            "supports": [item.value for item in self.supports],
            "does_not_support": [item.value for item in self.does_not_support],
            "status": self.status.value,
            "measured_object": self.measured_object,
            "measurement_value": self.measurement_value,
            "measurement_unit": self.measurement_unit,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceRecord:
        data = _mapping(payload, "EvidenceRecord")
        allowed = {
            "evidence_id",
            "claim",
            "level",
            "source_ref",
            "scope",
            "observation_window_seconds",
            "event_period_seconds",
            "controls",
            "repetitions",
            "supports",
            "does_not_support",
            "status",
            "measured_object",
            "measurement_value",
            "measurement_unit",
        }
        _reject_unknown(data, allowed, "EvidenceRecord")
        required = {"evidence_id", "claim", "level", "source_ref", "scope"}
        missing = required - set(data)
        if missing:
            raise ContractValidationError(
                f"EvidenceRecord missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            evidence_id=data["evidence_id"],
            claim=data["claim"],
            level=data["level"],
            source_ref=data["source_ref"],
            scope=data["scope"],
            observation_window_seconds=data.get("observation_window_seconds"),
            event_period_seconds=data.get("event_period_seconds"),
            controls=data.get("controls", ()),
            repetitions=data.get("repetitions", 1),
            supports=data.get("supports", ()),
            does_not_support=data.get("does_not_support", ()),
            status=data.get("status", EvidenceStatus.VALID),
            measured_object=data.get("measured_object"),
            measurement_value=data.get("measurement_value"),
            measurement_unit=data.get("measurement_unit"),
        )


@dataclass(frozen=True)
class ExperimentPlan:
    """A bounded, attributable experiment plan."""

    experiment_id: str
    question: str
    variable_under_test: str
    controls: tuple[str, ...]
    metrics: tuple[str, ...]
    observation_window_seconds: float
    repetitions: int
    exit_criterion: str
    decision_links: tuple[str, ...]
    event_period_seconds: float | None = None
    additional_changed_variables: tuple[str, ...] = ()
    declared_attribution: AttributionStatus = AttributionStatus.UNASSESSED

    def __post_init__(self) -> None:
        object.__setattr__(self, "experiment_id", _text(self.experiment_id, "experiment_id"))
        object.__setattr__(self, "question", _text(self.question, "question"))
        object.__setattr__(
            self, "variable_under_test", _text(self.variable_under_test, "variable_under_test")
        )
        object.__setattr__(self, "controls", _strings(self.controls, "controls", required=True))
        object.__setattr__(self, "metrics", _strings(self.metrics, "metrics", required=True))
        window = _positive_number(
            self.observation_window_seconds, "observation_window_seconds", allow_none=False
        )
        object.__setattr__(self, "observation_window_seconds", window)
        object.__setattr__(
            self, "repetitions", _non_negative_integer(self.repetitions, "repetitions", minimum=1)
        )
        object.__setattr__(self, "exit_criterion", _text(self.exit_criterion, "exit_criterion"))
        object.__setattr__(
            self, "decision_links", _strings(self.decision_links, "decision_links", required=True)
        )
        object.__setattr__(
            self,
            "event_period_seconds",
            _positive_number(self.event_period_seconds, "event_period_seconds"),
        )
        object.__setattr__(
            self,
            "additional_changed_variables",
            _strings(self.additional_changed_variables, "additional_changed_variables"),
        )
        object.__setattr__(
            self,
            "declared_attribution",
            _enum(self.declared_attribution, AttributionStatus, "declared_attribution"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "question": self.question,
            "variable_under_test": self.variable_under_test,
            "controls": list(self.controls),
            "metrics": list(self.metrics),
            "observation_window_seconds": self.observation_window_seconds,
            "repetitions": self.repetitions,
            "exit_criterion": self.exit_criterion,
            "decision_links": list(self.decision_links),
            "event_period_seconds": self.event_period_seconds,
            "additional_changed_variables": list(self.additional_changed_variables),
            "declared_attribution": self.declared_attribution.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExperimentPlan:
        data = _mapping(payload, "ExperimentPlan")
        allowed = {
            "experiment_id",
            "question",
            "variable_under_test",
            "controls",
            "metrics",
            "observation_window_seconds",
            "repetitions",
            "exit_criterion",
            "decision_links",
            "event_period_seconds",
            "additional_changed_variables",
            "declared_attribution",
        }
        _reject_unknown(data, allowed, "ExperimentPlan")
        required = {
            "experiment_id",
            "question",
            "variable_under_test",
            "controls",
            "metrics",
            "observation_window_seconds",
            "repetitions",
            "exit_criterion",
            "decision_links",
        }
        missing = required - set(data)
        if missing:
            raise ContractValidationError(
                f"ExperimentPlan missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            experiment_id=data["experiment_id"],
            question=data["question"],
            variable_under_test=data["variable_under_test"],
            controls=data["controls"],
            metrics=data["metrics"],
            observation_window_seconds=data["observation_window_seconds"],
            repetitions=data["repetitions"],
            exit_criterion=data["exit_criterion"],
            decision_links=data["decision_links"],
            event_period_seconds=data.get("event_period_seconds"),
            additional_changed_variables=data.get("additional_changed_variables", ()),
            declared_attribution=data.get("declared_attribution", AttributionStatus.UNASSESSED),
        )


@dataclass(frozen=True)
class DesignDecision:
    """Candidate designs and the explicit proof needed to call one necessary."""

    decision_id: str
    problem_link: str
    acceptance_link: str
    candidate_options: tuple[str, ...]
    rejected_options: tuple[str, ...] = ()
    necessary_conditions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    selected_option: str | None = None
    necessary_conditions_proven: bool = False
    selection_rationale: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _text(self.decision_id, "decision_id"))
        object.__setattr__(self, "problem_link", _text(self.problem_link, "problem_link"))
        object.__setattr__(self, "acceptance_link", _text(self.acceptance_link, "acceptance_link"))
        object.__setattr__(
            self, "candidate_options", _strings(self.candidate_options, "candidate_options", required=True)
        )
        object.__setattr__(self, "rejected_options", _strings(self.rejected_options, "rejected_options"))
        object.__setattr__(
            self,
            "necessary_conditions",
            _strings(self.necessary_conditions, "necessary_conditions"),
        )
        object.__setattr__(self, "evidence_refs", _strings(self.evidence_refs, "evidence_refs"))
        if self.selected_option is not None:
            object.__setattr__(self, "selected_option", _text(self.selected_option, "selected_option"))
        object.__setattr__(
            self,
            "necessary_conditions_proven",
            _bool(self.necessary_conditions_proven, "necessary_conditions_proven"),
        )
        if self.selection_rationale is not None:
            object.__setattr__(
                self, "selection_rationale", _text(self.selection_rationale, "selection_rationale")
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "problem_link": self.problem_link,
            "acceptance_link": self.acceptance_link,
            "candidate_options": list(self.candidate_options),
            "rejected_options": list(self.rejected_options),
            "necessary_conditions": list(self.necessary_conditions),
            "evidence_refs": list(self.evidence_refs),
            "selected_option": self.selected_option,
            "necessary_conditions_proven": self.necessary_conditions_proven,
            "selection_rationale": self.selection_rationale,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DesignDecision:
        data = _mapping(payload, "DesignDecision")
        allowed = {
            "decision_id",
            "problem_link",
            "acceptance_link",
            "candidate_options",
            "rejected_options",
            "necessary_conditions",
            "evidence_refs",
            "selected_option",
            "necessary_conditions_proven",
            "selection_rationale",
        }
        _reject_unknown(data, allowed, "DesignDecision")
        required = {"decision_id", "problem_link", "acceptance_link", "candidate_options"}
        missing = required - set(data)
        if missing:
            raise ContractValidationError(
                f"DesignDecision missing required field(s): {', '.join(sorted(missing))}"
            )
        return cls(
            decision_id=data["decision_id"],
            problem_link=data["problem_link"],
            acceptance_link=data["acceptance_link"],
            candidate_options=data["candidate_options"],
            rejected_options=data.get("rejected_options", ()),
            necessary_conditions=data.get("necessary_conditions", ()),
            evidence_refs=data.get("evidence_refs", ()),
            selected_option=data.get("selected_option"),
            necessary_conditions_proven=data.get("necessary_conditions_proven", False),
            selection_rationale=data.get("selection_rationale"),
        )


def _failed(
    gate_id: GateId,
    code: str,
    message: str,
    *,
    field: str | None = None,
    next_state=None,
) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        passed=False,
        findings=(GateFinding(code=code, message=message, field=field),),
        next_state=next_state,
    )


def _passed(gate_id: GateId) -> GateResult:
    return GateResult(gate_id=gate_id, passed=True)


def check_observation_window(
    *,
    observation_window_seconds: float | None,
    event_period_seconds: float | None,
    gate_id: GateId = GateId.EVIDENCE,
) -> GateResult:
    """Reject a claimed absence/coverage result when the window is too short."""

    if event_period_seconds is None:
        return _failed(
            gate_id,
            "WINDOW_UNVERIFIED",
            "event period is unknown; observation coverage cannot be established",
            field="event_period_seconds",
        )
    if observation_window_seconds is None:
        return _failed(
            gate_id,
            "WINDOW_MISSING",
            "observation window is required when an event period is relevant",
            field="observation_window_seconds",
        )
    if observation_window_seconds < event_period_seconds:
        return _failed(
            gate_id,
            "INSUFFICIENT_WINDOW",
            "observation window is shorter than the event period; absence is not established",
            field="observation_window_seconds",
        )
    return _passed(gate_id)


def check_evidence(record: EvidenceRecord) -> GateResult:
    """Check provenance fields, declared support boundary, and evidence status."""

    if not isinstance(record, EvidenceRecord):
        return _failed(GateId.EVIDENCE, "INVALID_EVIDENCE", "EvidenceRecord is required")
    if record.status is EvidenceStatus.CONTRADICTED:
        return _failed(
            GateId.EVIDENCE,
            "EVIDENCE_CONTRADICTED",
            "contradicted evidence cannot support a stronger conclusion",
            field="status",
        )
    if record.status is EvidenceStatus.INSUFFICIENT:
        return _failed(
            GateId.EVIDENCE,
            "EVIDENCE_INSUFFICIENT",
            "evidence is explicitly insufficient for a decision",
            field="status",
        )
    if record.event_period_seconds is not None:
        window_result = check_observation_window(
            observation_window_seconds=record.observation_window_seconds,
            event_period_seconds=record.event_period_seconds,
            gate_id=GateId.EVIDENCE,
        )
        if not window_result.passed:
            return window_result
    if record.status is EvidenceStatus.LIMITED and any(
        _CONCLUSION_RANK[level] > _CONCLUSION_RANK[ConclusionLevel.OBSERVATION]
        for level in record.supports
    ):
        return _failed(
            GateId.EVIDENCE,
            "LIMITED_SUPPORT_TOO_STRONG",
            "LIMITED evidence may not declare support above Observation",
            field="supports",
        )
    if any(
        _CONCLUSION_RANK[level] > _CONCLUSION_RANK[_MAX_CONCLUSION_BY_EVIDENCE[record.level]]
        for level in record.supports
    ):
        return _failed(
            GateId.EVIDENCE,
            "SUPPORTS_EXCEEDS_LEVEL",
            "declared supported conclusion exceeds the evidence level",
            field="supports",
        )
    return _passed(GateId.EVIDENCE)


def check_conclusion(
    conclusion: ConclusionLevel, evidence: Sequence[EvidenceRecord]
) -> GateResult:
    """Ensure a conclusion is explicitly supported and not stronger than evidence."""

    requested = _enum(conclusion, ConclusionLevel, "conclusion")
    records = tuple(evidence)
    if not records:
        return _failed(
            GateId.CONCLUSION,
            "EVIDENCE_REQUIRED",
            "a conclusion requires at least one evidence record",
            field="evidence",
        )
    if requested is ConclusionLevel.DESIGN_NECESSITY:
        return _failed(
            GateId.CONCLUSION,
            "DESIGN_NECESSITY_REQUIRES_SEPARATE_CHECK",
            "design necessity must pass the separate design-necessity check",
            field="conclusion",
        )
    for record in records:
        evidence_result = check_evidence(record)
        if not evidence_result.passed:
            continue
        allowed = _MAX_CONCLUSION_BY_EVIDENCE[record.level]
        declared_support = any(
            _CONCLUSION_RANK[level] >= _CONCLUSION_RANK[requested]
            for level in record.supports
        )
        if (
            declared_support
            and _CONCLUSION_RANK[requested] <= _CONCLUSION_RANK[allowed]
            and requested not in record.does_not_support
        ):
            return _passed(GateId.CONCLUSION)
    return _failed(
        GateId.CONCLUSION,
        "CONCLUSION_EXCEEDS_EVIDENCE",
        f"evidence does not explicitly support conclusion {requested.value}",
        field="conclusion",
    )


def check_experiment(plan: ExperimentPlan) -> GateResult:
    """Check controls, single-variable attribution, window, and decision linkage."""

    if not isinstance(plan, ExperimentPlan):
        return _failed(GateId.EXPERIMENT, "INVALID_EXPERIMENT", "ExperimentPlan is required")
    if plan.additional_changed_variables:
        return _failed(
            GateId.EXPERIMENT,
            "NOT_ATTRIBUTABLE",
            "experiment changes multiple core variables and cannot identify the cause",
            field="additional_changed_variables",
        )
    if plan.declared_attribution is AttributionStatus.NOT_ATTRIBUTABLE:
        return _failed(
            GateId.EXPERIMENT,
            "NOT_ATTRIBUTABLE",
            "experiment is explicitly marked not attributable",
            field="declared_attribution",
        )
    window = check_observation_window(
        observation_window_seconds=plan.observation_window_seconds,
        event_period_seconds=plan.event_period_seconds,
        gate_id=GateId.EXPERIMENT,
    )
    if plan.event_period_seconds is not None and not window.passed:
        return window
    return _passed(GateId.EXPERIMENT)


def check_design_necessity(decision: DesignDecision) -> GateResult:
    """Keep rejected alternatives separate from proof that a selected design is necessary."""

    if not isinstance(decision, DesignDecision):
        return _failed(
            GateId.DESIGN_NECESSITY,
            "INVALID_DESIGN_DECISION",
            "DesignDecision is required",
        )
    if decision.selected_option is None:
        return _passed(GateId.DESIGN_NECESSITY)
    if decision.selected_option not in decision.candidate_options:
        return _failed(
            GateId.DESIGN_NECESSITY,
            "SELECTED_OPTION_NOT_CANDIDATE",
            "selected option must be one of candidate_options",
            field="selected_option",
        )
    if not decision.necessary_conditions:
        return _failed(
            GateId.DESIGN_NECESSITY,
            "NECESSARY_CONDITIONS_MISSING",
            "selecting a necessary design requires explicit necessary_conditions",
            field="necessary_conditions",
        )
    if not decision.evidence_refs:
        return _failed(
            GateId.DESIGN_NECESSITY,
            "NECESSITY_EVIDENCE_MISSING",
            "necessary conditions require evidence references",
            field="evidence_refs",
        )
    if not decision.necessary_conditions_proven:
        return _failed(
            GateId.DESIGN_NECESSITY,
            "NECESSITY_NOT_PROVEN",
            "candidate selection and rejected alternatives do not prove necessity",
            field="necessary_conditions_proven",
        )
    if decision.selection_rationale is None:
        return _failed(
            GateId.DESIGN_NECESSITY,
            "SELECTION_RATIONALE_MISSING",
            "a necessary design requires an explicit selection rationale",
            field="selection_rationale",
        )
    return _passed(GateId.DESIGN_NECESSITY)


__all__ = [
    "AttributionStatus",
    "ConclusionLevel",
    "DesignDecision",
    "EvidenceLevel",
    "EvidenceRecord",
    "EvidenceStatus",
    "ExperimentPlan",
    "check_conclusion",
    "check_design_necessity",
    "check_evidence",
    "check_experiment",
    "check_observation_window",
]
