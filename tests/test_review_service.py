from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from goalanchor import (
    ArbiterResult,
    ContractValidationError,
    DecisionReport,
    DisputePacket,
    LifecycleState,
    MetaReviewResult,
    ReviewContext,
    ReviewerResult,
    ReviewFinding,
    ReviewOutcome,
    ReviewRisk,
    ReviewVerdict,
    evaluate_review_cycle,
    evaluate_review_round,
    required_reviewers,
    run_review_cycle,
)


def finding(finding_id: str) -> ReviewFinding:
    return ReviewFinding(
        finding_id=finding_id,
        issue="存在治理风险",
        evidence_refs=("E-1",),
        decision_impact="决定是否继续执行",
        remaining_uncertainty="风险是否可接受",
    )


def reviewer(reviewer_id: str, verdict: ReviewVerdict, round_number: int = 1) -> ReviewerResult:
    findings = () if verdict is ReviewVerdict.PASS else (finding(f"F-{reviewer_id}"),)
    return ReviewerResult(reviewer_id, round_number, verdict, findings)


def context() -> ReviewContext:
    return ReviewContext(
        goal="判断候选方案是否满足目标",
        prompt="审查目标、约束、证据和候选结果，输出结构化风险",
        constraints=("不能改变原始目标",),
        evidence_refs=("E-1",),
        candidate="候选方案 A",
    )


def test_review_context_round_trip_validates_fingerprint() -> None:
    original = context()
    restored = ReviewContext.from_dict(original.to_dict())
    assert restored == original

    corrupted = original.to_dict()
    corrupted["fingerprint"] = "invalid-fingerprint"
    with pytest.raises(ContractValidationError, match="fingerprint"):
        ReviewContext.from_dict(corrupted)


def contextual_reviewer(
    ctx: ReviewContext, reviewer_id: str, verdict: ReviewVerdict, round_number: int = 1
) -> ReviewerResult:
    findings = () if verdict is ReviewVerdict.PASS else (finding(f"F-{reviewer_id}"),)
    return ReviewerResult(reviewer_id, round_number, verdict, findings, True, ctx.fingerprint)


def test_review_depth_is_risk_driven() -> None:
    assert required_reviewers(ReviewRisk.LOW) == 1
    assert required_reviewers(ReviewRisk.MEDIUM) == 2
    assert required_reviewers(ReviewRisk.HIGH) == 2
    assert required_reviewers(ReviewRisk.CRITICAL) == 3


def test_low_risk_still_requires_one_review() -> None:
    with pytest.raises(ContractValidationError, match="LOW risk requires 1"):
        evaluate_review_round(ReviewRisk.LOW, (), round_number=1)


def test_unanimous_pass_and_bounded_warning_converge() -> None:
    passed = evaluate_review_round(
        ReviewRisk.HIGH,
        (reviewer("A", ReviewVerdict.PASS), reviewer("B", ReviewVerdict.PASS)),
        round_number=1,
    )
    assert passed.outcome is ReviewOutcome.PASS

    warned = evaluate_review_round(
        ReviewRisk.HIGH,
        (reviewer("A", ReviewVerdict.PASS), reviewer("B", ReviewVerdict.WARN)),
        round_number=1,
    )
    assert warned.outcome is ReviewOutcome.WARN


def test_disagreement_gets_one_more_round_then_escalates() -> None:
    first = evaluate_review_round(
        ReviewRisk.HIGH,
        (reviewer("A", ReviewVerdict.PASS), reviewer("B", ReviewVerdict.FAIL)),
        round_number=1,
    )
    assert first.outcome is ReviewOutcome.REVIEW_REQUIRED

    second = evaluate_review_round(
        ReviewRisk.HIGH,
        (
            reviewer("A", ReviewVerdict.PASS, 2),
            reviewer("B", ReviewVerdict.FAIL, 2),
        ),
        round_number=2,
    )
    assert second.outcome is ReviewOutcome.HUMAN_DECISION_REQUIRED
    assert second.next_state is LifecycleState.HUMAN_DECISION_REQUIRED


def test_unanimous_failure_escalates_without_recursive_review() -> None:
    failed = evaluate_review_round(
        ReviewRisk.HIGH,
        (reviewer("A", ReviewVerdict.FAIL), reviewer("B", ReviewVerdict.FAIL)),
        round_number=1,
    )
    assert failed.outcome is ReviewOutcome.HUMAN_DECISION_REQUIRED


