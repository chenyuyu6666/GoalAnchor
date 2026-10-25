"""Offline consumer regressions; native host or account trust is not claimed."""
from __future__ import annotations

import json
import sqlite3
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
from test_strict_review_chain import _bundle
from test_strict_review_proofs import (
    FixtureReviewVerifier,
    make_review_proof,
    ready_proved_runtime,
    second_round,
    with_verifier,
)

from goalanchor.review_delivery import build_packets
from goalanchor.workflow import StrictWorkflowError
from goalanchor.workflow_contracts import TrustedProofRejected, TrustedProofUnavailable
from goalanchor.workflow_protocol import EvidenceSnapshot, Operation, canonical, fingerprint

EXPIRED = "2026-10-16T01:06:00+08:00"
FAULTS = (
    "expired", "rejected", "unavailable", "provider_disabled",
    "tampered_head", "tampered_record", "tampered_chain",
)


def _approve(runtime, user, *, answer_id="approve-1"):
    question = runtime.request_authorization("op-1")
    event = user.sign(
        "user.answer", question.question.fingerprint,
        {"answer_id": answer_id, "selected": ["approve"], "custom": None}, at=runtime.clock(),
    )
    runtime.answer(question.question.question_id, event)
    return question, event


def _scope(runtime, baseline, user):
    event = _scope_event(runtime, baseline, user)
    return runtime.authorize_scope(baseline.goal_id, "scope-proof-test", event)


def _scope_event(runtime, baseline, user):
    payload = {
        "goal_id": baseline.goal_id, "baseline_digest": baseline.fingerprint,
        "scope_id": "scope-proof-test", "allowed_tools": ["write_text"],
        "workspace": runtime.policy.workspace, "expires_at": "2026-10-16T02:00:00+08:00",
    }
    return user.sign("user.scope", baseline.goal_id, payload, at=runtime.clock())


@pytest.mark.parametrize("scope", [False, True])
def test_mutable_grant_cannot_erase_consumption_or_revocation(tmp_path, scope):
    runtime, baseline, user, _, _ = ready_proved_runtime(tmp_path)
    calls = []
    if scope:
        grant = _scope(runtime, baseline, user)
        event = user.sign("user.revoke", grant["authorization_id"],
                          {"grant_id": grant["authorization_id"]}, at=runtime.clock())
        runtime.revoke_authorization(grant["authorization_id"], event)
    else:
        _approve(runtime, user)
        assert runtime.execute("op-1", "first", lambda op: calls.append(op) or {})["state"] == "SUCCEEDED"
        grant = runtime.store.list("strict_grant", "goal-1")[0]
    grant["state"] = "ACTIVE"
    for field in ("attempt_id", "consumed_at", "revoked_at", "revoke_event_id"):
        grant.pop(field, None)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE records SET payload=? WHERE kind=? AND key=?",
                   (canonical(grant), "strict_grant", grant["authorization_id"]))
    with pytest.raises(StrictWorkflowError):
        runtime.execute("op-1", "second", lambda op: calls.append(op) or {})
    assert len(calls) == (0 if scope else 1)


def test_new_review_at_dispatch_does_not_authorize_old_grant(tmp_path):
    runtime, _, user, _, provider = ready_proved_runtime(tmp_path)
    _approve(runtime, user)
    verifies = 0
    calls = []

    def replace_review():
        nonlocal verifies
        verifies += 1
        if verifies == 3:
            provider.on_verify = None
            bundle, event = second_round(runtime, provider)
            runtime.submit_review("goal-1", bundle, event)

    provider.on_verify = replace_review
    receipt = runtime.execute("op-1", "race", lambda op: calls.append(op) or {})
    assert receipt["state"] == "UNKNOWN"
    assert calls == []


