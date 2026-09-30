from __future__ import annotations

import json
from pathlib import Path

import pytest

from goalanchor import (
    ExecutePreflight,
    GateId,
    GoalContract,
    LifecycleState,
    StructureReview,
    VerifyRecord,
    WorkItem,
    check_execute,
    check_plan_change,
    check_q1_goal,
    check_q2_logic,
    check_q3_path,
    check_q4_minimum,
    check_q5_structure,
    check_verify,
    evaluate_plan,
)
from goalanchor.compat import main


def make_contract(**overrides) -> GoalContract:
    values = {
        "task_objective": "达到可接受的页面滑动流畅度",
        "constraints": ("保持视频和页面状态正确",),
        "acceptance_criteria": ("handler latency 满足目标",),
        "current_blocking_uncertainty": None,
        "stage": LifecycleState.PLAN,
        "deliverables": ("验证报告",),
    }
    values.update(overrides)
    return GoalContract(**values)


def make_item(**overrides) -> WorkItem:
    values = {
        "item_id": "W1",
        "issue": "无法区分 Snapshot 与 timer 的耗时贡献",
        "input_evidence": ("日志 E1",),
        "action": "分别执行 timer-only 与 capture-only profiling",
        "expected_decision_impact": "决定优化调度还是捕获路径",
        "exit_criterion": "得到两个路径的可归因耗时",
        "expected_result": "输出 handler latency 分解",
        "decision_link": "是否进入 Snapshot 架构重构",
    }
    values.update(overrides)
    return WorkItem(**values)


def test_q1_rejects_blocker_replacing_final_goal() -> None:
    result = check_q1_goal(
        make_contract(current_blocking_uncertainty="达到可接受的页面滑动流畅度")
    )
    assert not result.passed
    assert result.gate_id is GateId.Q1_GOAL
    assert result.findings[0].code == "BLOCKER_REPLACES_GOAL"


def test_q2_requires_result_and_decision_chain() -> None:
    result = check_q2_logic((make_item(expected_result=None),))
    assert not result.passed
    assert result.findings[0].code == "MISSING_EXPECTED_RESULT"
    assert check_q2_logic((make_item(),)).passed


def test_q3_rejects_unverified_assumptions_and_patches() -> None:
    assumptions = check_q3_path((make_item(assumptions=("Snapshot 是主要瓶颈",)),))
    assert not assumptions.passed
    assert assumptions.findings[0].code == "UNVERIFIED_ASSUMPTION"

    verified = check_q3_path(
        (make_item(assumptions=("Snapshot 是主要瓶颈",), assumptions_verified=True),)
    )
    assert verified.passed

    patch = check_q3_path((make_item(unverified_patch_refs=("临时关闭 Monitor",)),))
    assert not patch.passed
    assert patch.findings[0].code == "UNVERIFIED_PATCH"


def test_q4_requires_decision_link() -> None:
    result = check_q4_minimum((make_item(decision_link=None),))
    assert not result.passed
    assert result.findings[0].code == "MISSING_DECISION_LINK"
    assert check_q4_minimum((make_item(),)).passed


def test_q5_requires_review_and_rejects_structural_risks() -> None:
    assert not check_q5_structure((make_item(),), None).passed
    duplicate_id = check_q5_structure((make_item(), make_item()), StructureReview())
    assert not duplicate_id.passed
    assert duplicate_id.findings[0].code == "DUPLICATE_WORK_ITEM_ID"

    duplicate_responsibility = check_q5_structure(
        (make_item(),), StructureReview(responsibility_keys=("capture", "capture"))
    )
    assert not duplicate_responsibility.passed
    assert duplicate_responsibility.findings[0].code == "DUPLICATE_RESPONSIBILITY"

    risk = check_q5_structure(
        (make_item(),), StructureReview(public_path_hacks=("global callback",))
    )
    assert not risk.passed
    assert risk.findings[0].code == "STRUCTURE_RISK"

    assert check_q5_structure((make_item(),), StructureReview(responsibility_keys=("capture",))).passed