def test_review_results_must_be_independent_and_match_policy() -> None:
    dependent = ReviewerResult(
        "A",
        1,
        ReviewVerdict.WARN,
        (finding("F-A"),),
        isolated=False,
    )
    with pytest.raises(ContractValidationError, match="isolated"):
        evaluate_review_round(
            ReviewRisk.MEDIUM,
            (dependent, reviewer("B", ReviewVerdict.PASS)),
            round_number=1,
        )
    with pytest.raises(ContractValidationError, match="requires 2"):
        evaluate_review_round(
            ReviewRisk.HIGH,
            (reviewer("A", ReviewVerdict.PASS),),
            round_number=1,
        )
    with pytest.raises(ContractValidationError, match="1 or 2"):
        evaluate_review_round(ReviewRisk.HIGH, (), round_number=1, max_rounds=3)


def test_review_context_requires_the_same_goal_and_prompt_fingerprint() -> None:
    ctx = context()
    first = contextual_reviewer(ctx, "A", ReviewVerdict.PASS)
    mismatched = ReviewerResult("B", 1, ReviewVerdict.PASS, (), True, "different")
    arbiter = ArbiterResult(
        "arbiter",
        1,
        ReviewVerdict.PASS,
        "通过",
        "证据一致",
        ("A", "B"),
        ctx.fingerprint,
        False,
    )
    with pytest.raises(ContractValidationError, match="same ReviewContext"):
        evaluate_review_cycle(ctx, ReviewRisk.HIGH, (first, mismatched), arbiter, None, round_number=1)


def test_arbiter_disagreement_creates_dispute_packet_for_second_round() -> None:
    ctx = context()
    results = (
        contextual_reviewer(ctx, "A", ReviewVerdict.PASS),
        contextual_reviewer(ctx, "B", ReviewVerdict.FAIL),
    )
    arbiter = ArbiterResult(
        "arbiter",
        1,
        ReviewVerdict.PASS,
        "主 Agent 倾向通过",
        "主 Agent 认为 E-1 足以解释风险",
        ("A", "B"),
        ctx.fingerprint,
        True,
    )
    meta = MetaReviewResult(
        "meta-1",
        1,
        "arbiter",
        ReviewVerdict.FAIL,
        "主 Agent 忽略了 Reviewer B 的高风险发现",
        (finding("META-1"),),
        ctx.fingerprint,
        False,
    )
    result = evaluate_review_cycle(
        ctx,
        ReviewRisk.HIGH,
        results,
        arbiter,
        meta,
        round_number=1,
    )
    assert result.outcome is ReviewOutcome.REVIEW_REQUIRED
    assert isinstance(result.dispute_packet, DisputePacket)
    assert result.dispute_packet.context_fingerprint == ctx.fingerprint
    assert "same ReviewContext" in result.dispute_packet.re_review_prompt


def test_second_unresolved_cycle_stops_and_reports_to_user() -> None:
    ctx = context()
    results = (
        contextual_reviewer(ctx, "A", ReviewVerdict.PASS, 2),
        contextual_reviewer(ctx, "B", ReviewVerdict.FAIL, 2),
    )
    arbiter = ArbiterResult(
        "arbiter",
        2,
        ReviewVerdict.PASS,
        "仍倾向通过",
        "仍不同意 Reviewer B",
        ("A", "B"),
        ctx.fingerprint,
        True,
    )
    meta = MetaReviewResult(
        "meta-2",
        2,
        "arbiter",
        ReviewVerdict.FAIL,
        "争议仍未解决",
        (finding("META-2"),),
        ctx.fingerprint,
        False,
    )
    result = evaluate_review_cycle(
        ctx,
        ReviewRisk.HIGH,
        results,
        arbiter,
        meta,
        round_number=2,
    )
    assert result.outcome is ReviewOutcome.STOPPED
    assert result.next_state is LifecycleState.STOPPED
    assert isinstance(result.decision_report, DecisionReport)
    assert result.decision_report.required_user_decision


def test_converged_arbiter_and_meta_review_can_pass() -> None:
    ctx = context()
    results = (
        contextual_reviewer(ctx, "A", ReviewVerdict.PASS),
        contextual_reviewer(ctx, "B", ReviewVerdict.PASS),
    )
    arbiter = ArbiterResult(
        "arbiter",
        1,
        ReviewVerdict.PASS,
        "通过",
        "两位 Reviewer 一致通过",
        ("A", "B"),
        ctx.fingerprint,
        False,
    )
    meta = MetaReviewResult(
        "meta-1",
        1,
        "arbiter",
        ReviewVerdict.PASS,
        "主 Agent 判断与证据一致",
        (),
        ctx.fingerprint,
        True,
    )
    result = evaluate_review_cycle(
        ctx,
        ReviewRisk.HIGH,
        results,
        arbiter,
        meta,
        round_number=1,
    )
    assert result.outcome is ReviewOutcome.PASS