def test_cancel_without_inflight_receipt_blocks_future_dispatch(tmp_path):
    runtime, _, user, _, _ = ready_proved_runtime(tmp_path)
    _approve(runtime, user)
    event = user.sign("user.cancel", "goal-1", {"goal_id": "goal-1", "reason": "stop"}, at=runtime.clock())
    runtime.cancel("goal-1", event)
    assert runtime.store.get_event(event.event_id) == event.to_dict()
    assert runtime.cancel("goal-1", event)["state"] == "CANCELLED"
    calls = []
    with pytest.raises(StrictWorkflowError):
        runtime.execute("op-1", "cancelled", lambda op: calls.append(op) or {})
    with pytest.raises(StrictWorkflowError):
        runtime.request_authorization("op-1")
    assert calls == []


def test_cancel_history_storage_failure_is_a_public_unavailable_error(tmp_path, monkeypatch):
    runtime, *_ = ready_proved_runtime(tmp_path)

    def unavailable(**_kwargs):
        raise sqlite3.OperationalError("fixture unavailable")

    monkeypatch.setattr(runtime.store, "read_events", unavailable)
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_authorization("op-1")
    assert captured.value.code == "UNAVAILABLE"


def test_scope_expiry_bounds_final_dispatch_after_clock_advances(tmp_path, monkeypatch):
    runtime, baseline, user, _, provider = ready_proved_runtime(tmp_path)
    payload = {
        "goal_id": baseline.goal_id, "baseline_digest": baseline.fingerprint,
        "scope_id": "scope-short", "allowed_tools": ["write_text"],
        "workspace": runtime.policy.workspace, "expires_at": "2026-10-16T01:01:00+08:00",
    }
    event = user.sign("user.scope", baseline.goal_id, payload, at=runtime.clock())
    runtime.authorize_scope(baseline.goal_id, "scope-short", event)
    original_dispatch = runtime.store.dispatch_review_bound

    def advance_before_dispatch(*args, **kwargs):
        runtime.clock = lambda: "2026-10-16T01:02:00+08:00"
        return original_dispatch(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "dispatch_review_bound", advance_before_dispatch)
    dispatched = []
    receipt = runtime.execute("op-1", "expired-scope", lambda op: dispatched.append(op) or {})
    assert receipt["state"] == "UNKNOWN"
    assert dispatched == []


def test_scope_expiry_during_final_cas_stops_before_executor(tmp_path, monkeypatch):
    runtime, baseline, user, _, _ = ready_proved_runtime(tmp_path)
    payload = {
        "goal_id": baseline.goal_id, "baseline_digest": baseline.fingerprint,
        "scope_id": "scope-cas-short", "allowed_tools": ["write_text"],
        "workspace": runtime.policy.workspace, "expires_at": "2026-10-16T01:01:00+08:00",
    }
    event = user.sign("user.scope", baseline.goal_id, payload, at=runtime.clock())
    runtime.authorize_scope(baseline.goal_id, "scope-cas-short", event)
    original_check = runtime.store._strict_check
    checks = 0

    def advance_after_cas(db, expected):
        nonlocal checks
        original_check(db, expected)
        checks += 1
        if checks == 3:
            runtime.clock = lambda: "2026-10-16T01:02:00+08:00"

    monkeypatch.setattr(runtime.store, "_strict_check", advance_after_cas)
    dispatched = []
    receipt = runtime.execute("op-1", "expired-during-cas", lambda op: dispatched.append(op) or {})
    assert checks == 3
    assert receipt["state"] == "UNKNOWN"
    assert dispatched == []


def test_removed_event_immutability_trigger_blocks_review_use(tmp_path):
    runtime, *_ = ready_proved_runtime(tmp_path)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DROP TRIGGER events_no_delete")
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_authorization("op-1")
    assert captured.value.code == "UNTRUSTED"


