from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
from test_strict_baseline_evidence import (
    NOW,
    baseline_event,
    make_world,
    write_baseline,
    write_evidence,
)

from goalanchor.integration import ReviewFinding
from goalanchor.review_delivery import build_packets
from goalanchor.review_service import ArbiterResult, MetaReviewResult, ReviewerResult, ReviewRisk, ReviewVerdict
from goalanchor.workflow import StrictWorkflowError
from goalanchor.workflow_protocol import Operation


def _bundle(runtime, *, round_number=1, prior_rounds=None, with_finding=False):
    context = runtime.review_context("goal-1")
    findings = (
        (ReviewFinding("finding-1", "unresolved objection", ("evidence-1",), "blocks", "claim"),)
        if with_finding else ()
    )
    verdict = ReviewVerdict.WARN if with_finding else ReviewVerdict.PASS
    reviewers = (
        ReviewerResult(
            "reviewer-1", round_number, verdict, findings=findings, context_fingerprint=context.fingerprint
        ),
        ReviewerResult(
            "reviewer-2", round_number, verdict, findings=findings, context_fingerprint=context.fingerprint
        ),
    )
    arbiter = ArbiterResult(
        "arbiter", round_number, ReviewVerdict.PASS, "all checks pass", "same conclusion",
        ("reviewer-1", "reviewer-2"), context.fingerprint, False,
    )
    meta = MetaReviewResult(
        "meta", round_number, "arbiter", ReviewVerdict.PASS, "confirmed", (),
        context.fingerprint, True,
    )
    bundle = {
        "context": context.to_dict(),
        "risk": ReviewRisk.MEDIUM.value,
        "round_number": round_number,
        "max_rounds": 2,
        "reviewer_results": [item.to_dict() for item in reviewers],
        "arbiter": arbiter.to_dict(),
        "meta_review": meta.to_dict(),
        "dispositions": {} if not with_finding else {"finding-1": {"status": "OPEN", "evidence_refs": []}},
        "bindings": [
            {"agent_id": "reviewer-1", "session_id": "s1", "model": "m"},
            {"agent_id": "reviewer-2", "session_id": "s2", "model": "m"},
            {"agent_id": "arbiter", "session_id": "s3", "model": "m"},
            {"agent_id": "meta", "session_id": "s4", "model": "m"},
        ],
    }
    packets = build_packets(context, bundle, prior_rounds or [])
    bundle["delivery_receipts"] = [
        {
            "agent_id": agent_id,
            "session_id": item["session_id"],
            "input_digest": runtime.store.fingerprint(packets[agent_id]),
            "transport_receipt_id": "receipt-" + agent_id,
            "status": "DELIVERED",
        }
        for agent_id, item in ((item["agent_id"], item) for item in bundle["bindings"])
    ]
    return bundle


def _ready_runtime(tmp_path):
    runtime, baseline, user, observer, provider, _ = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    write_baseline(runtime, baseline, event, provider)
    write_evidence(runtime, baseline, observer, provider)
    return runtime, observer


def _ready_operation_runtime(tmp_path):
    runtime, baseline, user, observer, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    write_evidence(runtime, baseline, observer, provider)
    operation = Operation(
        "op-1", baseline.goal_id, baseline.fingerprint, "write_text", "{}", str(tmp_path), "item-1"
    )
    runtime.register_operation(operation)
    return runtime, baseline, user, observer


def test_strict_review_packet_and_submission_are_persisted(tmp_path):
    runtime, observer = _ready_runtime(tmp_path)
    bundle = _bundle(runtime)
    packets = runtime.review_packets("goal-1", bundle)
    assert set(packets) == {"reviewer-1", "reviewer-2", "arbiter", "meta"}
    event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, bundle, at=NOW)
    result = runtime.submit_review("goal-1", bundle, event)
    assert result["status"] == "PASSED"
    assert runtime.store.get("strict_review_record", "goal-1")["context_fingerprint"] == result["context_fingerprint"]
    chain = runtime.store.list("strict_review_chain", "goal-1")
    assert len(chain) == 1 and chain[0]["round_number"] == 1
    assert runtime.store.get("strict_review_head", "goal-1")["chain_key"] == "goal-1:1"


def test_strict_review_submission_is_idempotent_for_same_event(tmp_path):
    runtime, observer = _ready_runtime(tmp_path)
    bundle = _bundle(runtime)
    event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, bundle, at=NOW)
    first = runtime.submit_review("goal-1", bundle, event)
    assert runtime.submit_review("goal-1", deepcopy(bundle), event) == first


def test_strict_review_rejects_changed_context(tmp_path):
    runtime, observer = _ready_runtime(tmp_path)
    bundle = _bundle(runtime)
    changed = deepcopy(bundle)
    changed["context"]["candidate"] = "changed"
    event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, changed, at=NOW)
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.submit_review("goal-1", changed, event)
    assert captured.value.code == "UNTRUSTED"


def test_strict_review_stops_after_unresolved_second_round(tmp_path):
    runtime, observer = _ready_runtime(tmp_path)
    first = _bundle(runtime, with_finding=True)
    first_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, first, at=NOW)
    first_result = runtime.submit_review("goal-1", first, first_event)
    assert first_result["status"] == "REVIEW_REQUIRED"
    second = _bundle(runtime, round_number=2, prior_rounds=[first], with_finding=True)
    second["bindings"] = [
        {**item, "session_id": item["session_id"] + "-round2"} for item in second["bindings"]
    ]
    second["delivery_receipts"] = [
        {**item, "session_id": item["session_id"] + "-round2"} for item in second["delivery_receipts"]
    ]
    context = runtime.review_context("goal-1")
    second_event = observer.sign("observer.review", context.fingerprint, second, at=NOW)
    stopped = runtime.submit_review("goal-1", second, second_event)
    assert stopped["status"] == "STOPPED"
    assert stopped["unresolved"] == ["finding-1"]