@pytest.mark.parametrize(
    "reviewer_verdict,arbiter_verdict",
    [
        (ReviewVerdict.FAIL, ReviewVerdict.PASS),
        (ReviewVerdict.WARN, ReviewVerdict.PASS),
        (ReviewVerdict.PASS, ReviewVerdict.WARN),
        (ReviewVerdict.PASS, ReviewVerdict.FAIL),
    ],
)
@pytest.mark.parametrize(
    "round_number,max_rounds,expected",
    [(1, 2, ReviewOutcome.REVIEW_REQUIRED), (2, 2, ReviewOutcome.STOPPED), (1, 1, ReviewOutcome.STOPPED)],
)
def test_cycle_detects_hidden_verdict_disagreement(
    reviewer_verdict, arbiter_verdict, round_number, max_rounds, expected
):
    ctx = context()
    results = (
        contextual_reviewer(ctx, "A", ReviewVerdict.PASS, round_number),
        contextual_reviewer(ctx, "B", reviewer_verdict, round_number),
    )
    arbiter = ArbiterResult(
        "arbiter", round_number, arbiter_verdict, "裁决", "自报无分歧", ("A", "B"), ctx.fingerprint, False
    )
    meta = MetaReviewResult(
        "meta", round_number, "arbiter", arbiter_verdict, "认可主裁决",
        () if arbiter_verdict is ReviewVerdict.PASS else (finding("M"),), ctx.fingerprint, True,
    )
    result = evaluate_review_cycle(
        ctx, ReviewRisk.HIGH, results, arbiter, meta, round_number=round_number, max_rounds=max_rounds
    )
    assert result.outcome is expected
    if expected is ReviewOutcome.REVIEW_REQUIRED:
        assert result.dispute_packet is not None
        assert result.dispute_packet.reviewer_findings == tuple(f for item in results for f in item.findings)
    else:
        assert result.next_state is LifecycleState.STOPPED
        assert result.decision_report is not None


@pytest.mark.parametrize(
    "verdict,expected",
    [(ReviewVerdict.WARN, ReviewOutcome.WARN), (ReviewVerdict.FAIL, ReviewOutcome.HUMAN_DECISION_REQUIRED)],
)
def test_cycle_preserves_unanimous_non_pass(verdict, expected):
    ctx = context()
    results = tuple(contextual_reviewer(ctx, name, verdict) for name in ("A", "B"))
    arbiter = ArbiterResult("arbiter", 1, verdict, "一致结论", "相同意见", ("A", "B"), ctx.fingerprint, False)
    meta = MetaReviewResult("meta", 1, "arbiter", verdict, "一致", (finding("M"),), ctx.fingerprint, True)
    result = evaluate_review_cycle(ctx, ReviewRisk.HIGH, results, arbiter, meta, round_number=1)
    assert result.outcome is expected
    assert result.dispute_packet is None