def _invalidate(runtime, verifier, fault):
    if fault == "expired":
        runtime.clock = lambda: EXPIRED
    elif fault == "rejected":
        verifier.failure = TrustedProofRejected("fixture issuer rejection")
    elif fault == "unavailable":
        verifier.failure = TrustedProofUnavailable("fixture issuer unavailable")
    elif fault == "provider_disabled":
        return with_verifier(runtime, None)
    else:
        kind, key = {
            "tampered_head": ("strict_review_head", "goal-1"),
            "tampered_record": ("strict_review_record", "goal-1"),
            "tampered_chain": ("strict_review_chain", "goal-1:1"),
        }[fault]
        value = deepcopy(runtime.store.get(kind, key))
        if fault == "tampered_record":
            value["unresolved"] = ["injected-unresolved-finding"]
        elif fault == "tampered_chain":
            value["status"] = "REVIEW_REQUIRED" if value["status"] == "PASSED" else "PASSED"
        else:
            value["source_event_id"] = "unrelated-review-event"
        # Only mutable projections are corrupted; append-only attestation stays intact.
        with sqlite3.connect(runtime.store.path) as db:
            db.execute("UPDATE records SET payload=? WHERE kind=? AND key=?", (canonical(value), kind, key))
    return runtime


def _permission_state(runtime):
    return {
        "audit": runtime.store.audit(),
        **{kind: runtime.store.list(kind) for kind in (
            "strict_auth_question", "strict_grant", "strict_receipt", "strict_recovery", "strict_closure",
            "strict_risk",
        )},
    }


def _prepare_consumer(runtime, baseline, user, observer, consumer):
    question = None
    if consumer == "answer":
        question = runtime.request_authorization("op-1")
    elif consumer in {"execute", "close"}:
        _approve(runtime, user)
    if consumer == "close":
        runtime.execute("op-1", "attempt-complete", lambda _: {"exit_code": 0})
        observation = {
            "goal_id": baseline.goal_id, "baseline_digest": baseline.fingerprint,
            "criterion_id": "artifact", "object_ref": "artifact.txt", "actual": "done",
            "attempt_id": "attempt-complete",
        }
        runtime.observe(baseline.goal_id, observer.sign(
            "observer.observation", baseline.fingerprint, observation, at=runtime.clock(),
        ))
    return question


def _consume(runtime, baseline, user, consumer, question, calls):
    if consumer == "request_authorization":
        return runtime.request_authorization("op-1")
    if consumer == "answer":
        return runtime.answer(question.question.question_id, user.sign(
            "user.answer", question.question.fingerprint,
            {"answer_id": "approve-after-fault", "selected": ["approve"], "custom": None}, at=runtime.clock(),
        ))
    if consumer == "authorize_scope":
        return _scope(runtime, baseline, user)
    if consumer == "execute":
        return runtime.execute("op-1", "attempt-after-fault", lambda op: calls.append(op) or {"exit_code": 0})
    return runtime.close(baseline.goal_id)


@pytest.mark.parametrize("consumer", ["request_authorization", "answer", "authorize_scope", "execute", "close"])
@pytest.mark.parametrize("fault", FAULTS)
def test_invalid_review_proof_cannot_create_permission_effect_or_closure(tmp_path, consumer, fault):
    runtime, baseline, user, observer, verifier = ready_proved_runtime(tmp_path)
    question = _prepare_consumer(runtime, baseline, user, observer, consumer)
    runtime = _invalidate(runtime, verifier, fault)
    before = _permission_state(runtime)
    calls = []
    with pytest.raises(StrictWorkflowError) as captured:
        _consume(runtime, baseline, user, consumer, question, calls)
    expected = {
        "expired": "STALE", "rejected": "UNTRUSTED", "unavailable": "UNAVAILABLE",
        "provider_disabled": "UNAVAILABLE", "tampered_head": "UNTRUSTED",
        "tampered_record": "UNTRUSTED", "tampered_chain": "UNTRUSTED",
    }
    assert captured.value.code == expected[fault]
    assert calls == []
    assert _permission_state(runtime) == before


def test_current_proof_supports_authorization_execution_and_closure(tmp_path):
    runtime, baseline, user, observer, _ = ready_proved_runtime(tmp_path)
    _prepare_consumer(runtime, baseline, user, observer, "close")
    assert runtime.store.get("strict_receipt", "attempt-complete")["state"] == "SUCCEEDED"
    assert runtime.close(baseline.goal_id)["state"] == "CLOSED"


