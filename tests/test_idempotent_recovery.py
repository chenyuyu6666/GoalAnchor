"""Proposed T36/T37 recovery contract; a tested local fake is not a real backend claim."""
import hashlib
import multiprocessing
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from test_workflow import NOW, approve, operation, review, setup_workflow

from goalanchor.core import ContractValidationError
from goalanchor.workflow import Workflow, WorkflowPolicy
from goalanchor.workflow_protocol import EventAuthority, EvidenceSnapshot, canonical, fingerprint
from goalanchor.workflow_store import WorkflowStore

DEADLINE = "2026-10-16T01:30:00+08:00"


class IdempotentBackend:
    def __init__(self, name="fixture-backend"):
        self.name = name
        self.values = {}
        self.effects = 0
        self.transports = []

    def send(self, op):
        args = op.arguments
        key = args["idempotency_key"]
        self.transports.append(key)
        if key in self.values and self.values[key] != op.fingerprint:
            raise ValueError("same key different parameters")
        if key not in self.values:
            self.values[key] = op.fingerprint
            self.effects += 1
        return {"exit_code": 0, "object_ref": "artifact", "backend": self.name, "key": key}


def pending(tmp_path, *, tested_changes=None, resource=False, scope=False, second_backend=False):
    runtime, base, user, observer = setup_workflow(tmp_path)
    op = replace(operation(tmp_path, base), arguments_json=canonical({
        "text": "done", "idempotency_key": "K1", "backend": "fixture-backend"}))
    if resource:
        (tmp_path / "input").write_bytes(b"original")
        op = replace(op, resource_preconditions=(("input", hashlib.sha256(b"original").hexdigest()),))
    backend = IdempotentBackend()
    # Separately exercise the backend's same-key contract before registering its evidence.
    probe = IdempotentBackend()
    probe.send(op)
    probe.send(op)
    assert probe.effects == 1
    with pytest.raises(ValueError):
        probe.send(replace(op, arguments_json=canonical({"text": "changed", "idempotency_key": "K1"})))
    tested = {"contract": "goalanchor.idempotency-test.v1", "backend": "fixture-backend", "key": "K1",
              "operation_digest": op.fingerprint, "duplicate_same_key_same_parameters": True,
              "reject_changed_parameters": True, "logical_effects": 1}
    tested.update(tested_changes or {})
    snapshot = EvidenceSnapshot.capture("IDEMPOTENCY-TEST", "fixture-backend", canonical(tested).encode(),
                                        source="controlled-backend-test", collected_at=NOW, expires_at=DEADLINE)
    runtime.add_evidence("G1", snapshot, observer.sign("observer.evidence", snapshot.fingerprint,
                         {"goal_id": "G1", "snapshot": snapshot.to_dict()}, at=NOW))
    if second_backend:
        other_probe = IdempotentBackend("other-tested-backend")
        assert other_probe.send(op)["backend"] == "other-tested-backend"
        other_probe.send(op)
        assert other_probe.effects == 1
        with pytest.raises(ValueError):
            other_probe.send(replace(op, arguments_json=canonical({"text": "changed", "idempotency_key": "K1"})))
        other = {**tested, "backend": "other-tested-backend"}
        other_snapshot = EvidenceSnapshot.capture(
            "SECOND-TEST", "other-tested-backend", canonical(other).encode(),
            source="controlled-backend-test", collected_at=NOW, expires_at=DEADLINE
        )
        runtime.add_evidence("G1", other_snapshot, observer.sign(
            "observer.evidence", other_snapshot.fingerprint,
            {"goal_id": "G1", "snapshot": other_snapshot.to_dict()}, at=NOW
        ))
    review(runtime, observer)
    if scope:
        payload = {"goal_id": "G1", "baseline_digest": base.fingerprint, "scope_id": "S1",
                   "allowed_tools": ["write_text"], "workspace": str(tmp_path), "expires_at": DEADLINE}
        runtime.authorize_scope("G1", "S1", user.sign("user.scope", base.fingerprint, payload, at=NOW))
        runtime.register_operation(op)
    else:
        approve(runtime, op, user)
    def lost(actual):
        backend.send(actual)
        raise TimeoutError("backend applied effect; reply lost")
    assert runtime.execute("O1", "A1", lost)["state"] == "UNKNOWN"
    request = {"attempt_id": "A1", "operation_digest": op.fingerprint, "recovery_id": "R1",
               "backend": "fixture-backend", "key": "K1"}
    proof = {"attempt_id": "A1", "operation_digest": op.fingerprint, "backend": "fixture-backend",
             "key": "K1", "valid_until": DEADLINE, "test_evidence_id": "IDEMPOTENCY-TEST"}
    return runtime, base, user, observer, op, backend, request, proof


