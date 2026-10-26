from __future__ import annotations

import sqlite3
from copy import deepcopy

import pytest
from test_strict_baseline_evidence import NOW
from test_strict_review_chain import _bundle, _ready_operation_runtime, _ready_runtime

from goalanchor import StrictWorkflow, StrictWorkflowError
from goalanchor.review_delivery import build_packets
from goalanchor.workflow_protocol import SignedEvent, canonical, fingerprint, strict_loads


class FixtureReviewVerifier:
    """Controlled offline issuer fixture; never evidence of native host trust."""
    def __init__(self):
        self.proofs = {}
        self.failure = None
        self.on_verify = None

    def verify(self, review, **kwargs):
        if self.failure:
            raise self.failure
        if self.on_verify:
            self.on_verify()
        return deepcopy(self.proofs.get(fingerprint(review)))


def make_review_proof(runtime, bundle, *, prior=(), expires="2026-10-16T01:05:00+08:00"):
    context = runtime.review_context("goal-1")
    host = strict_loads(runtime.policy.host_binding)
    for binding in bundle["bindings"]:
        binding["model"] = host["model"]
    packets = build_packets(context, bundle, list(prior))
    reports = {x["reviewer_id"]: x for x in bundle["reviewer_results"]}
    reports[bundle["arbiter"]["arbiter_id"]] = bundle["arbiter"]
    reports[bundle["meta_review"]["reviewer_id"]] = bundle["meta_review"]
    receipts = {x["agent_id"]: x for x in bundle["delivery_receipts"]}
    roles = []
    for binding in bundle["bindings"]:
        agent_id = binding["agent_id"]
        identity = {**binding, "input_id": "input-" + binding["session_id"],
                    "turn_id": "turn-" + binding["session_id"]}
        payload = {**identity, "packet": packets[agent_id], "response": canonical(reports[agent_id])}
        source = runtime.observer_authority.sign(
            "host.review.completed", context.fingerprint, payload,
            event_id=receipts[agent_id]["transport_receipt_id"], at=NOW,
        ).to_dict()
        roles.append({**identity, "input_digest": fingerprint(packets[agent_id]),
                      "result_digest": fingerprint(reports[agent_id]), "source_event_id": source["event_id"],
                      "source_event_digest": fingerprint(source), "source_event": source})
    proof = {
        "schema_version": "goalanchor.review-proof/v1", "review_digest": fingerprint(bundle),
        "context_fingerprint": context.fingerprint, "policy_digest": runtime.policy.fingerprint,
        "host_binding": runtime.policy.host_binding,
        "runtime_binding_digest": context.strict_binding["runtime_binding_digest"],
        "round_number": bundle["round_number"], "skill_digest": bundle["skill_digest"],
        "issued_at": NOW, "expires_at": expires, "roles": roles,
    }
    proof["proof_digest"] = fingerprint(proof)
    return proof


def ready_proved_runtime(tmp_path):
    base, baseline, user, observer = _ready_operation_runtime(tmp_path)
    provider = FixtureReviewVerifier()
    runtime = with_verifier(base, provider)
    bundle = _bundle(runtime)
    bundle["skill_digest"] = "a" * 64  # Explicit fixture, not an installed Skill claim.
    packets = build_packets(runtime.review_context("goal-1"), bundle, [])
    for receipt in bundle["delivery_receipts"]:
        receipt["input_digest"] = fingerprint(packets[receipt["agent_id"]])
    proof = make_review_proof(runtime, bundle)
    provider.proofs[fingerprint(bundle)] = proof
    event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, bundle, at=NOW)
    runtime.submit_review("goal-1", bundle, event)
    return runtime, baseline, user, observer, provider


def test_native_proof_and_signed_attestation_survive_reopen(tmp_path):
    runtime, _, _, _, provider = ready_proved_runtime(tmp_path)
    proof = next(iter(provider.proofs.values()))
    record = runtime.store.get("strict_review_record", "goal-1")
    attestation = runtime.store.get_review_attestation("goal-1", 1)
    assert record["proof_digest"] == proof["proof_digest"]
    assert attestation["companion_event"]["payload"]["proof"] == proof
    reopened = with_verifier(runtime, provider)
    assert reopened._current_review("goal-1", reopened.review_context("goal-1")) == record
    assert reopened.request_authorization("op-1").state == "WAITING"


def test_disabling_provider_cannot_downgrade_native_review(tmp_path):
    runtime, _, _, _, _ = ready_proved_runtime(tmp_path)
    offline = with_verifier(runtime, None)
    with pytest.raises(StrictWorkflowError) as captured:
        offline.request_authorization("op-1")
    assert captured.value.code == "UNAVAILABLE"