def test_current_proof_supports_scope_and_single_consumption(tmp_path):
    runtime, baseline, user, _, _ = ready_proved_runtime(tmp_path)
    assert _scope(runtime, baseline, user)["state"] == "ACTIVE"
    calls = []
    receipt = runtime.execute("op-1", "scope-attempt", lambda op: calls.append(op) or {"exit_code": 0})
    assert receipt["state"] == "SUCCEEDED"
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["expired", "rejected", "unavailable", "provider_disabled"])
def test_review_proof_failure_does_not_prevent_denial(tmp_path, fault):
    runtime, _, user, _, verifier = ready_proved_runtime(tmp_path)
    question = runtime.request_authorization("op-1")
    runtime = _invalidate(runtime, verifier, fault)
    denied = runtime.answer(question.question.question_id, user.sign(
        "user.answer", question.question.fingerprint,
        {"answer_id": "deny-after-fault", "selected": ["deny"], "custom": None}, at=runtime.clock(),
    ))
    assert denied.decision == "DENY"
    assert runtime.store.list("strict_grant") == ()


@pytest.mark.parametrize("fault", ["expired", "rejected", "unavailable", "provider_disabled"])
def test_review_proof_failure_does_not_prevent_revoke_and_cancel(tmp_path, fault):
    runtime, baseline, user, _, verifier = ready_proved_runtime(tmp_path)
    _, approved_event = _approve(runtime, user)
    grant_id = "grant-" + approved_event.event_id
    runtime = _invalidate(runtime, verifier, fault)
    revoked = runtime.revoke_authorization(grant_id, user.sign(
        "user.revoke", grant_id, {"grant_id": grant_id}, at=runtime.clock(),
    ))
    assert revoked["state"] == "REVOKED"
    cancelled = runtime.cancel(baseline.goal_id, user.sign(
        "user.cancel", baseline.goal_id, {"goal_id": baseline.goal_id, "reason": "stop test"}, at=runtime.clock(),
    ))
    assert cancelled["state"] == "CANCELLED"


def _ready_resume_runtime(tmp_path):
    base, baseline, user, observer, evidence_provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    verifier = FixtureReviewVerifier()
    runtime = with_verifier(base, verifier)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), evidence_provider)
    operation = Operation(
        "op-1", baseline.goal_id, baseline.fingerprint, "write_text",
        canonical({"backend": "backend-1", "idempotency_key": "key-1"}), str(tmp_path), "item-1",
    )
    runtime.register_operation(operation)
    tested = {
        "contract": "goalanchor.idempotency-test.v1", "backend": "backend-1", "key": "key-1",
        "operation_digest": operation.fingerprint, "duplicate_same_key_same_parameters": True,
        "reject_changed_parameters": True, "logical_effects": 1,
    }
    snapshot = EvidenceSnapshot.capture(
        "idem-proof", "backend-1", json.dumps(tested).encode(), source="trusted-runner", collected_at=NOW,
    )
    event = observer.sign("observer.evidence", snapshot.fingerprint,
                          {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()}, at=NOW)
    register_evidence(runtime, baseline, snapshot, event, evidence_provider)
    runtime.add_evidence(baseline.goal_id, snapshot, event)
    bundle = _bundle(runtime)
    bundle["skill_digest"] = "a" * 64
    packets = build_packets(runtime.review_context(baseline.goal_id), bundle, [])
    for receipt in bundle["delivery_receipts"]:
        receipt["input_digest"] = fingerprint(packets[receipt["agent_id"]])
    verifier.proofs[fingerprint(bundle)] = make_review_proof(runtime, bundle)
    runtime.submit_review(baseline.goal_id, bundle, observer.sign(
        "observer.review", runtime.review_context(baseline.goal_id).fingerprint, bundle, at=NOW,
    ))
    _approve(runtime, user)

    def unknown(_operation):
        raise RuntimeError("fixture unknown transport outcome")

    assert runtime.execute("op-1", "attempt-unknown", unknown)["state"] == "UNKNOWN"
    return runtime, baseline, user, observer, verifier, operation