def resume(runtime, user, observer, request, proof, executor):
    return runtime.resume_transport("A1", user.sign("user.resume", "A1", request, at=NOW),
                                    observer.sign("observer.idempotency", "A1", proof, at=NOW), executor)


def test_t36_recovery_preserves_key_and_logical_effect_until_trusted_reconciliation(tmp_path):
    runtime, base, user, observer, op, backend, request, proof = pending(tmp_path)
    recovered = resume(runtime, user, observer, request, proof, backend.send)
    assert recovered["attempt_id"] == "A1" and recovered["recovery_id"] == "R1"
    assert recovered["operation_digest"] == op.fingerprint
    assert recovered["state"] == "AWAITING_RECONCILIATION"
    assert backend.transports == ["K1", "K1"] and backend.effects == 1
    assert runtime.store.get("receipt", "A1")["state"] == "UNKNOWN"
    result = {"exit_code": 0, "object_ref": "artifact"}
    observed = EvidenceSnapshot.capture(
        "RESULT", "O1", canonical(result).encode(), source="backend-query", collected_at=NOW
    )
    runtime.add_evidence("G1", observed, observer.sign("observer.evidence", observed.fingerprint,
                         {"goal_id": "G1", "snapshot": observed.to_dict()}, at=NOW))
    data = {"attempt_id": "A1", "operation_digest": op.fingerprint, "state": "SUCCEEDED", "result": result,
            "evidence_id": "RESULT", "source_request_id": "O1"}
    assert runtime.reconcile("A1", observer.sign("observer.reconcile", "A1", data, at=NOW))["state"] == "SUCCEEDED"
    assert backend.effects == 1


@pytest.mark.parametrize("change", ["unknown-backend", "wrong-key", "changed-parameters", "missing-test", "expired"])
def test_t37_unproved_or_expired_recovery_is_explicitly_refused(tmp_path, change):
    runtime, _, user, observer, _, backend, request, proof = pending(tmp_path)
    proof = deepcopy(proof)
    if change == "unknown-backend":
        proof["backend"] = request["backend"] = "untested-backend"
    elif change == "wrong-key":
        proof["key"] = request["key"] = "K2"
    elif change == "changed-parameters":
        proof["operation_digest"] = request["operation_digest"] = "0" * 64
    elif change == "missing-test":
        proof["test_evidence_id"] = "unregistered"
    else:
        runtime.clock = lambda: "2026-10-16T01:31:00+08:00"
    with pytest.raises(ContractValidationError):
        resume(runtime, user, observer, request, proof, backend.send)
    assert backend.transports == ["K1"]
    assert runtime.store.get("receipt", "A1")["state"] == "UNKNOWN"


def test_t36_concurrent_fresh_resume_events_only_one_recovery_transport(tmp_path):
    runtime, _, user, observer, _, backend, request, proof = pending(tmp_path)
    def contender(n):
        data = {**request, "recovery_id": "R" + str(n)}
        try:
            return resume(runtime, user, observer, data, proof, backend.send)
        except ContractValidationError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(contender, range(4)))
    assert any(item is not None for item in results)
    assert backend.transports == ["K1", "K1"]
    assert backend.effects == 1


def test_t37_timeout_after_recovery_never_automatically_resends(tmp_path):
    runtime, _, user, observer, _, backend, request, proof = pending(tmp_path)
    event = user.sign("user.resume", "A1", request, at=NOW)
    attestation = observer.sign("observer.idempotency", "A1", proof, at=NOW)
    def lost(op):
        backend.send(op)
        raise TimeoutError("recovery transport also lost")
    record = runtime.resume_transport("A1", event, attestation, lost)
    assert record["state"] == "UNKNOWN"
    assert runtime.resume_transport("A1", event, attestation, lost) == record
    assert backend.transports == ["K1", "K1"]
    assert backend.effects == 1


