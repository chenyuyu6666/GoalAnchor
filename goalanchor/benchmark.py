"""Small deterministic benchmark for the Phase 6 governance risks."""

from __future__ import annotations

from typing import Any

from .core import GoalContract, LifecycleState, WorkItem
from .decision import ActionType, AuthorizationScope, check_action_authorization
from .gates import check_q1_goal, check_q3_path
from .integration import ReviewFinding
from .review_service import (
    ArbiterResult,
    DisputePacket,
    MetaReviewResult,
    ReviewContext,
    ReviewerResult,
    ReviewOutcome,
    ReviewRisk,
    ReviewVerdict,
    run_review_cycle,
)
from .verification import VerificationOutcome, VerificationResult, apply_verification_transition


def _contract(**overrides: Any) -> GoalContract:
    values: dict[str, Any] = {
        "task_objective": "完成受控治理验证",
        "constraints": ("保持原始目标不变",),
        "acceptance_criteria": ("四类风险均可被阻断",),
        "current_blocking_uncertainty": None,
        "stage": LifecycleState.PLAN,
        "deliverables": ("Phase 6 基准报告",),
    }
    values.update(overrides)
    return GoalContract(**values)


def _work_item(**overrides: Any) -> WorkItem:
    values: dict[str, Any] = {
        "item_id": "BENCH-W1",
        "issue": "验证治理边界",
        "input_evidence": ("benchmark-input",),
        "action": "执行固定治理场景",
        "expected_decision_impact": "决定是否允许继续",
        "exit_criterion": "得到固定结果码",
        "expected_result": "输出治理结果",
        "decision_link": "是否允许进入下一步",
    }
    values.update(overrides)
    return WorkItem(**values)


def _finding_codes(result: Any) -> list[str]:
    return [finding.code for finding in result.findings]


def _review_finding(finding_id: str) -> ReviewFinding:
    return ReviewFinding(
        finding_id=finding_id,
        issue="Reviewer 发现候选方案存在争议",
        evidence_refs=("phase6-review-evidence",),
        decision_impact="决定是否继续执行候选方案",
        remaining_uncertainty="争议是否已解决",
    )


