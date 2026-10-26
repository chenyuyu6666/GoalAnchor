from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from test_strict_baseline_evidence import (
    NOW,
    baseline_event,
    make_world,
    register_evidence,
    write_baseline,
    write_evidence,
)

from goalanchor.integration import ReviewFinding
from goalanchor.review_delivery import build_packets
from goalanchor.review_service import ArbiterResult, MetaReviewResult, ReviewerResult, ReviewRisk, ReviewVerdict
from goalanchor.workflow import StrictWorkflowError
from goalanchor.workflow_protocol import EvidenceSnapshot, Operation, canonical


def _bundle(runtime, *, round_number=1, prior_rounds=None, with_finding=False, dispositions=None):
    context = runtime.review_context("goal-1")
    findings = (
        (ReviewFinding("finding-1", "unresolved objection", ("evidence-1",), "blocks", "claim"),)
        if with_finding else ()
    )
    verdict = ReviewVerdict.PASS
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
        "dispositions": (
            dispositions
            if dispositions is not None
            else {} if not with_finding else {"finding-1": {"status": "OPEN", "evidence_refs": []}}
        ),
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
    packets = build_packets(runtime.review_context("goal-1"), second, [first])
    second["delivery_receipts"] = [
        {
            **item,
            "input_digest": runtime.store.fingerprint(packets[item["agent_id"]]),
        }
        for item in second["delivery_receipts"]
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
    packets = build_packets(runtime.review_context("goal-1"), second, [first])
    second["delivery_receipts"] = [
        {**item, "input_digest": runtime.store.fingerprint(packets[item["agent_id"]])}
        for item in second["delivery_receipts"]
    ]
    second_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, second, at=NOW)
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.submit_review("goal-1", second, second_event)
    assert captured.value.code == "UNTRUSTED"


def test_strict_review_rejects_corrupted_append_only_chain(tmp_path):
    runtime, observer = _ready_runtime(tmp_path)
    first = _bundle(runtime)
    first_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, first, at=NOW)
    runtime.submit_review("goal-1", first, first_event)
    stored = runtime.store.get("strict_review_chain", "goal-1:1")
    corrupted = deepcopy(stored)
    corrupted["bundle"]["context"]["candidate"] = "changed"
    corruption_event = observer.sign("controller.test.corrupt-chain", "goal-1:1", corrupted, at=NOW)
    runtime.store.commit(
        [("strict_review_chain", "goal-1:1", corrupted)],
        {("strict_review_chain", "goal-1:1"): runtime.store.fingerprint(stored)},
        corruption_event.event_id,
        corruption_event.to_dict(),
    )
    second = _bundle(runtime, round_number=2, prior_rounds=[first])
    second["bindings"] = [
        {**item, "session_id": item["session_id"] + "-round2"} for item in second["bindings"]
    ]
    second["delivery_receipts"] = [
        {**item, "session_id": item["session_id"] + "-round2"} for item in second["delivery_receipts"]
    ]
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.submit_review(
            "goal-1", second,
            observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, second, at=NOW),
        )
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


def test_strict_reconciled_unknown_can_close_without_review_evidence_drift(tmp_path):
    """Post-review recovery evidence must not invalidate the frozen Strict review."""
    runtime, baseline, user, observer = _ready_operation_runtime(tmp_path)
    bundle = _bundle(runtime)
    event = observer.sign("observer.review", runtime.review_context(baseline.goal_id).fingerprint, bundle, at=NOW)
    assert runtime.submit_review(baseline.goal_id, bundle, event)["status"] == "PASSED"
    question = runtime.request_authorization("op-1")
    runtime.answer(
        question.question.question_id,
        user.sign("user.answer", question.question.fingerprint,
                  {"answer_id": "approve-1", "selected": ["approve"], "custom": None}, at=NOW),
    )

    def lost(_operation):
        raise TimeoutError("result unknown")

    receipt = runtime.execute("op-1", "attempt-lost", lost)
    assert receipt["state"] == "UNKNOWN"
    reconcile_payload = {
        "attempt_id": "attempt-lost",
        "operation_digest": receipt["operation_digest"],
        "outcome": "SUCCEEDED",
        "result": {"exit_code": 0, "object_ref": "artifact.txt"},
    }
    reconciled = runtime.reconcile(
        "attempt-lost",
        observer.sign("observer.reconcile", "attempt-lost", reconcile_payload, at=NOW),
    )
    assert reconciled["state"] == "SUCCEEDED"
    observation_payload = {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "criterion_id": "artifact",
        "object_ref": "artifact.txt",
        "actual": "done",
        "attempt_id": "attempt-lost",
    }
    assert runtime.observe(
        baseline.goal_id, observer.sign("observer.observation", baseline.fingerprint, observation_payload, at=NOW)
    )["status"] == "PASS"
    assert runtime.close(baseline.goal_id)["state"] == "CLOSED"

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