def _resume_events(runtime, user, observer, operation):
    request = {
        "attempt_id": "attempt-unknown", "operation_digest": operation.fingerprint,
        "recovery_id": "recovery-proof", "backend": "backend-1", "key": "key-1",
    }
    proof = {
        "attempt_id": "attempt-unknown", "operation_digest": operation.fingerprint,
        "backend": "backend-1", "key": "key-1", "valid_until": "2026-10-17T01:00:00+08:00",
        "test_evidence_id": "idem-proof",
    }
    return (
        user.sign("user.resume", "attempt-unknown", request, at=runtime.clock()),
        observer.sign("observer.idempotency", "attempt-unknown", proof, at=runtime.clock()),
    )


@pytest.mark.parametrize("fault", FAULTS)
def test_invalid_review_proof_prevents_resume_transport_without_dispatch(tmp_path, fault):
    runtime, _, user, observer, verifier, operation = _ready_resume_runtime(tmp_path)
    runtime = _invalidate(runtime, verifier, fault)
    request, proof = _resume_events(runtime, user, observer, operation)
    before = _permission_state(runtime)
    calls = []
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.resume_transport("attempt-unknown", request, proof,
                                 lambda op: calls.append(op) or {"exit_code": 0})
    expected = {"expired": "STALE", "unavailable": "UNAVAILABLE", "provider_disabled": "UNAVAILABLE"}
    assert captured.value.code == expected.get(fault, "UNTRUSTED")
    assert calls == []
    assert _permission_state(runtime) == before


def test_current_native_review_proof_allows_resume_only_until_reconciliation(tmp_path):
    runtime, _, user, observer, _, operation = _ready_resume_runtime(tmp_path)
    request, proof = _resume_events(runtime, user, observer, operation)
    calls = []
    recovery = runtime.resume_transport("attempt-unknown", request, proof,
                                        lambda op: calls.append(op) or {"exit_code": 0})
    assert recovery["state"] == "AWAITING_RECONCILIATION"
    assert runtime.store.get("strict_receipt", "attempt-unknown")["state"] == "UNKNOWN"
    assert len(calls) == 1


def _ready_risk_runtime(tmp_path):
    base, baseline, user, observer, evidence_provider, _ = make_world(tmp_path)
    base.policy = replace(base.policy, acceptable_risks=("finding-1",))
    base.runtime_binding_provider.policy = base.policy
    verifier = FixtureReviewVerifier()
    runtime = with_verifier(base, verifier)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), evidence_provider)
    write_evidence(runtime, baseline, observer, evidence_provider)
    bundle = _bundle(runtime, with_finding=True)
    bundle["skill_digest"] = "a" * 64
    packets = build_packets(runtime.review_context(baseline.goal_id), bundle, [])
    for receipt in bundle["delivery_receipts"]:
        receipt["input_digest"] = fingerprint(packets[receipt["agent_id"]])
    verifier.proofs[fingerprint(bundle)] = make_review_proof(runtime, bundle)
    result = runtime.submit_review(baseline.goal_id, bundle, observer.sign(
        "observer.review", runtime.review_context(baseline.goal_id).fingerprint, bundle, at=NOW,
    ))
    assert result["status"] == "REVIEW_REQUIRED"
    return runtime, baseline, user, verifier


def _accept_known_risk(runtime, baseline, user):
    payload = {
        "goal_id": baseline.goal_id, "baseline_digest": baseline.fingerprint, "finding_id": "finding-1",
        "reason": "Explicit fixture policy permits this bounded risk", "expires_at": "2026-10-16T02:00:00+08:00",
    }
    return runtime.accept_risk(baseline.goal_id, "finding-1", user.sign(
        "user.risk", "finding-1", payload, at=runtime.clock(),
    ))


def test_risk_disposition_needs_authentic_review_but_does_not_require_passed_status(tmp_path):
    runtime, baseline, user, _ = _ready_risk_runtime(tmp_path)
    assert _accept_known_risk(runtime, baseline, user)["finding_id"] == "finding-1"
    assert runtime.store.get("strict_review_record", baseline.goal_id)["status"] == "REVIEW_REQUIRED"
    assert runtime.store.list("strict_grant") == ()