def test_strict_review_rejects_corrupted_persisted_history(tmp_path):
    runtime, observer = _ready_runtime(tmp_path)
    first = _bundle(runtime, with_finding=True)
    first_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, first, at=NOW)
    runtime.submit_review("goal-1", first, first_event)
    stored = runtime.store.get("strict_review_record", "goal-1")
    corrupted = deepcopy(stored)
    corrupted["history"][0]["reviewer_results"][0].pop("findings")
    corruption_event = observer.sign("controller.test.corrupt", "goal-1", corrupted, at=NOW)
    runtime.store.commit(
        [("strict_review_record", "goal-1", corrupted)],
        {("strict_review_record", "goal-1"): runtime.store.fingerprint(stored)},
        corruption_event.event_id,
        corruption_event.to_dict(),
    )
    second = _bundle(runtime, round_number=2, prior_rounds=[first], with_finding=True)
    second["bindings"] = [
        {**item, "session_id": item["session_id"] + "-round2"} for item in second["bindings"]
    ]
    second["delivery_receipts"] = [
        {**item, "session_id": item["session_id"] + "-round2"} for item in second["delivery_receipts"]
    ]
    second_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, second, at=NOW)
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.submit_review("goal-1", second, second_event)
    assert captured.value.code == "UNTRUSTED"


def test_strict_authorization_question_and_one_time_grant(tmp_path):
    runtime, baseline, user, observer = _ready_operation_runtime(tmp_path)
    context = runtime.review_context("goal-1")
    bundle = _bundle(runtime)
    event = observer.sign("observer.review", context.fingerprint, bundle, at=NOW)
    assert runtime.submit_review("goal-1", bundle, event)["status"] == "PASSED"
    question = runtime.request_authorization("op-1")
    assert question.question.kind == "AUTHORIZATION"
    answer_event = user.sign(
        "user.answer",
        question.question.fingerprint,
        {"answer_id": "approve-1", "selected": ["approve"], "custom": None},
        at=NOW,
    )
    answered = runtime.answer(question.question.question_id, answer_event)
    assert answered.decision == "APPROVE"
    grants = runtime.store.list("strict_grant", baseline.goal_id)
    assert len(grants) == 1 and grants[0]["state"] == "ACTIVE"
    calls = []
    receipt = runtime.execute(
        "op-1", "attempt-1", lambda operation: calls.append(operation.operation_id) or {"exit_code": 0}
    )
    assert receipt["state"] == "SUCCEEDED" and calls == ["op-1"]
    assert runtime.execute("op-1", "attempt-1", lambda _: {"exit_code": 99}) == receipt
    assert runtime.store.list("strict_grant", baseline.goal_id)[0]["state"] == "CONSUMED"
    observation_payload = {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "criterion_id": "artifact",
        "object_ref": "artifact.txt",
        "actual": "done",
        "attempt_id": "attempt-1",
    }
    observation_event = observer.sign(
        "observer.observation", baseline.fingerprint, observation_payload, at=NOW
    )
    assert runtime.observe(baseline.goal_id, observation_event)["status"] == "PASS"
    closure = runtime.close(baseline.goal_id)
    assert closure["state"] == "CLOSED"
    assert runtime.close(baseline.goal_id) == closure

def test_strict_authorization_answer_rejects_runtime_identity_rotation(tmp_path):
    runtime, _baseline, user, observer = _ready_operation_runtime(tmp_path)
    bundle = _bundle(runtime)
    review_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, bundle, at=NOW)
    runtime.submit_review("goal-1", bundle, review_event)
    question = runtime.request_authorization("op-1")
    runtime.runtime_binding_provider.rotate(session_id="rotated-session")
    answer_event = user.sign(
        "user.answer",
        question.question.fingerprint,
        {"answer_id": "approve-rotated", "selected": ["approve"], "custom": None},
        at=NOW,
    )
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.answer(question.question.question_id, answer_event)
    assert captured.value.code == "STALE"


def test_strict_execution_failure_becomes_unknown_and_cannot_reuse_grant(tmp_path):
    runtime, _baseline, user, observer = _ready_operation_runtime(tmp_path)
    bundle = _bundle(runtime)
    review_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, bundle, at=NOW)
    runtime.submit_review("goal-1", bundle, review_event)
    question = runtime.request_authorization("op-1")
    answer_event = user.sign(
        "user.answer", question.question.fingerprint,
        {"answer_id": "approve-1", "selected": ["approve"], "custom": None}, at=NOW,
    )
    runtime.answer(question.question.question_id, answer_event)
    receipt = runtime.execute("op-1", "attempt-unknown", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    assert receipt["state"] == "UNKNOWN"
    reconcile_payload = {
        "attempt_id": "attempt-unknown",
        "operation_digest": receipt["operation_digest"],
        "outcome": "SUCCEEDED",
        "result": {"exit_code": 0, "object_ref": "artifact"},
    }
    reconcile_event = observer.sign("observer.reconcile", "attempt-unknown", reconcile_payload, at=NOW)
    reconciled = runtime.reconcile("attempt-unknown", reconcile_event)
    assert reconciled["state"] == "SUCCEEDED"
    assert runtime.reconcile("attempt-unknown", reconcile_event) == reconciled
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.execute("op-1", "attempt-next", lambda _: {"exit_code": 0})
    assert captured.value.code == "UNTRUSTED"