def test_strict_scope_authorization_derives_one_time_grant_and_revoke_blocks(tmp_path):
    runtime, baseline, user, observer = _ready_operation_runtime(tmp_path)
    bundle = _bundle(runtime)
    review_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, bundle, at=NOW)
    runtime.submit_review("goal-1", bundle, review_event)
    scope_payload = {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "scope_id": "scope-1",
        "allowed_tools": ["write_text"],
        "workspace": str(tmp_path),
        "expires_at": "2026-10-16T02:00:00+08:00",
    }
    scope_event = user.sign("user.scope", baseline.goal_id, scope_payload, at=NOW)
    scope = runtime.authorize_scope(baseline.goal_id, "scope-1", scope_event)
    assert scope["kind"] == "AUTONOMY_GRANT"
    revoked = runtime.revoke_authorization(
        "scope-1", user.sign("user.revoke", "scope-1", {"grant_id": "scope-1"}, at=NOW)
    )
    assert revoked["state"] == "REVOKED"
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.execute("op-1", "scope-attempt", lambda _: {"exit_code": 0})
    assert captured.value.code == "UNTRUSTED"


def test_strict_risk_acceptance_requires_policy_and_second_round_disposition(tmp_path):
    runtime, baseline, user, observer, provider, _ = make_world(tmp_path)
    runtime.policy = replace(runtime.policy, acceptable_risks=("finding-1",))
    runtime.runtime_binding_provider.policy = runtime.policy
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    write_evidence(runtime, baseline, observer, provider)
    first = _bundle(runtime, with_finding=True)
    first_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, first, at=NOW)
    assert runtime.submit_review("goal-1", first, first_event)["status"] == "REVIEW_REQUIRED"
    risk_payload = {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "finding_id": "finding-1",
        "reason": "Risk is explicitly bounded for this release.",
        "expires_at": "2026-10-17T01:00:00+08:00",
    }
    risk_event = user.sign("user.risk", "finding-1", risk_payload, at=NOW)
    assert runtime.accept_risk(baseline.goal_id, "finding-1", risk_event)["finding_id"] == "finding-1"
    second = _bundle(
        runtime,
        round_number=2,
        prior_rounds=[first],
        with_finding=True,
        dispositions={"finding-1": {"status": "RISK_ACCEPTED", "evidence_refs": []}},
    )
    second["bindings"] = [
        {**item, "session_id": item["session_id"] + "-round2"} for item in second["bindings"]
    ]
    second["delivery_receipts"] = [
        {**item, "session_id": item["session_id"] + "-round2"} for item in second["delivery_receipts"]
    ]
    risk_packets = build_packets(runtime.review_context("goal-1"), second, [first])
    second["delivery_receipts"] = [
        {**item, "input_digest": runtime.store.fingerprint(risk_packets[item["agent_id"]])}
        for item in second["delivery_receipts"]
    ]
    second_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, second, at=NOW)
    assert runtime.submit_review("goal-1", second, second_event)["status"] == "PASSED"


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