@pytest.mark.parametrize("fault", FAULTS)
def test_risk_disposition_cannot_use_an_invalid_review_proof(tmp_path, fault):
    runtime, baseline, user, verifier = _ready_risk_runtime(tmp_path)
    runtime = _invalidate(runtime, verifier, fault)
    before = _permission_state(runtime)
    with pytest.raises(StrictWorkflowError) as captured:
        _accept_known_risk(runtime, baseline, user)
    expected = {"expired": "STALE", "unavailable": "UNAVAILABLE", "provider_disabled": "UNAVAILABLE"}
    assert captured.value.code == expected.get(fault, "UNTRUSTED")
    assert _permission_state(runtime) == before


@pytest.mark.parametrize("consumer", [
    "request_authorization", "answer", "authorize_scope", "execute", "close", "resume_transport",
])
@pytest.mark.parametrize("fault", ["expired", "rejected", "provider_disabled", "tampered_record"])
def test_consumer_idempotent_return_cannot_bypass_current_proof(tmp_path, consumer, fault):
    calls = []
    if consumer == "resume_transport":
        runtime, baseline, user, observer, verifier, operation = _ready_resume_runtime(tmp_path)
        request, proof = _resume_events(runtime, user, observer, operation)
        runtime.resume_transport("attempt-unknown", request, proof, lambda _: {"exit_code": 0})

        def replay():
            return runtime.resume_transport("attempt-unknown", request, proof,
                                            lambda op: calls.append(op) or {"exit_code": 0})
    else:
        runtime, baseline, user, observer, verifier = ready_proved_runtime(tmp_path)
        if consumer == "request_authorization":
            runtime.request_authorization("op-1")

            def replay():
                return runtime.request_authorization("op-1")
        elif consumer == "answer":
            question, answer_event = _approve(runtime, user)

            def replay():
                return runtime.answer(question.question.question_id, answer_event)
        elif consumer == "authorize_scope":
            scope_event = _scope_event(runtime, baseline, user)
            runtime.authorize_scope(baseline.goal_id, "scope-proof-test", scope_event)

            def replay():
                return runtime.authorize_scope(baseline.goal_id, "scope-proof-test", scope_event)
        elif consumer == "execute":
            _approve(runtime, user)
            runtime.execute("op-1", "completed-attempt", lambda _: {"exit_code": 0})

            def replay():
                return runtime.execute("op-1", "completed-attempt", lambda op: calls.append(op) or {"exit_code": 0})
        else:
            _prepare_consumer(runtime, baseline, user, observer, "close")
            runtime.close(baseline.goal_id)

            def replay():
                return runtime.close(baseline.goal_id)
    runtime = _invalidate(runtime, verifier, fault)
    before = _permission_state(runtime)
    with pytest.raises(StrictWorkflowError) as captured:
        replay()
    expected = {"expired": "STALE", "provider_disabled": "UNAVAILABLE"}
    assert captured.value.code == expected.get(fault, "UNTRUSTED")
    assert calls == []
    assert _permission_state(runtime) == before


@pytest.mark.parametrize("fault", ["expired", "rejected", "unavailable", "provider_disabled"])
def test_interrupted_running_receipt_can_be_cancelled_when_review_proof_fails(tmp_path, fault):
    runtime, baseline, user, _, verifier = ready_proved_runtime(tmp_path)
    _approve(runtime, user)

    class SimulatedProcessDeath(BaseException):
        pass

    def interrupted(_operation):
        raise SimulatedProcessDeath

    with pytest.raises(SimulatedProcessDeath):
        runtime.execute("op-1", "interrupted-attempt", interrupted)
    assert runtime.store.get("strict_receipt", "interrupted-attempt")["state"] == "RUNNING"
    runtime = _invalidate(runtime, verifier, fault)
    result = runtime.cancel(baseline.goal_id, user.sign(
        "user.cancel", baseline.goal_id, {"goal_id": baseline.goal_id, "reason": "cancel interrupted test"},
        at=runtime.clock(),
    ))
    assert result["state"] == "CANCELLED"
    receipt = runtime.store.get("strict_receipt", "interrupted-attempt")
    assert receipt["state"] == "CANCELLED"
    assert receipt["dispatch_right"] is False
