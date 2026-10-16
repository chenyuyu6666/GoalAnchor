"""Adversarial delivery acceptance; only trusted local fixtures, no real dispatch."""
from copy import deepcopy
from dataclasses import replace

import pytest
from test_workflow import NOW, approve, operation, review, setup_workflow

from goalanchor.core import ContractValidationError
from goalanchor.integration import ReviewFinding
from goalanchor.questions import Question, QuestionOption
from goalanchor.workflow_protocol import EvidenceSnapshot, canonical


def test_repeated_answer_creates_one_grant(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    runtime.register_operation(operation(tmp_path, base))
    q = runtime.request_authorization("O1").question
    event = user.sign("user.answer", q.fingerprint,
                      {"answer_id": "A", "selected": ["approve"], "custom": None}, at=NOW)
    assert runtime.answer(q.question_id, event) == runtime.answer(q.question_id, event)
    assert len(runtime.store.list("grant", "G1")) == 1
    assert runtime.store.list("receipt", "G1") == ()


@pytest.mark.parametrize("mutation", ["shared-session", "different-model", "missing-binding"])
def test_review_requires_verified_role_isolation(tmp_path, mutation):
    (tmp_path / "source").mkdir()
    (tmp_path / "target").mkdir()
    source, _, _, source_observer = setup_workflow(tmp_path / "source")
    bundle = deepcopy(review(source, source_observer)["history"][0])
    runtime, _, _, observer = setup_workflow(tmp_path / "target")
    if mutation == "shared-session":
        bundle["bindings"][1]["session_id"] = bundle["bindings"][0]["session_id"]
    elif mutation == "different-model":
        bundle["bindings"][1]["model"] = "different"
    else:
        bundle["bindings"].pop()
    ctx = runtime.review_context("G1")
    with pytest.raises(ContractValidationError):
        runtime.submit_review("G1", bundle, observer.sign("observer.review", ctx.fingerprint, bundle, at=NOW))
    assert runtime.store.get("review", "G1") is None


@pytest.mark.parametrize("kind,subject,at", [
    ("user.scope", "correct", NOW), ("user.answer", "wrong", NOW),
    ("user.answer", "correct", "2026-10-17T01:00:00+08:00"),
])
def test_answer_rejects_wrong_event_binding(tmp_path, kind, subject, at):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    runtime.register_operation(operation(tmp_path, base))
    q = runtime.request_authorization("O1").question
    event = user.sign(kind, q.fingerprint if subject == "correct" else subject,
                      {"answer_id": "A", "selected": ["approve"], "custom": None}, at=at)
    with pytest.raises(ContractValidationError):
        runtime.answer(q.question_id, event)
    assert runtime.store.list("grant", "G1") == ()


@pytest.mark.parametrize("status,refs", [("UNRESOLVED", []), ("FIXED", ["missing"]),
                                          ("REFUTED", ["E1"]), ("RISK_ACCEPTED", ["E1"])])
def test_equal_pass_verdicts_cannot_hide_finding(tmp_path, status, refs):
    runtime, _, _, observer = setup_workflow(tmp_path)
    finding = ReviewFinding("F1", "unsafe assumption", ("E1",), "blocks execution", "unverified")
    result = review(runtime, observer, finding=finding,
                    dispositions={"F1": {"status": status, "evidence_refs": refs}})
    assert result["status"] != "PASSED"
    assert "F1" in result["unresolved"]


@pytest.mark.parametrize("state", ["CANCELLED", "EXPIRED", "SUPERSEDED"])
def test_terminal_question_never_issues_authorization(tmp_path, state):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    runtime.register_operation(operation(tmp_path, base))
    q = runtime.request_authorization("O1").question
    payload = {"question_id": q.question_id, "state": state}
    runtime.transition_question(q.question_id, state, user.sign("user.question", q.fingerprint, payload, at=NOW))
    with pytest.raises(ContractValidationError):
        runtime.answer(q.question_id, user.sign("user.answer", q.fingerprint,
                       {"answer_id": "late", "selected": ["approve"], "custom": None}, at=NOW))
    assert runtime.store.list("grant", "G1") == ()


def test_cancel_blocks_new_dispatch_and_late_observation(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    runtime.execute("O1", "done", lambda _: {"exit_code": 0, "object_ref": "artifact"})
    runtime.cancel("G1", user.sign("user.stop", base.fingerprint,
                                  {"goal_id": "G1", "reason": "stop"}, at=NOW))
    calls = []
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "later", lambda _: calls.append(1))
    observed = {"goal_id": "G1", "baseline_digest": base.fingerprint, "criterion_id": "C1",
                "object_ref": "artifact", "actual": "done", "attempt_id": "done"}
    with pytest.raises(ContractValidationError):
        runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, observed, at=NOW))
    assert calls == []


