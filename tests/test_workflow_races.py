from contextlib import contextmanager
from dataclasses import replace

import pytest
from test_workflow import NOW, approve, operation, review, setup_workflow

from goalanchor.core import ContractValidationError
from goalanchor.workflow_protocol import EvidenceSnapshot


def test_new_evidence_between_review_check_and_reserve_blocks_dispatch(tmp_path, monkeypatch):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    reserve = runtime.store.reserve

    def interleave(*args, **kwargs):
        snapshot = EvidenceSnapshot.capture("E2", "new-facts", b"changed", source="runner", collected_at=NOW)
        runtime.add_evidence("G1", snapshot, observer.sign("observer.evidence", snapshot.fingerprint,
                             {"goal_id": "G1", "snapshot": snapshot.to_dict()}, at=NOW))
        return reserve(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "reserve", interleave)
    calls = []
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "race", lambda _: calls.append(1) or {"exit_code": 0})
    assert calls == []


def test_unknown_created_during_closure_check_prevents_closed_commit(tmp_path, monkeypatch):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    runtime.execute("O1", "done", lambda _: {"exit_code": 0, "object_ref": "artifact"})
    payload = dict(goal_id="G1", baseline_digest=base.fingerprint, criterion_id="C1",
                   object_ref="artifact", actual="done", attempt_id="done")
    runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, payload, at=NOW))
    approve(runtime, replace(operation(tmp_path, base), operation_id="O2", work_item_id="W2"), user)
    commit = runtime.store.commit

    def interleave(changes, expected, event_id, event, **kwargs):
        if any(kind == "closure" for kind, _, _ in changes):
            def lost(_):
                raise TimeoutError("unknown effect")
            assert runtime.execute("O2", "lost", lost)["state"] == "UNKNOWN"
        return commit(changes, expected, event_id, event, **kwargs)

    monkeypatch.setattr(runtime.store, "commit", interleave)
    with pytest.raises(ContractValidationError):
        runtime.close("G1")
    assert runtime.store.get("goal", "G1")["state"] != "CLOSED"


def test_close_transaction_rejects_dependency_expiring_after_checks(tmp_path, monkeypatch):
    runtime, base, user, observer = setup_workflow(tmp_path)
    snapshot = EvidenceSnapshot.capture("short", "facts", b"time-bound", source="runner", collected_at=NOW,
                                       expires_at="2026-10-16T01:00:01+08:00")
    runtime.add_evidence("G1", snapshot, observer.sign("observer.evidence", snapshot.fingerprint,
                         {"goal_id": "G1", "snapshot": snapshot.to_dict()}, at=NOW))
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    runtime.execute("O1", "done", lambda _: {"exit_code": 0, "object_ref": "artifact"})
    payload = dict(goal_id="G1", baseline_digest=base.fingerprint, criterion_id="C1",
                   object_ref="artifact", actual="done", attempt_id="done")
    runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, payload, at=NOW))
    commit = runtime.store.commit

    def interleave(changes, expected, event_id, event, **kwargs):
        if any(kind == "closure" for kind, _, _ in changes):
            runtime.clock = lambda: "2026-10-16T01:00:02+08:00"
        return commit(changes, expected, event_id, event, **kwargs)

    monkeypatch.setattr(runtime.store, "commit", interleave)
    with pytest.raises(ContractValidationError):
        runtime.close("G1")
    assert runtime.store.get("goal", "G1")["state"] != "CLOSED"


def test_consume_samples_current_time_after_transaction_lock(tmp_path, monkeypatch):
    runtime, base, user, observer = setup_workflow(tmp_path)
    snapshot = EvidenceSnapshot.capture("short", "facts", b"time-bound", source="runner", collected_at=NOW,
                                       expires_at="2026-10-16T01:00:01+08:00")
    runtime.add_evidence("G1", snapshot, observer.sign("observer.evidence", snapshot.fingerprint,
                         {"goal_id": "G1", "snapshot": snapshot.to_dict()}, at=NOW))
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    consume = runtime.store.consume
    transaction = runtime.store._transaction

    @contextmanager
    def waited_transaction():
        with transaction() as db:
            runtime.clock = lambda: "2026-10-16T01:00:02+08:00"
            yield db

    def consume_after_wait(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "_transaction", waited_transaction)
            return consume(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "consume", consume_after_wait)
    calls = []
    with pytest.raises(ContractValidationError, match="expired"):
        runtime.execute("O1", "past-deadline", lambda _: calls.append(1) or {"exit_code": 0})
    assert calls == []
    assert not runtime.store.get("receipt", "past-deadline")["dispatch_right"]