def run_phase6_benchmark() -> dict[str, Any]:
    """Run four fixed scenarios and return a stable machine-readable report."""

    scenarios: list[dict[str, Any]] = []

    goal_result = check_q1_goal(
        _contract(current_blocking_uncertainty="完成受控治理验证")
    )
    scenarios.append({
        "scenario_id": "PH6-GOAL-DRIFT",
        "risk": "goal_drift",
        "expected": {"passed": False, "finding_codes": ["BLOCKER_REPLACES_GOAL"]},
        "actual": {"passed": goal_result.passed, "finding_codes": _finding_codes(goal_result)},
    })

    premise_result = check_q3_path(
        (_work_item(assumptions=("当前假设已经被证实",)),)
    )
    scenarios.append({
        "scenario_id": "PH6-UNVERIFIED-ASSUMPTION",
        "risk": "unverified_assumption",
        "expected": {"passed": False, "finding_codes": ["UNVERIFIED_ASSUMPTION"]},
        "actual": {"passed": premise_result.passed, "finding_codes": _finding_codes(premise_result)},
    })

    authorization_result = check_action_authorization(
        ActionType.SOURCE_EDIT,
        scope=AuthorizationScope.SESSION,
        scope_ref="phase6-session",
        at="2026-09-19T10:00:00+08:00",
    )
    scenarios.append({
        "scenario_id": "PH6-UNAUTHORIZED-EDIT",
        "risk": "unauthorized_execution",
        "expected": {
            "passed": False,
            "finding_codes": ["AUTHORIZATION_REQUIRED"],
            "next_state": "HUMAN_DECISION_REQUIRED",
        },
        "actual": {
            "passed": authorization_result.passed,
            "finding_codes": _finding_codes(authorization_result),
            "next_state": (
                None
                if authorization_result.next_state is None
                else authorization_result.next_state.value
            ),
        },
    })

    verification_result = VerificationResult(
        outcome=VerificationOutcome.FAIL,
        acceptance_criteria=("结果满足目标",),
        completed_criteria=(),
        evidence_refs=("phase6-evidence",),
        reason="固定验证场景未满足验收条件",
    )
    replanned = apply_verification_transition(
        _contract(stage=LifecycleState.EXECUTE), verification_result
    )
    scenarios.append({
        "scenario_id": "PH6-VERIFICATION-FAILURE",
        "risk": "unverified_completion",
        "expected": {"next_state": "REPLAN"},
        "actual": {"next_state": replanned.stage.value},
    })

    review_context = ReviewContext(
        goal="判断候选方案是否仍满足原始目标",
        prompt="审核目标、约束、证据和候选方案，独立输出结构化结论",
        constraints=("不得偷换目标",),
        evidence_refs=("phase6-review-evidence",),
        candidate="候选方案 A",
    )
    review_calls: list[tuple[str, int, bool]] = []

    def reviewer_call(
        context: ReviewContext,
        reviewer_id: str,
        round_number: int,
        dispute: DisputePacket | None,
    ) -> ReviewerResult:
        review_calls.append((reviewer_id, round_number, dispute is not None))
        verdict = (
            ReviewVerdict.FAIL
            if round_number == 1 and reviewer_id == "reviewer-2"
            else ReviewVerdict.PASS
        )
        findings = () if verdict is ReviewVerdict.PASS else (_review_finding("R1"),)
        return ReviewerResult(
            reviewer_id,
            round_number,
            verdict,
            findings,
            True,
            context.fingerprint,
        )

    def arbiter_call(
        context: ReviewContext,
        results: tuple[ReviewerResult, ...],
        round_number: int,
        dispute: DisputePacket | None,
    ) -> ArbiterResult:
        return ArbiterResult(
            "arbiter",
            round_number,
            ReviewVerdict.PASS,
            "主 Agent 汇总 Reviewer 结果",
            "第二轮重新比较原始证据" if round_number == 2 else "需要争议复审",
            tuple(item.reviewer_id for item in results),
            context.fingerprint,
            round_number == 1,
        )

    def meta_review_call(
        context: ReviewContext,
        reviewer_results: tuple[ReviewerResult, ...],
        arbiter: ArbiterResult,
        round_number: int,
    ) -> MetaReviewResult:
        verdict = ReviewVerdict.FAIL if round_number == 1 else ReviewVerdict.PASS
        findings = () if verdict is ReviewVerdict.PASS else (_review_finding("META1"),)
        return MetaReviewResult(
            "meta-reviewer",
            round_number,
            arbiter.arbiter_id,
            verdict,
            "独立复核主 Agent 判断",
            findings,
            context.fingerprint,
            round_number == 2,
        )

    review_result = run_review_cycle(
        review_context,
        ReviewRisk.HIGH,
        reviewer_call,
        arbiter_call,
        meta_review_call,
    )
    scenarios.append({
        "scenario_id": "PH6-FULL-MULTI-AGENT-REVIEW",
        "risk": "multi_agent_review_convergence",
        "expected": {
            "outcome": ReviewOutcome.PASS.value,
            "rounds": 2,
            "same_context": True,
            "dispute_round": True,
        },
        "actual": {
            "outcome": review_result.outcome.value,
            "rounds": review_result.round_number,
            "same_context": all(
                item[0] in {"reviewer-1", "reviewer-2"} for item in review_calls
            ),
            "dispute_round": any(item[2] for item in review_calls),
        },
    })

    for scenario in scenarios:
        scenario["aligned"] = scenario["expected"] == scenario["actual"]
    return {
        "benchmark_id": "goalanchor-phase6-core-risks-and-review",
        "total": len(scenarios),
        "aligned_count": sum(item["aligned"] for item in scenarios),
        "scenarios": scenarios,
    }


__all__ = ["run_phase6_benchmark"]