def test_scope_reuses_permission_but_revocation_blocks_dispatch(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    payload = {"goal_id": "G1", "baseline_digest": base.fingerprint, "scope_id": "S1",
               "allowed_tools": ["write_text"], "workspace": str(tmp_path),
               "expires_at": "2026-10-16T02:00:00+08:00"}
    event = user.sign("user.scope", base.fingerprint, payload, at=NOW)
    grant = runtime.authorize_scope("G1", "S1", event)
    assert runtime.authorize_scope("G1", "S1", event) == grant
    calls = []
    for n in range(2):
        op = replace(operation(tmp_path, base), operation_id=f"scope-{n}",
                     arguments_json=canonical({"path": f"{n}.txt", "text": "done"}))
        runtime.register_operation(op)
        assert runtime.execute(op.operation_id, f"scope-attempt-{n}",
                               lambda _: calls.append(1) or {"exit_code": 0})["state"] == "SUCCEEDED"
    assert len(calls) == 2
    assert runtime.store.list("question", "G1") == ()
    grant_id = grant["authorization_id"]
    runtime.revoke_authorization(grant_id, user.sign("user.revoke", grant_id, {"grant_id": grant_id}, at=NOW))
    op = replace(operation(tmp_path, base), operation_id="after-revoke")
    runtime.register_operation(op)
    with pytest.raises(ContractValidationError):
        runtime.execute(op.operation_id, "revoked", lambda _: calls.append(1))
    assert len(calls) == 2


def test_scope_narrower_than_policy_cannot_authorize_outer_workspace(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    child = tmp_path / "allowed"
    child.mkdir()
    payload = {"goal_id": "G1", "baseline_digest": base.fingerprint, "scope_id": "narrow",
               "allowed_tools": ["write_text"], "workspace": str(child),
               "expires_at": "2026-10-16T02:00:00+08:00"}
    runtime.authorize_scope("G1", "narrow", user.sign("user.scope", base.fingerprint, payload, at=NOW))
    runtime.register_operation(operation(tmp_path, base))
    calls = []
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "outside-scope", lambda _: calls.append(1))
    assert calls == []


def test_new_baseline_does_not_reuse_old_closure(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    runtime.execute("O1", "done", lambda _: {"exit_code": 0, "object_ref": "artifact"})
    observed = {"goal_id": "G1", "baseline_digest": base.fingerprint, "criterion_id": "C1",
                "object_ref": "artifact", "actual": "done", "attempt_id": "done"}
    runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, observed, at=NOW))
    closed = runtime.close("G1")
    new = replace(base, revision=2, plan="new acceptance run")
    runtime.start_baseline(new, user.sign("user.baseline", new.fingerprint, new.to_dict(), at=NOW))
    assert runtime.store.get("closure", base.fingerprint) == closed
    assert runtime.store.get("goal", "G1")["state"] != "CLOSED"
    with pytest.raises(ContractValidationError):
        runtime.close("G1")


def test_unknown_reconciliation_requires_correlated_evidence_and_never_resends(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    op = operation(tmp_path, base)
    approve(runtime, op, user)
    calls = []
    def timeout(_):
        calls.append(1)
        raise TimeoutError("response lost")
    assert runtime.execute("O1", "lost", timeout)["state"] == "UNKNOWN"
    snapshot = EvidenceSnapshot.capture("E-result", "O1", b"observed success", source="runner", collected_at=NOW)
    runtime.add_evidence("G1", snapshot, observer.sign("observer.evidence", snapshot.fingerprint,
                         {"goal_id": "G1", "snapshot": snapshot.to_dict()}, at=NOW))
    payload = {"attempt_id": "lost", "operation_digest": op.fingerprint, "state": "SUCCEEDED",
               "result": {"exit_code": 0, "object_ref": "artifact"}, "evidence_id": "E-result",
               "source_request_id": "wrong-request"}
    with pytest.raises(ContractValidationError):
        runtime.reconcile("lost", observer.sign("observer.reconcile", "lost", payload, at=NOW))
    assert runtime.store.get("receipt", "lost")["state"] == "UNKNOWN"
    payload["source_request_id"] = "O1"
    event = observer.sign("observer.reconcile", "lost", payload, at=NOW)
    assert runtime.reconcile("lost", event)["state"] == "SUCCEEDED"
    assert runtime.reconcile("lost", event)["state"] == "SUCCEEDED"
    assert calls == [1]


@pytest.mark.parametrize("change", [{"object_ref": "other"}, {"baseline_digest": "0" * 64},
                                    {"object_digest": "0" * 64}, {"actual": "stale version"}])
def test_observation_cannot_substitute_object_or_version(tmp_path, change):
    import hashlib
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    digest = hashlib.sha256(b"done").hexdigest()
    runtime.execute("O1", "A", lambda _: {"exit_code": 0, "object_ref": "artifact", "object_digest": digest})
    payload = {"goal_id": "G1", "baseline_digest": base.fingerprint, "criterion_id": "C1",
               "object_ref": "artifact", "actual": "done", "attempt_id": "A", **change}
    with pytest.raises(ContractValidationError):
        runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, payload, at=NOW))
    assert runtime.store.list("observation", "G1") == ()


def test_direction_answer_and_repeated_checks_do_not_dispatch_or_grant(tmp_path):
    runtime, base, user, _ = setup_workflow(tmp_path)
    q = Question("direction", "G1", 1, "choose", "choose path", (QuestionOption("a", "A"),),
                 baseline_digest=base.fingerprint, blocking_items=("W1",))
    runtime.request_question(q)
    runtime.answer(q.question_id, user.sign("user.answer", q.fingerprint,
                   {"answer_id": "A", "selected": ["a"], "custom": None}, at=NOW))
    for _ in range(3):
        assert runtime.work_item_decision("G1", "W1") == "ALLOW"
    assert runtime.store.list("grant", "G1") == ()
    assert runtime.store.list("receipt", "G1") == ()