def test_replay_rechecks_expiry_even_when_provider_offers_extended_deadline(tmp_path):
    runtime, _, _, _, provider = ready_proved_runtime(tmp_path)
    record = runtime.store.get("strict_review_record", "goal-1")
    bundle = record["history"][0]
    event = SignedEvent.from_dict(runtime.store.get_event(record["source_event_id"]))
    runtime.clock = lambda: "2026-10-16T01:06:00+08:00"
    proof = provider.proofs[fingerprint(bundle)]
    proof["expires_at"] = "2026-10-16T01:30:00+08:00"
    proof["proof_digest"] = fingerprint({k: v for k, v in proof.items() if k != "proof_digest"})
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.submit_review("goal-1", bundle, event)
    assert captured.value.code == "STALE"


def test_same_event_id_cannot_bypass_signature_verification(tmp_path):
    runtime, _, _, _, _ = ready_proved_runtime(tmp_path)
    record = runtime.store.get("strict_review_record", "goal-1")
    raw = runtime.store.get_event(record["source_event_id"])
    forged = SignedEvent.from_dict({**raw, "signature": "0" * 64})
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.submit_review("goal-1", record["history"][0], forged)
    assert captured.value.code == "UNTRUSTED"


def test_stripping_all_projection_proof_fields_does_not_enable_offline_downgrade(tmp_path):
    runtime, _, _, _, _ = ready_proved_runtime(tmp_path)
    for kind, key in (("strict_review_record", "goal-1"), ("strict_review_head", "goal-1"),
                      ("strict_review_chain", "goal-1:1")):
        value = runtime.store.get(kind, key)
        value.pop("proof_digest")
        with sqlite3.connect(runtime.store.path) as db:
            db.execute("UPDATE records SET payload=? WHERE kind=? AND key=?", (canonical(value), kind, key))
    offline = with_verifier(runtime, None)
    with pytest.raises(StrictWorkflowError) as captured:
        offline.request_authorization("op-1")
    assert captured.value.code == "UNAVAILABLE"


def test_enabling_provider_does_not_promote_old_offline_review(tmp_path):
    base, _, _, observer = _ready_operation_runtime(tmp_path)
    bundle = _bundle(base)
    event = observer.sign("observer.review", base.review_context("goal-1").fingerprint, bundle, at=NOW)
    base.submit_review("goal-1", bundle, event)
    runtime = with_verifier(base, FixtureReviewVerifier())
    before = runtime.store.audit()
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_authorization("op-1")
    assert captured.value.code == "UNTRUSTED"
    assert runtime.store.get_review_attestation("goal-1", 1) is None
    assert runtime.store.audit() == before


def test_native_grant_review_binding_cannot_be_changed_in_projection(tmp_path):
    runtime, _, user, _, _ = ready_proved_runtime(tmp_path)
    question = runtime.request_authorization("op-1").question
    answer = user.sign("user.answer", question.fingerprint,
                       {"answer_id": "yes", "selected": ["approve"], "custom": None}, at=NOW)
    runtime.answer(question.question_id, answer)
    grant = runtime.store.list("strict_grant", "goal-1")[0]
    grant["expires_at"] = "2026-10-16T02:00:00+08:00"
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE records SET payload=? WHERE kind='strict_grant' AND key=?",
                   (canonical(grant), grant["authorization_id"]))
    called = []
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.execute("op-1", "tampered-grant", lambda op: called.append(op) or {})
    assert captured.value.code == "UNTRUSTED"
    assert called == []


def test_proof_expiry_at_commit_boundary_creates_no_question(tmp_path, monkeypatch):
    runtime, _, _, _, _ = ready_proved_runtime(tmp_path)
    original = runtime.store.commit_review_bound
    def expire(*args, **kwargs):
        runtime.clock = lambda: "2026-10-16T01:06:00+08:00"
        return original(*args, **{**kwargs, "clock": runtime.clock})
    monkeypatch.setattr(runtime.store, "commit_review_bound", expire)
    before = runtime.store.audit()
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_authorization("op-1")
    assert captured.value.code == "STALE"
    assert runtime.store.list("strict_auth_question", "goal-1") == ()
    assert runtime.store.audit() == before


def second_round(runtime, provider, *, reuse_source=False):
    previous = runtime.store.get("strict_review_record", "goal-1")["history"]
    bundle = _bundle(runtime, round_number=2, prior_rounds=previous)
    bundle["skill_digest"] = "a" * 64
    for binding in bundle["bindings"]:
        binding["session_id"] += "-round2"
    packets = build_packets(runtime.review_context("goal-1"), bundle, previous)
    for receipt in bundle["delivery_receipts"]:
        receipt["session_id"] += "-round2"
        receipt["input_digest"] = fingerprint(packets[receipt["agent_id"]])
        if not reuse_source:
            receipt["transport_receipt_id"] += "-round2"
    proof = make_review_proof(runtime, bundle, prior=previous)
    provider.proofs[fingerprint(bundle)] = proof
    event = runtime.observer_authority.sign("observer.review", runtime.review_context("goal-1").fingerprint,
                                            bundle, at=NOW)
    return bundle, event


