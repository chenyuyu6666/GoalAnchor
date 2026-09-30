from __future__ import annotations

import json
from pathlib import Path

import pytest

from goalanchor import (
    AttributionStatus,
    ConclusionLevel,
    DesignDecision,
    EvidenceLevel,
    EvidenceRecord,
    EvidenceStatus,
    ExperimentPlan,
    check_conclusion,
    check_design_necessity,
    check_evidence,
    check_experiment,
    check_observation_window,
)
from goalanchor.compat import main


def make_evidence(**overrides) -> EvidenceRecord:
    values = {
        "evidence_id": "E1",
        "claim": "Snapshot API wall-time is 343–532 ms",
        "level": EvidenceLevel.REPRODUCIBLE_EXPERIMENT,
        "source_ref": ("log:E1", "source:display.c:42"),
        "scope": "LVGL handler during page swipe",
        "observation_window_seconds": 30,
        "event_period_seconds": 10,
        "controls": ("resolution=720x720", "ARGB8888"),
        "repetitions": 3,
        "supports": (ConclusionLevel.ASSOCIATION,),
        "does_not_support": (ConclusionLevel.ROOT_CAUSE,),
        "status": EvidenceStatus.VALID,
        "measured_object": "snapshot_take_to_draw_buf",
        "measurement_value": "343–532",
        "measurement_unit": "ms",
    }
    values.update(overrides)
    return EvidenceRecord(**values)


def make_experiment(**overrides) -> ExperimentPlan:
    values = {
        "experiment_id": "X1",
        "question": "Does timer scan add measurable handler latency?",
        "variable_under_test": "timer_scan",
        "controls": ("snapshot_capture=off", "same firmware", "same input trace"),
        "metrics": ("handler_duration", "frame_interval", "input_latency"),
        "observation_window_seconds": 30,
        "repetitions": 3,
        "exit_criterion": "three runs report the median handler duration",
        "decision_links": ("choose timer or snapshot optimization",),
        "event_period_seconds": 10,
        "declared_attribution": AttributionStatus.ATTRIBUTABLE,
    }
    values.update(overrides)
    return ExperimentPlan(**values)


def make_design(**overrides) -> DesignDecision:
    values = {
        "decision_id": "D1",
        "problem_link": "page-swipe-latency",
        "acceptance_link": "REQ-UI-01",
        "candidate_options": ("keep-snapshot", "tile-view"),
        "rejected_options": ("timer-only-fix",),
        "necessary_conditions": ("preserve video state", "meet frame latency"),
        "evidence_refs": ("E1", "X1"),
        "selected_option": "tile-view",
        "necessary_conditions_proven": True,
        "selection_rationale": "Only this candidate meets both measured conditions.",
    }
    values.update(overrides)
    return DesignDecision(**values)


def test_evidence_round_trip_preserves_measurement_boundary() -> None:
    record = make_evidence()
    assert EvidenceRecord.from_dict(record.to_dict()) == record
    assert json.loads(json.dumps(record.to_dict(), ensure_ascii=False))["level"] == (
        "ReproducibleExperiment"
    )


def test_evidence_rejects_contradicted_and_insufficient_status() -> None:
    contradicted = check_evidence(make_evidence(status=EvidenceStatus.CONTRADICTED))
    insufficient = check_evidence(make_evidence(status=EvidenceStatus.INSUFFICIENT))
    assert not contradicted.passed
    assert contradicted.findings[0].code == "EVIDENCE_CONTRADICTED"
    assert not insufficient.passed
    assert insufficient.findings[0].code == "EVIDENCE_INSUFFICIENT"


def test_limited_evidence_cannot_claim_more_than_observation() -> None:
    result = check_evidence(
        make_evidence(status=EvidenceStatus.LIMITED, supports=(ConclusionLevel.CAUSAL,))
    )
    assert not result.passed
    assert result.findings[0].code == "LIMITED_SUPPORT_TOO_STRONG"


def test_declared_support_cannot_exceed_evidence_level() -> None:
    result = check_evidence(
        make_evidence(
            level=EvidenceLevel.FACT,
            supports=(ConclusionLevel.ASSOCIATION,),
            does_not_support=(),
        )
    )
    assert not result.passed
    assert result.findings[0].code == "SUPPORTS_EXCEEDS_LEVEL"


