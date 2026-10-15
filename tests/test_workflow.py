from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.questions import Question, QuestionOption
from goalanchor.review_delivery import build_packets
from goalanchor.review_service import ArbiterResult, MetaReviewResult, ReviewerResult, ReviewVerdict
from goalanchor.workflow import Workflow, WorkflowPolicy
from goalanchor.workflow_protocol import (
    Baseline,
    Criterion,
    EventAuthority,
    EvidenceSnapshot,
    Operation,
    canonical,
    fingerprint,
)
from goalanchor.workflow_store import WorkflowStore

NOW = "2026-10-16T01:00:00+08:00"


def setup_workflow(tmp_path: Path):
    user = EventAuthority(b"u" * 32, "trusted-ui")
    observer = EventAuthority(b"o" * 32, "trusted-runner")
    runtime = Workflow(
        WorkflowStore(tmp_path / "state.db"),
        user,
        observer,
        WorkflowPolicy(str(tmp_path), ("write_text",)),
        clock=lambda: NOW,
    )
    baseline = Baseline(
        "G1",
        1,
        "deliver artifact",
        "write and verify",
        (Criterion("C1", "artifact content", canonical("done"), "artifact"),),
        required_operations=("O1",),
    )
    runtime.start_baseline(baseline, user.sign("user.baseline", baseline.fingerprint, baseline.to_dict(), at=NOW))
    snapshot = EvidenceSnapshot.capture("E1", "plan-evidence", b"frozen facts", source="test-runner", collected_at=NOW)
    runtime.add_evidence(
        "G1",
        snapshot,
        observer.sign(
            "observer.evidence",
            snapshot.fingerprint,
            {"goal_id": "G1", "snapshot": snapshot.to_dict()},
            at=NOW,
        ),
    )
    return runtime, baseline, user, observer


def synthetic_delivery(runtime, bundle):
    """Explicit local fixture attestation; no actual model transport is claimed."""
    previous = runtime.store.get("review", "G1")
    prior = (
        previous["history"]
        if previous and previous["context_fingerprint"] == runtime.review_context("G1").fingerprint
        else []
    )
    packets = build_packets(runtime.review_context("G1"), bundle, prior)
    bundle["delivery_receipts"] = [
        {
            "agent_id": binding["agent_id"],
            "session_id": binding["session_id"],
            "input_digest": fingerprint(packets[binding["agent_id"]]),
            "transport_receipt_id": "synthetic-" + binding["session_id"],
            "status": "DELIVERED",
        }
        for binding in bundle["bindings"]
    ]


def review(runtime, observer, *, finding=None, dispositions=None):
    ctx = runtime.review_context("G1")
    reports = [
        ReviewerResult(name, 1, ReviewVerdict.PASS, () if finding is None else (finding,), True, ctx.fingerprint)
        for name in ("A", "B")
    ]
    arbiter = ArbiterResult(
        "arbiter", 1, ReviewVerdict.PASS, "pass", "frozen evidence", ("A", "B"), ctx.fingerprint, False
    )
    meta = MetaReviewResult("meta", 1, "arbiter", ReviewVerdict.PASS, "checked raw reports", (), ctx.fingerprint, True)
    bundle = {
        "context": ctx.to_dict(),
        "risk": "HIGH",
        "round_number": 1,
        "max_rounds": 2,
        "reviewer_results": [item.to_dict() for item in reports],
        "arbiter": arbiter.to_dict(),
        "meta_review": meta.to_dict(),
        "dispositions": dispositions or {},
        "bindings": [
            {"agent_id": name, "session_id": name + "-session", "model": "test-model"}
            for name in ("A", "B", "arbiter", "meta")
        ],
    }
    synthetic_delivery(runtime, bundle)
    event = observer.sign("observer.review", ctx.fingerprint, bundle, at=NOW)
    return runtime.submit_review("G1", bundle, event)


def operation(tmp_path, baseline):
    return Operation(
        "O1",
        "G1",
        baseline.fingerprint,
        "write_text",
        canonical({"path": "artifact.txt", "text": "done"}),
        str(tmp_path),
        "W1",
    )


