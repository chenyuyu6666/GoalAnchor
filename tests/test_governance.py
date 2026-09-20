from __future__ import annotations

from goalanchor import (
    ContractValidationError,
    GateFinding,
    GateId,
    GateReport,
    GateResult,
    GovernanceGate,
    GoalContract,
    LifecycleState,
    apply_suggested_transition,
    governance_owners,
    map_gate_report,
    map_gate_result,
)
import pytest


def make_result(gate_id: GateId, passed: bool = True) -> GateResult:
    findings = () if passed else (GateFinding("FAIL", "test failure"),)
    return GateResult(gate_id=gate_id, passed=passed, findings=findings)


def make_contract(stage: LifecycleState = LifecycleState.PLAN) -> GoalContract:
    return GoalContract(
        task_objective="完成受控治理映射",
        constraints=(),
        acceptance_criteria=("映射保持旧规则行为",),
        current_blocking_uncertainty=None,
        stage=stage,
        deliverables=("兼容适配层",),
    )


def test_every_existing_rule_has_exactly_one_primary_owner() -> None:
    for gate_id in GateId:
        primary, secondary = governance_owners(gate_id)
        assert isinstance(primary, GovernanceGate)
        assert primary not in secondary
        assert len(secondary) == len(set(secondary))


def test_mapping_reuses_the_original_result_without_reinterpreting_it() -> None:
    result = make_result(GateId.Q3_PATH, passed=False)
    mappings = map_gate_result(result)

    assert mappings[0].gate_id is GovernanceGate.EVIDENCE
    assert mappings[0].primary_owner
    assert all(mapping.result is result for mapping in mappings)
    assert [mapping.gate_id for mapping in mappings[1:]] == [
        GovernanceGate.GOAL,
        GovernanceGate.DECISION,
    ]
    assert all(not mapping.primary_owner for mapping in mappings[1:])
    assert mappings[0].result.findings[0].code == "FAIL"


def test_report_mapping_preserves_source_order_and_does_not_run_checks() -> None:
    q1 = make_result(GateId.Q1_GOAL)
    q2 = make_result(GateId.Q2_LOGIC)
    mappings = map_gate_report(GateReport((q1, q2)))

    assert [mapping.result.gate_id for mapping in mappings] == [
        GateId.Q1_GOAL,
        GateId.Q2_LOGIC,
        GateId.Q2_LOGIC,
    ]
    assert sum(mapping.primary_owner for mapping in mappings) == 2


def test_mapping_is_machine_readable_and_keeps_existing_result_shape() -> None:
    result = make_result(GateId.EXECUTE, passed=False)
    payload = map_gate_result(result)[0].to_dict()

    assert payload["gate_id"] == "ExecutionGate"
    assert payload["source_rules"] == ["EXECUTE"]
    assert payload["primary_owner"] is True
    assert payload["result"] == result.to_dict()


def test_only_primary_explicit_suggestions_can_change_lifecycle() -> None:
    suggested = GateResult(
        gate_id=GateId.PLAN_CHANGED,
        passed=False,
        findings=(GateFinding("PLAN_CHANGED", "replan required"),),
        next_state=LifecycleState.REPLAN,
    )
    primary, secondary = map_gate_result(suggested)

    updated = apply_suggested_transition(make_contract(), primary)
    assert updated.stage is LifecycleState.REPLAN

    with pytest.raises(ContractValidationError, match="secondary"):
        apply_suggested_transition(make_contract(), secondary)

    without_suggestion = map_gate_result(make_result(GateId.Q1_GOAL))[0]
    with pytest.raises(ContractValidationError, match="does not provide"):
        apply_suggested_transition(make_contract(), without_suggestion)
