from __future__ import annotations

import pytest

from goalanchor import (
    ContractValidationError,
    GoalContract,
    LifecycleState,
    VerificationOutcome,
    VerificationResult,
    apply_verification_transition,
)


def make_contract(stage: LifecycleState) -> GoalContract:
    return GoalContract(
        task_objective="完成验证闭环",
        constraints=(),
        acceptance_criteria=("标准 A", "标准 B"),
        current_blocking_uncertainty=None,
        stage=stage,
        deliverables=("验证结果",),
    )


def test_pass_requires_all_criteria_and_closes_execute() -> None:
    result = VerificationResult(
        outcome=VerificationOutcome.PASS,
        acceptance_criteria=("标准 A", "标准 B"),
        completed_criteria=("标准 A", "标准 B"),
        evidence_refs=("E-1",),
        reason="两个验收标准均已观察到",
    )
    assert apply_verification_transition(make_contract(LifecycleState.EXECUTE), result).stage is LifecycleState.CLOSED


def test_warn_preserves_state_and_requires_remaining_risks() -> None:
    result = VerificationResult(
        outcome=VerificationOutcome.WARN,
        acceptance_criteria=("标准 A", "标准 B"),
        completed_criteria=("标准 A",),
        evidence_refs=("E-2",),
        reason="主要路径通过，但覆盖不足",
        remaining_risks=("标准 B 尚未覆盖",),
    )
    contract = make_contract(LifecycleState.EXECUTE)
    assert apply_verification_transition(contract, result) == contract


def test_fail_requires_evidence_and_returns_to_replan() -> None:
    result = VerificationResult(
        outcome=VerificationOutcome.FAIL,
        acceptance_criteria=("标准 A",),
        completed_criteria=(),
        evidence_refs=("E-3",),
        reason="标准 A 未满足",
    )
    replanned = apply_verification_transition(make_contract(LifecycleState.EXECUTE), result)
    assert replanned.stage is LifecycleState.REPLAN
    assert replanned.current_blocking_uncertainty == "标准 A 未满足"


def test_pass_cannot_close_from_non_execute_stage() -> None:
    result = VerificationResult(
        outcome=VerificationOutcome.PASS,
        acceptance_criteria=("标准 A",),
        completed_criteria=("标准 A",),
        evidence_refs=("E-4",),
        reason="通过",
    )
    with pytest.raises(ContractValidationError, match="EXECUTE"):
        apply_verification_transition(make_contract(LifecycleState.VERIFY), result)


def test_non_pass_results_require_evidence_boundaries() -> None:
    with pytest.raises(ContractValidationError, match="evidence_refs"):
        VerificationResult(
            outcome=VerificationOutcome.FAIL,
            acceptance_criteria=("标准 A",),
            completed_criteria=(),
            evidence_refs=(),
            reason="没有通过",
        )
    with pytest.raises(ContractValidationError, match="remaining_risks"):
        VerificationResult(
            outcome=VerificationOutcome.WARN,
            acceptance_criteria=("标准 A",),
            completed_criteria=(),
            evidence_refs=("E-5",),
            reason="存在风险",
        )


@pytest.mark.parametrize("criteria", [("标准 A",), ("替代标准",), ("标准 A", "标准 B", "额外标准")])
def test_pass_cannot_close_a_different_acceptance_baseline(criteria) -> None:
    contract = make_contract(LifecycleState.EXECUTE)
    result = VerificationResult(
        outcome=VerificationOutcome.PASS, acceptance_criteria=criteria, completed_criteria=criteria,
        evidence_refs=("E-current",), reason="结果自身通过不代表原目标通过",
    )
    with pytest.raises(ContractValidationError, match="acceptance_criteria"):
        apply_verification_transition(contract, result)
    assert contract.stage is LifecycleState.EXECUTE


def test_matching_baseline_allows_reordering_and_keeps_empty_pass_evidence() -> None:
    result = VerificationResult(
        outcome=VerificationOutcome.PASS, acceptance_criteria=("标准 B", "标准 A", "标准 A"),
        completed_criteria=("标准 A", "标准 B"), evidence_refs=(), reason="保留集合语义与既有空证据策略",
    )
    assert apply_verification_transition(make_contract(LifecycleState.EXECUTE), result).stage is LifecycleState.CLOSED