def approve(runtime, op, user):
    runtime.register_operation(op)
    record = runtime.request_authorization(op.operation_id)
    payload = {"answer_id": "answer-" + record.question.question_id, "selected": ["approve"], "custom": None}
    event = user.sign("user.answer", record.question.fingerprint, payload, at=NOW)
    return runtime.answer(record.question.question_id, event)


def test_strict_chain_executes_once_and_closes_only_with_observation(tmp_path: Path) -> None:
    runtime, base, user, observer = setup_workflow(tmp_path)
    assert review(runtime, observer)["status"] == "PASSED"
    op = operation(tmp_path, base)
    assert approve(runtime, op, user).decision == "APPROVE"
    calls = []

    def execute(actual):
        calls.append(actual.fingerprint)
        (tmp_path / "artifact.txt").write_text("done", encoding="utf-8")
        return {"exit_code": 0, "object_ref": "artifact"}

    receipt = runtime.execute("O1", "attempt-1", execute)
    assert receipt["state"] == "SUCCEEDED"
    assert runtime.execute("O1", "attempt-1", execute) == receipt
    assert len(calls) == 1
    with pytest.raises(ContractValidationError):
        runtime.close("G1")
    observed = {
        "goal_id": "G1",
        "baseline_digest": base.fingerprint,
        "criterion_id": "C1",
        "object_ref": "artifact",
        "actual": (tmp_path / "artifact.txt").read_text(encoding="utf-8"),
        "attempt_id": "attempt-1",
    }
    runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, observed, at=NOW))
    assert runtime.close("G1")["state"] == "CLOSED"


def test_forged_answer_cannot_issue_grant(tmp_path: Path) -> None:
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    op = operation(tmp_path, base)
    runtime.register_operation(op)
    q = runtime.request_authorization("O1").question
    forged = EventAuthority(b"x" * 32, "trusted-ui").sign(
        "user.answer",
        q.fingerprint,
        {"answer_id": "fake", "selected": ["approve"], "actor": "USER"},
        at=NOW,
    )
    with pytest.raises(ContractValidationError):
        runtime.answer(q.question_id, forged)
    assert runtime.store.list("grant", "G1") == ()


def test_unanswered_question_only_blocks_dependent_work(tmp_path: Path) -> None:
    runtime, base, user, observer = setup_workflow(tmp_path)
    q = Question(
        "Q1",
        "G1",
        1,
        "direction",
        "which path",
        (QuestionOption("a", "A"),),
        blocking_items=("W-blocked",),
        baseline_digest=base.fingerprint,
    )
    runtime.request_question(q)
    assert runtime.work_item_decision("G1", "W-blocked") == "WAIT"
    assert runtime.work_item_decision("G1", "W-independent") == "ALLOW"
    assert runtime.request_question(replace(q, question_id="Q-alias")).question.question_id == "Q1"


def test_changed_baseline_rejects_old_answer_and_execution(tmp_path: Path) -> None:
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    op = operation(tmp_path, base)
    runtime.register_operation(op)
    q = runtime.request_authorization("O1").question
    new = replace(base, revision=2, plan="new plan")
    runtime.start_baseline(new, user.sign("user.baseline", new.fingerprint, new.to_dict(), at=NOW))
    answer = user.sign(
        "user.answer", q.fingerprint, {"answer_id": "late", "selected": ["approve"], "custom": None}, at=NOW
    )
    with pytest.raises(ContractValidationError):
        runtime.answer(q.question_id, answer)
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "old-attempt", lambda _: {"exit_code": 0})


def test_unknown_execution_is_not_replayed_or_closed(tmp_path: Path) -> None:
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    calls = []

    def lost_result(_):
        calls.append(1)
        raise TimeoutError("external result unknown")

    assert runtime.execute("O1", "lost", lost_result)["state"] == "UNKNOWN"
    assert runtime.execute("O1", "lost", lost_result)["state"] == "UNKNOWN"
    assert calls == [1]
    with pytest.raises(ContractValidationError):
        runtime.close("G1")


