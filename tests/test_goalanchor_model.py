from __future__ import annotations

import json

import pytest

from goalanchor import (
    Claim,
    ClaimKind,
    ContractValidationError,
    GoalContract,
    InvalidTransitionError,
    LifecycleState,
    PlanChangeField,
    WorkItem,
    WorkItemStatus,
    allowed_transitions,
    can_transition,
    compare_contracts,
    plan_changed,
    transition,
)


def make_contract(**overrides) -> GoalContract:
    values = {
        "task_objective": "交付睡眠—唤醒—恢复稳定闭环",
        "constraints": ("不能在 ISR 中阻塞",),
        "acceptance_criteria": ("连续运行覆盖有效观察窗口",),
        "current_blocking_uncertainty": None,
        "stage": LifecycleState.PLAN,
        "deliverables": ("可验证固件",),
        "requirements": ("REQ39", "REQ64"),
        "core_assumptions": ("唤醒源必须可被独立证明",),
        "main_strategy": "先建立证据链，再实施最小变更",
        "evidence_obligations": ("记录源码到测试对象的版本关系",),
        "plan_revision": 0,
    }
    values.update(overrides)
    return GoalContract(**values)


def test_goal_contract_round_trip_is_deterministic() -> None:
    contract = make_contract()

    first = contract.to_json()
    second = GoalContract.from_json(first).to_json()

    assert first == second
    decoded = json.loads(first)
    assert decoded["stage"] == "PLAN"
    assert decoded["task_objective"] != decoded["current_blocking_uncertainty"]


def test_goal_contract_requires_acceptance_and_deliverables() -> None:
    with pytest.raises(ContractValidationError, match="acceptance_criteria"):
        make_contract(acceptance_criteria=())
    with pytest.raises(ContractValidationError, match="deliverables"):
        make_contract(deliverables=())


def test_blocker_cannot_replace_goal_and_blocked_requires_blocker() -> None:
    with pytest.raises(ContractValidationError, match="BLOCKED stage"):
        make_contract(stage=LifecycleState.BLOCKED)

    blocked = make_contract(
        stage=LifecycleState.BLOCKED,
        current_blocking_uncertainty="尚未确认唤醒源的因果证据",
    )
    assert blocked.task_objective == "交付睡眠—唤醒—恢复稳定闭环"
    assert blocked.current_blocking_uncertainty != blocked.task_objective

    with pytest.raises(ContractValidationError, match="HUMAN_DECISION_REQUIRED stage"):
        make_contract(stage=LifecycleState.HUMAN_DECISION_REQUIRED)


def test_work_item_round_trip_preserves_decision_link_and_status() -> None:
    item = WorkItem(
        item_id="GA-S1-02-W01",
        issue="当前 blocker 是否阻塞下一步实施",
        input_evidence=("日志片段 L17", "复现实验 E02"),
        action="只检查能改变闸门决策的证据",
        expected_decision_impact="决定是否进入 VERIFY",
        exit_criterion="得到一个可观察的 PASS 或 FAIL",
        status=WorkItemStatus.VERIFYING,
    )

    assert WorkItem.from_json(item.to_json()) == item


def test_claim_classification_never_upgrades_implicitly() -> None:
    observation = Claim(
        text="按键后设备醒了",
        kind=ClaimKind.OBSERVATION,
        source="实验 E01",
    )
    decoded = Claim.from_json(observation.to_json())
    assert decoded.kind is ClaimKind.OBSERVATION
    with pytest.raises(ContractValidationError, match="explicit basis"):
        Claim(
            text=observation.text,
            kind=ClaimKind.FACT,
            source=observation.source,
        )

    fact = observation.reclassify(
        ClaimKind.FACT,
        basis="独立复现实验 E02 与来源测量一致",
    )
    assert fact.kind is ClaimKind.FACT
    assert fact.basis


def test_hypothesis_to_constraint_also_requires_explicit_basis() -> None:
    hypothesis = Claim(
        text="Monitor 可能触发恢复竞争",
        kind=ClaimKind.HYPOTHESIS,
        source="调查记录 I03",
    )
    with pytest.raises(ContractValidationError, match="explicit basis"):
        hypothesis.reclassify(ClaimKind.CONSTRAINT, basis=" ")
    constraint = hypothesis.reclassify(
        ClaimKind.CONSTRAINT,
        basis="受控实验 E04 排除了无 Monitor 路径",
    )
    assert constraint.kind is ClaimKind.CONSTRAINT