@pytest.mark.parametrize("change", ["revoked", "grant-expired"])
def test_t37_resume_requires_original_permission_still_valid(tmp_path, change):
    runtime, _, user, observer, _, backend, request, proof = pending(tmp_path)
    grant = runtime.store.get("receipt", "A1")["authorization_id"]
    if change == "revoked":
        runtime.revoke_authorization(grant, user.sign("user.revoke", grant, {"grant_id": grant}, at=NOW))
    else:
        old = runtime.store.get("grant", grant)
        expired = {**old, "expires_at": "2026-10-16T00:59:00+08:00"}
        runtime.store.commit([("grant", grant, expired)], {("grant", grant): fingerprint(old)},
                             "grant-deadline-fault", {"kind": "test-expired-original-permission"})
    with pytest.raises(ContractValidationError):
        resume(runtime, user, observer, request, proof, backend.send)
    assert backend.transports == ["K1"]


def test_t37_two_tested_backends_cannot_exchange_original_key(tmp_path):
    runtime, _, user, observer, op, backend, request, proof = pending(tmp_path, second_backend=True)
    assert op.arguments["backend"] == "fixture-backend"
    request["backend"] = proof["backend"] = "other-tested-backend"
    proof["test_evidence_id"] = "SECOND-TEST"
    with pytest.raises(ContractValidationError):
        resume(runtime, user, observer, request, proof, backend.send)
    assert backend.transports == ["K1"]


@pytest.mark.parametrize("phase", ["RESERVED", "CONSUMED"])
def test_t37_recovery_persistent_write_failure_never_dispatches(tmp_path, monkeypatch, phase):
    runtime, _, user, observer, _, backend, request, proof = pending(tmp_path)
    commit = runtime.store.commit
    def failed(changes, *args, **kwargs):
        if any(kind == "recovery" and data["phase"] == phase for kind, _, data in changes):
            raise sqlite3.OperationalError("injected durable write failure")
        return commit(changes, *args, **kwargs)
    monkeypatch.setattr(runtime.store, "commit", failed)
    with pytest.raises(sqlite3.OperationalError):
        resume(runtime, user, observer, request, proof, backend.send)
    assert backend.transports == ["K1"]
    record = runtime.store.get("recovery", "A1")
    assert record is None or not record["transport_right"]


@pytest.mark.parametrize("crash", ["before-send", "after-effect"])
def test_t37_consumed_recovery_crash_is_unknown_without_retransmit(tmp_path, crash):
    runtime, _, user, observer, _, backend, request, proof = pending(tmp_path)
    event = user.sign("user.resume", "A1", request, at=NOW)
    attestation = observer.sign("observer.idempotency", "A1", proof, at=NOW)
    def interrupted(op):
        if crash == "after-effect":
            backend.send(op)
        raise KeyboardInterrupt("process died")
    with pytest.raises(KeyboardInterrupt):
        runtime.resume_transport("A1", event, attestation, interrupted)
    runtime.store = WorkflowStore(tmp_path / "state.db")
    recovered = runtime.resume_transport("A1", event, attestation, lambda _: pytest.fail("no crash replay"))
    assert recovered["state"] == "UNKNOWN" and recovered["transport_right"]
    assert backend.transports == (["K1"] if crash == "before-send" else ["K1", "K1"])
    assert runtime.store.get("receipt", "A1")["state"] == "UNKNOWN"