def test_two_rounds_cannot_reuse_a_host_source_event_id(tmp_path):
    runtime, _, _, _, provider = ready_proved_runtime(tmp_path)
    bundle, event = second_round(runtime, provider, reuse_source=True)
    before = runtime.store.audit()
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.submit_review("goal-1", bundle, event)
    assert captured.value.code == "CONFLICT"
    assert runtime.store.audit() == before


def test_new_review_round_has_new_question_and_cannot_relabel_old_approval(tmp_path):
    runtime, _, user, _, provider = ready_proved_runtime(tmp_path)
    old_question = runtime.request_authorization("op-1").question
    bundle, event = second_round(runtime, provider)
    runtime.submit_review("goal-1", bundle, event)
    new_question = runtime.request_authorization("op-1").question
    assert new_question.question_id != old_question.question_id
    old = runtime.store.get("strict_auth_question", old_question.question_id)
    old["proof_digest"] = runtime.store.get("strict_review_record", "goal-1")["proof_digest"]
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE records SET payload=? WHERE kind='strict_auth_question' AND key=?",
                   (canonical(old), old_question.question_id))
    approval = user.sign("user.answer", old_question.fingerprint,
                         {"answer_id": "old-approval", "selected": ["approve"], "custom": None}, at=NOW)
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.answer(old_question.question_id, approval)
    assert captured.value.code == "UNTRUSTED"
    assert runtime.store.list("strict_grant", "goal-1") == ()



class EmptyReviewVerifier:
    def verify(self, review, **kwargs):
        return {}


def with_verifier(runtime, verifier):
    return StrictWorkflow(
        runtime.store, runtime.user_authority, runtime.observer_authority, runtime.policy,
        trusted_event_verifier=runtime.trusted_event_verifier,
        trusted_review_verifier=verifier,
        trusted_runtime_binding_provider=runtime.runtime_binding_provider,
        clock=runtime.clock,
    )


def test_empty_trusted_review_proof_is_rejected_without_writes(tmp_path):
    base, observer = _ready_runtime(tmp_path)
    runtime = with_verifier(base, EmptyReviewVerifier())
    bundle = _bundle(runtime)
    event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, bundle, at=NOW)
    before = deepcopy(runtime.store.audit())
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.submit_review("goal-1", bundle, event)
    assert captured.value.code == "UNTRUSTED"
    assert runtime.store.get("strict_review_record", "goal-1") is None
    assert runtime.store.audit() == before


def test_offline_projection_cannot_promote_unresolved_review(tmp_path):
    runtime, _, _, observer = _ready_operation_runtime(tmp_path)
    bundle = _bundle(runtime, with_finding=True)
    event = observer.sign("observer.review", runtime.review_context("goal-1").fingerprint, bundle, at=NOW)
    assert runtime.submit_review("goal-1", bundle, event)["status"] == "REVIEW_REQUIRED"
    rows = [(kind, key, runtime.store.get(kind, key)) for kind, key in
            (("strict_review_record", "goal-1"), ("strict_review_head", "goal-1"),
             ("strict_review_chain", "goal-1:1"))]
    with sqlite3.connect(runtime.store.path) as db:
        for kind, key, value in rows:
            value["status"] = "PASSED"
            if kind == "strict_review_record":
                value["unresolved"] = []
            db.execute("UPDATE records SET payload=? WHERE kind=? AND key=?", (canonical(value), kind, key))
    with pytest.raises(StrictWorkflowError, match="signed"):
        runtime.request_authorization("op-1")


def test_provider_callback_cannot_replace_validated_cas_head(tmp_path):
    runtime, _, _, _, provider = ready_proved_runtime(tmp_path)
    calls = 0

    def mutate():
        nonlocal calls
        calls += 1
        if calls == 2:
            value = runtime.store.get("strict_review_head", "goal-1")
            value["status"] = "REVIEW_REQUIRED"
            with sqlite3.connect(runtime.store.path) as db:
                db.execute("UPDATE records SET payload=? WHERE kind=? AND key=?",
                           (canonical(value), "strict_review_head", "goal-1"))

    provider.on_verify = mutate
    with pytest.raises(StrictWorkflowError):
        runtime.request_authorization("op-1")


def test_missing_append_only_guard_returns_public_error(tmp_path):
    runtime, *_ = ready_proved_runtime(tmp_path)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DROP TRIGGER strict_review_attestations_no_update")
    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_authorization("op-1")
    assert captured.value.code == "UNTRUSTED"