def test_plan_changed_covers_only_semantic_plan_fields() -> None:
    baseline = make_contract()
    blocker_update = make_contract(
        current_blocking_uncertainty="需要确认唤醒源",
        stage=LifecycleState.VERIFY,
    )
    assert not plan_changed(baseline, blocker_update)

    changed = make_contract(main_strategy="先做混杂实验")
    diff = compare_contracts(baseline, changed)
    assert diff.changed
    assert diff.invalidates_previous_validation
    assert diff.changed_fields == (PlanChangeField.MAIN_STRATEGY,)

    changed = make_contract(deliverables=("源码补丁", "验证报告"))
    assert compare_contracts(baseline, changed).changed_fields == (
        PlanChangeField.DELIVERABLE_SCOPE,
    )


def test_plan_changed_detects_every_declared_trigger() -> None:
    baseline = make_contract()
    cases = (
        ("task_objective", "新的最终交付目标", PlanChangeField.OBJECTIVE),
        ("requirements", ("REQ-new",), PlanChangeField.REQUIREMENTS),
        ("constraints", ("新的安全约束",), PlanChangeField.CONSTRAINTS),
        ("core_assumptions", ("新的核心假设",), PlanChangeField.CORE_ASSUMPTIONS),
        ("main_strategy", "新的主策略", PlanChangeField.MAIN_STRATEGY),
        ("acceptance_criteria", ("新的验收条件",), PlanChangeField.ACCEPTANCE),
        (
            "evidence_obligations",
            ("新的证据义务",),
            PlanChangeField.EVIDENCE_OBLIGATIONS,
        ),
        ("deliverables", ("新的交付物",), PlanChangeField.DELIVERABLE_SCOPE),
    )
    for field_name, value, expected_field in cases:
        diff = compare_contracts(baseline, make_contract(**{field_name: value}))
        assert diff.changed_fields == (expected_field,)

    # A bookkeeping revision alone does not change the plan semantics.
    assert not plan_changed(baseline, make_contract(plan_revision=1))


def test_lifecycle_transitions_are_explicit_and_bounded() -> None:
    contract = make_contract()
    assert can_transition(LifecycleState.PLAN, LifecycleState.VERIFY)
    assert not can_transition(LifecycleState.PLAN, LifecycleState.EXECUTE)
    assert not can_transition(LifecycleState.CLOSED, LifecycleState.PLAN)

    verifying = transition(contract, LifecycleState.VERIFY)
    executing = transition(verifying, LifecycleState.EXECUTE)
    closed = transition(executing, LifecycleState.CLOSED)
    assert closed.stage is LifecycleState.CLOSED

    with pytest.raises(InvalidTransitionError):
        transition(contract, LifecycleState.EXECUTE)

    blocked = transition(
        contract,
        LifecycleState.BLOCKED,
        blocking_uncertainty="无法进入下一安全实施步骤",
    )
    assert blocked.stage is LifecycleState.BLOCKED
    assert transition(blocked, LifecycleState.REPLAN).stage is LifecycleState.REPLAN

    waiting = transition(
        contract,
        LifecycleState.HUMAN_DECISION_REQUIRED,
        blocking_uncertainty="存在两个同样可行的方案，需要用户选择",
    )
    assert transition(waiting, LifecycleState.PLAN).stage is LifecycleState.PLAN
    assert transition(waiting, LifecycleState.EXECUTE).stage is LifecycleState.EXECUTE

    stopped = transition(
        contract,
        LifecycleState.STOPPED,
        blocking_uncertainty="用户终止当前路径",
    )
    assert stopped.stage is LifecycleState.STOPPED
    with pytest.raises(InvalidTransitionError):
        transition(stopped, LifecycleState.EXECUTE)
    with pytest.raises(InvalidTransitionError):
        transition(stopped, LifecycleState.PLAN)

    replanned = transition(blocked, LifecycleState.REPLAN)
    assert transition(
        replanned,
        LifecycleState.STOPPED,
        blocking_uncertainty="重新规划路径后仍明确终止",
    ).stage is LifecycleState.STOPPED


def test_transition_table_is_json_friendly_and_stable() -> None:
    table = allowed_transitions()
    assert table["PLAN"] == (
        "VERIFY",
        "HUMAN_DECISION_REQUIRED",
        "BLOCKED",
        "STOPPED",
        "REPLAN",
    )
    assert table["STOPPED"] == ()
    assert table["CLOSED"] == ()