def test_strict_resume_transport_requires_new_binding_and_leaves_reconcile_state(tmp_path):
    runtime, baseline, user, observer, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    tested = {
        "contract": "goalanchor.idempotency-test.v1",
        "backend": "backend-1",
        "key": "key-1",
        "operation_digest": "pending",
        "duplicate_same_key_same_parameters": True,
        "reject_changed_parameters": True,
        "logical_effects": 1,
    }
    snapshot = EvidenceSnapshot.capture(
        "idem-1", "backend-1", json.dumps(tested).encode(), source="trusted-runner", collected_at=NOW
    )
    evidence_event = observer.sign(
        "observer.evidence", snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()}, at=NOW,
    )
    register_evidence(runtime, baseline, snapshot, evidence_event, provider)
    runtime.add_evidence(baseline.goal_id, snapshot, evidence_event)
    operation = Operation(
        "op-resume", baseline.goal_id, baseline.fingerprint, "write_text",
        canonical({"backend": "backend-1", "idempotency_key": "key-1"}),
        str(tmp_path), "item-1",
    )
    tested["operation_digest"] = operation.fingerprint
    # The evidence content is intentionally created before the operation digest is known;
    # the strict recovery path must reject this stale proof.
    runtime.register_operation(operation)
    bundle = _bundle(runtime)
    review_event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, bundle, at=NOW)
    runtime.submit_review("goal-1", bundle, review_event)
    question = runtime.request_authorization("op-resume")
    runtime.answer(
        question.question.question_id,
        user.sign("user.answer", question.question.fingerprint,
                  {"answer_id": "approve", "selected": ["approve"], "custom": None}, at=NOW),
    )
    receipt = runtime.execute("op-resume", "attempt-resume", lambda _: (_ for _ in ()).throw(RuntimeError("unknown")))
    assert receipt["state"] == "UNKNOWN"
    request = {
        "attempt_id": "attempt-resume", "operation_digest": operation.fingerprint,
        "recovery_id": "recovery-1", "backend": "backend-1", "key": "key-1",
    }
    proof_payload = {
        "attempt_id": "attempt-resume", "operation_digest": operation.fingerprint,
        "backend": "backend-1", "key": "key-1", "valid_until": "2026-10-17T01:00:00+08:00",
        "test_evidence_id": "idem-1",
    }
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.resume_transport(
            "attempt-resume",
            user.sign("user.resume", "attempt-resume", request, at=NOW),
            observer.sign("observer.idempotency", "attempt-resume", proof_payload, at=NOW),
            lambda _: {"exit_code": 0},
        )
    assert captured.value.code == "UNTRUSTED"


def test_strict_resume_transport_positive_path_is_bounded_until_reconcile(tmp_path):
    runtime, baseline, user, observer, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-resume-positive", baseline.goal_id, baseline.fingerprint, "write_text",
        canonical({"backend": "backend-1", "idempotency_key": "key-1"}),
        str(tmp_path), "item-1",
    )
    runtime.register_operation(operation)
    tested = {
        "contract": "goalanchor.idempotency-test.v1",
        "backend": "backend-1",
        "key": "key-1",
        "operation_digest": operation.fingerprint,
        "duplicate_same_key_same_parameters": True,
        "reject_changed_parameters": True,
        "logical_effects": 1,
    }
    snapshot = EvidenceSnapshot.capture(
        "idem-positive", "backend-1", json.dumps(tested).encode(), source="trusted-runner", collected_at=NOW
    )
    evidence_event = observer.sign(
        "observer.evidence", snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()}, at=NOW,
    )
    register_evidence(runtime, baseline, snapshot, evidence_event, provider)
    runtime.add_evidence(baseline.goal_id, snapshot, evidence_event)
    bundle = _bundle(runtime)
    runtime.submit_review(
        baseline.goal_id,
        bundle,
        observer.sign("observer.review", runtime.review_context(baseline.goal_id).fingerprint, bundle, at=NOW),
    )
    question = runtime.request_authorization(operation.operation_id)
    runtime.answer(
        question.question.question_id,
        user.sign("user.answer", question.question.fingerprint,
                  {"answer_id": "approve", "selected": ["approve"], "custom": None}, at=NOW),
    )
    receipt = runtime.execute(
        operation.operation_id, "attempt-resume-positive", lambda _: (_ for _ in ()).throw(RuntimeError("unknown"))
    )
    request = {
        "attempt_id": "attempt-resume-positive", "operation_digest": operation.fingerprint,
        "recovery_id": "recovery-positive", "backend": "backend-1", "key": "key-1",
    }
    proof_payload = {
        "attempt_id": "attempt-resume-positive", "operation_digest": operation.fingerprint,
        "backend": "backend-1", "key": "key-1", "valid_until": "2026-10-17T01:00:00+08:00",
        "test_evidence_id": "idem-positive",
    }
    recovered = runtime.resume_transport(
        "attempt-resume-positive",
        user.sign("user.resume", "attempt-resume-positive", request, at=NOW),
        observer.sign("observer.idempotency", "attempt-resume-positive", proof_payload, at=NOW),
        lambda _: {"exit_code": 0},
    )
    assert receipt["state"] == "UNKNOWN"
    assert recovered["state"] == "AWAITING_RECONCILIATION"