def test_evidence_content_change_invalidates_old_review(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.txt"
    evidence.write_bytes(b"first snapshot")
    old = replace(context(), evidence_digests=(("E-1", hashlib.sha256(evidence.read_bytes()).hexdigest()),))
    evidence.write_bytes(b"changed snapshot")
    current = replace(old, evidence_digests=(("E-1", hashlib.sha256(evidence.read_bytes()).hexdigest()),))
    assert old.evidence_refs == current.evidence_refs
    assert old.fingerprint != current.fingerprint
    results = tuple(contextual_reviewer(old, name, ReviewVerdict.PASS) for name in ("A", "B"))
    arbiter = ArbiterResult("arbiter", 1, ReviewVerdict.PASS, "通过", "旧证据", ("A", "B"), old.fingerprint, False)
    with pytest.raises(ContractValidationError, match="same ReviewContext"):
        evaluate_review_cycle(current, ReviewRisk.HIGH, results, arbiter, None, round_number=1)


def test_evidence_digests_are_frozen_canonical_and_round_trip() -> None:
    digests = [["E-2", "B" * 64], ["E-1", "A" * 64]]
    ctx = replace(context(), evidence_refs=("E-1", "E-2"), evidence_digests=digests)
    original_fingerprint = ctx.fingerprint
    digests[0][1] = "c" * 64
    digests.append(["extra", "d" * 64])
    assert ctx.evidence_digests == (("E-1", "a" * 64), ("E-2", "b" * 64))
    assert ctx.fingerprint == original_fingerprint
    reverse = replace(ctx, evidence_digests=tuple(reversed(ctx.evidence_digests)))
    assert reverse.fingerprint == ctx.fingerprint
    payload = ctx.to_dict()
    assert payload["evidence_digests"] == [["E-1", "a" * 64], ["E-2", "b" * 64]]
    assert ReviewContext.from_dict(payload) == ctx
    payload["evidence_digests"][0][1] = "f" * 64
    with pytest.raises(ContractValidationError, match="fingerprint"):
        ReviewContext.from_dict(payload)


def test_legacy_context_fingerprint_and_wire_format_are_preserved() -> None:
    ctx = ReviewContext("goal", "prompt", (), ("E-1",), "candidate")
    assert ctx.fingerprint == "e7719e03623e6f3daec18cc12881405a295dd9a1287e65e28e2ba8a7b40865b8"
    assert "evidence_digests" not in ctx.to_dict()
    assert replace(ctx, evidence_digests=()).to_dict() == ctx.to_dict()
    assert ReviewContext.from_dict(ctx.to_dict()) == ctx


@pytest.mark.parametrize(
    "digests",
    [
        None, "invalid", {"E-1": "a" * 64}, ["E-1"], [["E-1"]], [["E-1", "a" * 64, "extra"]],
        [["E-1", None]], [[1, "a" * 64]], [["E-1", "a" * 63]], [["E-1", "z" * 64]],
        [["E-1", "a" * 64], ["E-1", "b" * 64]], [["unknown", "a" * 64]],
    ],
)
def test_invalid_evidence_digests_are_rejected(digests) -> None:
    with pytest.raises(ContractValidationError, match="evidence_digests"):
        replace(context(), evidence_digests=digests)


def test_nonempty_digests_require_complete_reference_coverage() -> None:
    with pytest.raises(ContractValidationError, match="evidence_digests"):
        replace(context(), evidence_refs=("E-1", "E-2"), evidence_digests=(("E-1", "a" * 64),))
    duplicated_refs = replace(context(), evidence_refs=("E-1", "E-1"), evidence_digests=(("E-1", "a" * 64),))
    assert duplicated_refs.evidence_digests == (("E-1", "a" * 64),)


def test_high_risk_requires_meta_verdict_to_match_arbiter() -> None:
    ctx = context()
    results = (
        contextual_reviewer(ctx, "A", ReviewVerdict.PASS),
        contextual_reviewer(ctx, "B", ReviewVerdict.PASS),
    )
    arbiter = ArbiterResult(
        "arbiter", 1, ReviewVerdict.PASS, "通过", "证据一致", ("A", "B"), ctx.fingerprint, False
    )
    meta = MetaReviewResult(
        "meta-1", 1, "arbiter", ReviewVerdict.WARN, "有保留意见", (finding("META-1"),),
        ctx.fingerprint, True,
    )
    result = evaluate_review_cycle(
        ctx, ReviewRisk.HIGH, results, arbiter, meta, round_number=1
    )
    assert result.outcome is ReviewOutcome.REVIEW_REQUIRED
    assert result.dispute_packet is not None

    medium = evaluate_review_cycle(
        ctx, ReviewRisk.MEDIUM, results, arbiter, meta, round_number=1
    )
    assert medium.outcome is ReviewOutcome.WARN
    assert medium.dispute_packet is None


def test_runner_calls_multiple_agents_with_same_context_and_retries_dispute() -> None:
    ctx = context()
    reviewer_calls: list[tuple[str, int, bool]] = []
    arbiter_calls: list[int] = []
    meta_calls: list[int] = []

    def reviewer_call(review_context, reviewer_id, round_number, dispute):
        reviewer_calls.append((reviewer_id, round_number, dispute is not None))
        verdict = ReviewVerdict.FAIL if round_number == 1 and reviewer_id == "reviewer-2" else ReviewVerdict.PASS
        return contextual_reviewer(review_context, reviewer_id, verdict, round_number)

    def arbiter_call(review_context, results, round_number, dispute):
        arbiter_calls.append(round_number)
        return ArbiterResult(
            "arbiter",
            round_number,
            ReviewVerdict.PASS,
            "汇总结果",
            "第二轮已重新比较证据" if round_number == 2 else "主 Agent 需要复审",
            tuple(item.reviewer_id for item in results),
            review_context.fingerprint,
            round_number == 1,
        )

    def meta_review_call(review_context, reviewer_results, arbiter, round_number):
        meta_calls.append(round_number)
        assert tuple(item.reviewer_id for item in reviewer_results) == (
            "reviewer-1",
            "reviewer-2",
        )
        return MetaReviewResult(
            "meta",
            round_number,
            arbiter.arbiter_id,
            ReviewVerdict.FAIL if round_number == 1 else ReviewVerdict.PASS,
            "复核结果",
            () if round_number == 2 else (finding("META-R1"),),
            review_context.fingerprint,
            round_number == 2,
        )

    result = run_review_cycle(ctx, ReviewRisk.HIGH, reviewer_call, arbiter_call, meta_review_call)

    assert result.outcome is ReviewOutcome.PASS
    assert arbiter_calls == [1, 2]
    assert meta_calls == [1, 2]
    assert reviewer_calls == [
        ("reviewer-1", 1, False),
        ("reviewer-2", 1, False),
        ("reviewer-1", 2, True),
        ("reviewer-2", 2, True),
    ]