def test_evaluate_plan_runs_all_five_gates_in_fixed_order() -> None:
    report = evaluate_plan(make_contract(), (make_item(),), StructureReview())
    assert report.passed
    assert [result.gate_id.value for result in report.results] == [
        "Q1 Goal",
        "Q2 Logic",
        "Q3 Path",
        "Q4 Minimum",
        "Q5 Structure",
    ]


def test_verify_requires_three_observable_finite_fields() -> None:
    assert not check_verify(None).passed
    assert not check_verify(
        VerifyRecord("Snapshot API", "决定是否改架构", "得到 profiling", observable=False)
    ).passed
    finite = check_verify(
        VerifyRecord("Snapshot API", "决定是否改架构", "得到 profiling", finite=False)
    )
    assert not finite.passed
    assert finite.findings[0].code == "EXIT_NOT_FINITE"
    assert check_verify(VerifyRecord("Snapshot API", "决定是否改架构", "得到 profiling")).passed


def test_execute_requires_verify_state_and_recovery() -> None:
    contract = make_contract(stage=LifecycleState.VERIFY)
    assert not check_execute(contract, None).passed

    good = ExecutePreflight(
        plan_state=LifecycleState.VERIFY,
        verify_passed=True,
        constraints_acknowledged=True,
        change_scope="只改 profiling 埋点",
        recovery_path="保留旧固件并可回滚",
    )
    assert check_execute(contract, good).passed

    wrong_state = check_execute(
        contract,
        ExecutePreflight(
            plan_state=LifecycleState.PLAN,
            verify_passed=True,
            constraints_acknowledged=True,
            change_scope="scope",
            recovery_path="rollback",
        ),
    )
    assert not wrong_state.passed
    assert wrong_state.findings[0].code == "VERIFY_STATE_REQUIRED"

    not_verified = check_execute(
        contract,
        ExecutePreflight(
            plan_state=LifecycleState.VERIFY,
            verify_passed=False,
            constraints_acknowledged=True,
            change_scope="scope",
            recovery_path="rollback",
        ),
    )
    assert not not_verified.passed
    assert not_verified.findings[0].code == "VERIFY_NOT_PASSED"


def test_plan_change_requires_replan() -> None:
    before = make_contract()
    assert check_plan_change(before, make_contract()).passed
    changed = check_plan_change(before, make_contract(main_strategy="先测 timer-only"))
    assert not changed.passed
    assert changed.next_state is LifecycleState.REPLAN
    assert changed.findings[0].code == "PLAN_CHANGED"


def test_gate_results_are_machine_readable() -> None:
    result = check_q4_minimum((make_item(decision_link=None),)).to_dict()
    assert result["gate_id"] == "Q4 Minimum"
    assert result["passed"] is False
    assert result["findings"][0]["field"].endswith("decision_link")


def test_gate_cli_evaluates_serialized_plan(tmp_path: Path, capsys) -> None:
    record = {
        "contract": make_contract().to_dict(),
        "work_items": [make_item().to_dict()],
        "structure_review": StructureReview().to_dict(),
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    assert main(["gate", "--phase", "plan", "--record", str(path), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is True


def test_gate_cli_returns_one_for_failed_gate(tmp_path: Path, capsys) -> None:
    record = {"contract": make_contract().to_dict(), "work_items": [make_item().to_dict()]}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    assert main(["gate", "--phase", "plan", "--record", str(path)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is False
    assert "Q5 Structure" in output["failed_gate_ids"]


def test_gate_cli_requires_record_fields(tmp_path: Path, capsys) -> None:
    path = tmp_path / "empty.json"
    path.write_text("{}", encoding="utf-8")
    assert main(["gate", "--phase", "plan-change", "--record", str(path)]) == 2
    assert "missing required field" in capsys.readouterr().err