def test_decline_is_reused_not_reasked_for_same_operation(tmp_path: Path) -> None:
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    runtime.register_operation(operation(tmp_path, base))
    q = runtime.request_authorization("O1").question
    answer = user.sign("user.answer", q.fingerprint, {"answer_id": "no", "selected": ["deny"], "custom": None}, at=NOW)
    assert runtime.answer(q.question_id, answer).decision == "DENY"
    assert runtime.request_authorization("O1").decision == "DENY"
    assert runtime.answer(q.question_id, answer).decision == "DENY"
    assert runtime.store.list("grant", "G1") == ()


def test_hard_policy_and_expired_evidence_cannot_be_overridden(tmp_path: Path) -> None:
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    forbidden = replace(operation(tmp_path, base), tool="forbidden")
    with pytest.raises(ContractValidationError):
        runtime.register_operation(forbidden)
    expired = EvidenceSnapshot.capture(
        "E-expired",
        "artifact",
        b"old",
        source="runner",
        collected_at="2026-10-15T00:00:00+08:00",
        expires_at="2026-10-15T01:00:00+08:00",
    )
    with pytest.raises(ContractValidationError):
        runtime.add_evidence(
            "G1",
            expired,
            observer.sign(
                "observer.evidence",
                expired.fingerprint,
                {"goal_id": "G1", "snapshot": expired.to_dict()},
                at=NOW,
            ),
        )


def test_receipt_replay_and_close_are_idempotent_after_closure(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    receipt = runtime.execute("O1", "A", lambda _: {"exit_code": 0, "object_ref": "artifact"})
    payload = {
        "goal_id": "G1",
        "baseline_digest": base.fingerprint,
        "criterion_id": "C1",
        "object_ref": "artifact",
        "actual": "done",
        "attempt_id": "A",
    }
    runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, payload, at=NOW))
    closed = runtime.close("G1")
    assert runtime.close("G1") == closed
    assert runtime.execute("O1", "A", lambda _: pytest.fail("must not redispatch")) == receipt