def test_strict_reuse_observation_requires_unchanged_criterion_and_signed_source(tmp_path):
    runtime, baseline, user, observer, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    write_evidence(runtime, baseline, observer, provider)
    operation = Operation(
        "op-reuse", baseline.goal_id, baseline.fingerprint, "write_text", "{}", str(tmp_path), "item-1"
    )
    runtime.register_operation(operation)
    bundle = _bundle(runtime)
    runtime.submit_review(
        baseline.goal_id,
        bundle,
        observer.sign("observer.review", runtime.review_context(baseline.goal_id).fingerprint, bundle, at=NOW),
    )
    question = runtime.request_authorization(operation.operation_id)
    runtime.answer(
        question.question.question_id,
        user.sign("user.answer", question.question.fingerprint,
                  {"answer_id": "approve", "selected": ["approve"], "custom": None}, at=NOW),
    )
    runtime.execute(operation.operation_id, "attempt-reuse", lambda _: {"exit_code": 0})
    old_observation_payload = {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "criterion_id": "artifact",
        "object_ref": "artifact.txt",
        "actual": "done",
        "attempt_id": "attempt-reuse",
    }
    runtime.observe(
        baseline.goal_id,
        observer.sign("observer.observation", baseline.fingerprint, old_observation_payload, at=NOW),
    )
    old_key = f"{baseline.fingerprint}:artifact"
    old_observation = runtime.store.get("strict_observation", old_key)
    replacement = replace(baseline, revision=2, plan="Write and verify the artifact again")
    replacement_event = baseline_event(runtime, replacement, event_id="replacement-baseline")
    from test_strict_baseline_evidence import register_baseline
    register_baseline(runtime, replacement, replacement_event, provider)
    runtime.start_baseline(replacement, replacement_event)
    reuse_payload = {
        "goal_id": replacement.goal_id,
        "baseline_digest": replacement.fingerprint,
        "source_baseline_digest": baseline.fingerprint,
        "criterion_id": "artifact",
        "object_ref": "artifact.txt",
        "actual": "done",
        "object_digest": old_observation["object_digest"],
        "no_impact_reason": "The criterion and artifact are unchanged.",
        "source_observation_fingerprint": runtime.store.fingerprint(old_observation),
    }
    reused = runtime.reuse_observation(
        replacement.goal_id,
        observer.sign("observer.reuse_observation", replacement.fingerprint, reuse_payload, at=NOW),
    )
    assert reused["observation_kind"] == "REUSED"
    assert reused["status"] == "PASS"


def test_strict_review_concurrent_same_round_has_one_cas_winner(tmp_path):
    runtime, observer = _ready_runtime(tmp_path)
    bundle = _bundle(runtime)
    context_fingerprint = runtime.review_context("goal-1").fingerprint
    events = [
        observer.sign("observer.review", context_fingerprint, bundle, event_id=f"review-{index}", at=NOW)
        for index in (1, 2)
    ]

    def submit(event):
        try:
            return ("ok", runtime.submit_review("goal-1", bundle, event)["status"])
        except StrictWorkflowError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(submit, event) for event in events)]
    assert sum(result[0] == "ok" for result in results) == 1
    assert sum(result[0] == "error" for result in results) == 1
    assert any(result[1] in {"STALE", "CONFLICT", "UNTRUSTED"} for result in results)
    assert len(runtime.store.list("strict_review_chain", "goal-1")) == 1