def test_observation_window_rejects_pseudo_negative() -> None:
    short = check_observation_window(
        observation_window_seconds=11,
        event_period_seconds=300,
    )
    missing_period = check_observation_window(
        observation_window_seconds=11,
        event_period_seconds=None,
    )
    enough = check_observation_window(
        observation_window_seconds=300,
        event_period_seconds=300,
    )
    assert not short.passed
    assert short.findings[0].code == "INSUFFICIENT_WINDOW"
    assert not missing_period.passed
    assert missing_period.findings[0].code == "WINDOW_UNVERIFIED"
    assert enough.passed

    evidence = check_evidence(
        make_evidence(observation_window_seconds=11, event_period_seconds=300)
    )
    assert not evidence.passed
    assert evidence.findings[0].code == "INSUFFICIENT_WINDOW"


def test_conclusion_strength_is_bounded_by_evidence() -> None:
    assert check_conclusion(ConclusionLevel.OBSERVATION, (make_evidence(),)).passed
    assert check_conclusion(ConclusionLevel.ASSOCIATION, (make_evidence(),)).passed
    causal = check_conclusion(ConclusionLevel.CAUSAL, (make_evidence(),))
    assert not causal.passed
    assert causal.findings[0].code == "CONCLUSION_EXCEEDS_EVIDENCE"

    exclusion = make_evidence(
        evidence_id="E2",
        level=EvidenceLevel.EXCLUSIONARY_EVIDENCE,
        supports=(ConclusionLevel.CAUSAL,),
        does_not_support=(),
    )
    assert check_conclusion(ConclusionLevel.CAUSAL, (exclusion,)).passed


def test_root_cause_requires_root_cause_evidence() -> None:
    root = make_evidence(
        evidence_id="E3",
        level=EvidenceLevel.ROOT_CAUSE_EVIDENCE,
        supports=(ConclusionLevel.ROOT_CAUSE,),
        does_not_support=(),
    )
    assert check_conclusion(ConclusionLevel.ROOT_CAUSE, (root,)).passed
    assert not check_conclusion(ConclusionLevel.DESIGN_NECESSITY, (root,)).passed


def test_experiment_rejects_mixed_variables_and_short_window() -> None:
    mixed = check_experiment(make_experiment(additional_changed_variables=("print_work",)))
    short = check_experiment(make_experiment(observation_window_seconds=5))
    assert not mixed.passed
    assert mixed.findings[0].code == "NOT_ATTRIBUTABLE"
    assert not short.passed
    assert short.findings[0].code == "INSUFFICIENT_WINDOW"
    assert check_experiment(make_experiment(event_period_seconds=None)).passed


def test_design_rejection_does_not_prove_necessity() -> None:
    no_conditions = check_design_necessity(
        make_design(necessary_conditions=(), necessary_conditions_proven=True)
    )
    not_proven = check_design_necessity(make_design(necessary_conditions_proven=False))
    no_rationale = check_design_necessity(make_design(selection_rationale=None))
    assert not no_conditions.passed
    assert no_conditions.findings[0].code == "NECESSARY_CONDITIONS_MISSING"
    assert not not_proven.passed
    assert not_proven.findings[0].code == "NECESSITY_NOT_PROVEN"
    assert not no_rationale.passed
    assert no_rationale.findings[0].code == "SELECTION_RATIONALE_MISSING"
    assert check_design_necessity(make_design()).passed


def test_design_selection_must_be_a_candidate() -> None:
    result = check_design_necessity(make_design(selected_option="invented-option"))
    assert not result.passed
    assert result.findings[0].code == "SELECTED_OPTION_NOT_CANDIDATE"


def test_evidence_cli_reports_stronger_conclusion_as_failure(tmp_path: Path, capsys) -> None:
    record = {
        "conclusion_level": "Causal",
        "evidence": [make_evidence().to_dict()],
    }
    path = tmp_path / "conclusion.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    assert main(["gate", "--phase", "conclusion", "--record", str(path)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["findings"][0]["code"] == "CONCLUSION_EXCEEDS_EVIDENCE"


def test_experiment_cli_accepts_attributable_plan(tmp_path: Path, capsys) -> None:
    path = tmp_path / "experiment.json"
    path.write_text(
        json.dumps({"experiment": make_experiment().to_dict()}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert main(["gate", "--phase", "experiment", "--record", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True


def test_design_cli_rejects_a_without_b_necessity(tmp_path: Path, capsys) -> None:
    path = tmp_path / "design.json"
    path.write_text(
        json.dumps(
            {"design": make_design(necessary_conditions_proven=False).to_dict()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert main(["gate", "--phase", "design", "--record", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["findings"][0]["code"] == (
        "NECESSITY_NOT_PROVEN"
    )