def test_signed_observation_predating_dispatch_is_rejected(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    runtime.execute("O1", "A", lambda _: {"exit_code": 0, "object_ref": "artifact"})
    payload = {
        "goal_id": "G1",
        "baseline_digest": base.fingerprint,
        "criterion_id": "C1",
        "object_ref": "artifact",
        "actual": "done",
        "attempt_id": "A",
    }
    with pytest.raises(ContractValidationError):
        runtime.observe(
            "G1", observer.sign("observer.observation", base.fingerprint, payload, at="2026-10-15T01:00:00+08:00")
        )


def test_accepted_risk_cannot_bypass_policy_allowlist(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    payload = {
        "goal_id": "G1",
        "baseline_digest": base.fingerprint,
        "finding_id": "F1",
        "reason": "accept",
        "expires_at": "2026-10-16T02:00:00+08:00",
    }
    with pytest.raises(ContractValidationError):
        runtime.accept_risk("G1", "F1", user.sign("user.risk", base.fingerprint, payload, at=NOW))


@pytest.mark.parametrize("selected", ["approve", {"approve": True}])
def test_answer_requires_selection_array(tmp_path, selected):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    runtime.register_operation(operation(tmp_path, base))
    q = runtime.request_authorization("O1").question
    event = user.sign(
        "user.answer", q.fingerprint, {"answer_id": "malformed", "selected": selected, "custom": None}, at=NOW
    )
    with pytest.raises(ContractValidationError):
        runtime.answer(q.question_id, event)
    assert runtime.store.list("grant", "G1") == ()


def test_cancellation_during_dispatch_retains_receipt_without_reviving_goal(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)

    def executor(_):
        runtime.cancel(
            "G1", user.sign("user.stop", base.fingerprint, {"goal_id": "G1", "reason": "stop during effect"}, at=NOW)
        )
        return {"exit_code": 0, "object_ref": "artifact"}

    receipt = runtime.execute("O1", "running", executor)
    assert receipt["state"] == "SUCCEEDED"
    assert runtime.store.get("goal", "G1")["state"] == "STOPPED"
    assert runtime.execute("O1", "running", executor) == receipt


def test_expired_accepted_risk_invalidates_review_before_dispatch_and_closure(tmp_path):
    from copy import deepcopy

    from goalanchor.integration import ReviewFinding

    runtime, base, user, observer = setup_workflow(tmp_path)
    runtime.policy = replace(runtime.policy, acceptable_risks=("F1",))
    # Policy is fixed at baseline creation; use a new signed baseline for this policy.
    base = replace(base, revision=2)
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict(), at=NOW))
    finding = ReviewFinding("F1", "bounded residual risk", ("E1",), "needs acceptance", "known")
    pending = review(runtime, observer, finding=finding)
    payload = {
        "goal_id": "G1",
        "baseline_digest": base.fingerprint,
        "finding_id": "F1",
        "reason": "accept bounded risk",
        "expires_at": "2026-10-16T01:01:00+08:00",
    }
    runtime.accept_risk("G1", "F1", user.sign("user.risk", base.fingerprint, payload, at=NOW))
    bundle = deepcopy(pending["history"][0])
    bundle["round_number"] = 2
    for key in ("reviewer_results",):
        for report in bundle[key]:
            report["round_number"] = 2
    bundle["arbiter"]["round_number"] = 2
    bundle["meta_review"]["round_number"] = 2
    for binding in bundle["bindings"]:
        binding["session_id"] += "-second"
    bundle["dispositions"] = {"F1": {"status": "RISK_ACCEPTED", "evidence_refs": ["E1"]}}
    ctx = runtime.review_context("G1")
    synthetic_delivery(runtime, bundle)
    assert (
        runtime.submit_review("G1", bundle, observer.sign("observer.review", ctx.fingerprint, bundle, at=NOW))["status"]
        == "PASSED"
    )
    approve(runtime, operation(tmp_path, base), user)
    original_reserve = runtime.store.reserve

    def reserve_after_risk_deadline(*args, **kwargs):
        runtime.clock = lambda: "2026-10-16T01:02:00+08:00"
        return original_reserve(*args, **kwargs)

    runtime.store.reserve = reserve_after_risk_deadline
    with pytest.raises(ContractValidationError, match="expired"):
        runtime.execute("O1", "risk-crossed", lambda _: pytest.fail("must not dispatch"))
    assert runtime.store.list("receipt", "G1") == ()
    runtime.clock = lambda: "2026-10-16T01:02:00+08:00"
    with pytest.raises(ContractValidationError, match="risk"):
        runtime.execute("O1", "expired-risk", lambda _: pytest.fail("must not dispatch"))
    with pytest.raises(ContractValidationError, match="risk"):
        runtime.close("G1")


@pytest.mark.parametrize("scope", [False, True])
def test_evidence_deadline_is_atomic_dispatch_bound(tmp_path, scope):
    runtime, base, user, observer = setup_workflow(tmp_path)
    expiry = "2026-10-16T01:01:00+08:00"
    snapshot = EvidenceSnapshot.capture(
        "short", "plan", b"short-lived", source="runner", collected_at=NOW, expires_at=expiry
    )
    runtime.add_evidence(
        "G1",
        snapshot,
        observer.sign(
            "observer.evidence", snapshot.fingerprint, {"goal_id": "G1", "snapshot": snapshot.to_dict()}, at=NOW
        ),
    )
    review(runtime, observer)
    op = operation(tmp_path, base)
    if scope:
        payload = {
            "goal_id": "G1",
            "baseline_digest": base.fingerprint,
            "scope_id": "S",
            "allowed_tools": ["write_text"],
            "workspace": str(tmp_path),
            "expires_at": "2026-10-16T02:00:00+08:00",
        }
        runtime.authorize_scope("G1", "S", user.sign("user.scope", base.fingerprint, payload, at=NOW))
        runtime.register_operation(op)
    else:
        approve(runtime, op, user)
    original_reserve = runtime.store.reserve

    def crossed_deadline(*args, **kwargs):
        # The evidence/review checks already finished. The transactional gate owns time.
        runtime.clock = lambda: "2026-10-16T01:02:00+08:00"
        return original_reserve(*args, **kwargs)

    runtime.store.reserve = crossed_deadline
    with pytest.raises(ContractValidationError, match="expired"):
        runtime.execute("O1", "deadline", lambda _: pytest.fail("expired evidence must never dispatch"))
    assert runtime.store.list("receipt", "G1") == ()