@pytest.mark.parametrize("change", ["revoke", "evidence-expiry", "cancel", "blocking-question"])
def test_t37_consume_boundary_changes_prevent_recovery_dispatch(tmp_path, monkeypatch, change):
    from goalanchor.questions import Question, QuestionOption
    runtime, base, user, observer, _, backend, request, proof = pending(tmp_path)
    commit = runtime.store.commit
    def interleave(changes, *args, **kwargs):
        if any(kind == "recovery" and data["phase"] == "CONSUMED" for kind, _, data in changes):
            if change == "evidence-expiry":
                runtime.clock = lambda: "2026-10-16T01:31:00+08:00"
            elif change == "revoke":
                grant = runtime.store.get("receipt", "A1")["authorization_id"]
                runtime.revoke_authorization(grant, user.sign("user.revoke", grant, {"grant_id": grant}, at=NOW))
            elif change == "cancel":
                runtime.cancel("G1", user.sign("user.stop", base.fingerprint,
                               {"goal_id": "G1", "reason": "stop before recovery send"}, at=NOW))
            else:
                runtime.request_question(Question("new-block", "G1", 1, "direction", "Which?",
                    (QuestionOption("a", "A"),), blocking_items=("W1",), baseline_digest=base.fingerprint))
        return commit(changes, *args, **kwargs)
    monkeypatch.setattr(runtime.store, "commit", interleave)
    with pytest.raises(ContractValidationError):
        resume(runtime, user, observer, request, proof, backend.send)
    assert backend.transports == ["K1"]
    assert not runtime.store.get("recovery", "A1")["transport_right"]


def test_t37_forged_idempotency_proof_is_rejected(tmp_path):
    runtime, _, user, _, _, backend, request, proof = pending(tmp_path)
    forged = EventAuthority(b"x" * 32, "trusted-runner")
    with pytest.raises(ContractValidationError):
        resume(runtime, user, forged, request, proof, backend.send)
    assert backend.transports == ["K1"]


@pytest.mark.parametrize("changed", [{"duplicate_same_key_same_parameters": 1},
                                    {"reject_changed_parameters": "true"}, {"logical_effects": True}])
def test_t37_tested_contract_requires_exact_boolean_and_integer_types(tmp_path, changed):
    runtime, _, user, observer, _, backend, request, proof = pending(tmp_path, tested_changes=changed)
    with pytest.raises(ContractValidationError):
        resume(runtime, user, observer, request, proof, backend.send)
    assert backend.transports == ["K1"]


@pytest.mark.parametrize("change", ["resource", "parent-revoked"])
def test_t37_original_resource_and_parent_scope_remain_required(tmp_path, change):
    runtime, _, user, observer, _, backend, request, proof = pending(
        tmp_path, resource=change == "resource", scope=change == "parent-revoked"
    )
    if change == "resource":
        (tmp_path / "input").write_bytes(b"changed")
    else:
        child = runtime.store.get("grant", runtime.store.get("receipt", "A1")["authorization_id"])
        parent = child["parent_id"]
        runtime.revoke_authorization(parent, user.sign("user.revoke", parent, {"grant_id": parent}, at=NOW))
    with pytest.raises(ContractValidationError):
        resume(runtime, user, observer, request, proof, backend.send)
    assert backend.transports == ["K1"]


def recovery_contender(directory, request, proof, ready, start, results):
    runtime = Workflow(WorkflowStore(os.path.join(directory, "state.db")),
                       EventAuthority(b"u" * 32, "trusted-ui"), EventAuthority(b"o" * 32, "trusted-runner"),
                       WorkflowPolicy(directory, ("write_text",)), clock=lambda: NOW)
    ready.put(True)
    start.wait(10)
    def send(op):
        descriptor = os.open(os.path.join(directory, "recovery-sends"),
                             os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            os.write(descriptor, b"send\n")
        finally:
            os.close(descriptor)
        return {"exit_code": 0, "backend": op.arguments["backend"], "key": op.arguments["idempotency_key"]}
    try:
        resume(runtime, runtime.user_authority, runtime.observer_authority, request, proof, send)
        results.put("won")
    except ContractValidationError:
        results.put("rejected")
    except BaseException as exc:
        results.put(type(exc).__name__)


def test_t36_shared_sqlite_processes_elect_one_recovery_transport(tmp_path):
    _, _, _, _, _, _, request, proof = pending(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=recovery_contender,
               args=(str(tmp_path), {**request, "recovery_id": f"process-{n}"}, proof, ready, start, results))
               for n in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=15)
        start.set()
        outcomes = [results.get(timeout=15) for _ in workers]
        assert sorted(outcomes) == ["rejected", "won"]
        assert (tmp_path / "recovery-sends").read_bytes() == b"send\n"
    finally:
        start.set()
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        assert all(worker.exitcode == 0 for worker in workers)
