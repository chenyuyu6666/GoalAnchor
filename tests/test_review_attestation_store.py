"""Offline storage boundaries for controller-attested review proofs."""
from __future__ import annotations

import multiprocessing
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.workflow_protocol import EventAuthority, canonical, fingerprint
from goalanchor.workflow_store import WorkflowStore, WorkflowStoreConflict
from goalanchor.workflow_store_core import WorkflowStoreCore

NOW = "2026-10-25T01:00:00+08:00"
LATER = "2026-10-25T02:00:00+08:00"
AUTHORITY = EventAuthority(b"r" * 32, "test-review-controller")


@contextmanager
def lease(expected):
    yield {"binding_digest": expected, "generation": 1}


def ready_store(tmp_path):
    store = WorkflowStore(tmp_path / "review.db")
    store.commit([("goal", "G", {"goal_id": "G", "generation": 0})],
                 {("goal", "G"): None}, "seed", {"kind": "fixture"})
    return store


def batch(store, suffix="one"):
    source = AUTHORITY.sign("observer.review", "context", {"round_number": 1},
                            event_id="review-" + suffix, at=NOW)
    companion = AUTHORITY.sign("controller.review.attested", "context", {
        "goal_id": "G", "round_number": 1, "source_event_id": source.event_id,
        "source_event_digest": fingerprint(source.to_dict()), "proof": {"fixture": "not-native"},
    }, event_id="attested-" + suffix, at=NOW)
    record = {"goal_id": "G", "source_event_id": source.event_id, "status": "PASSED"}
    changes = [("strict_review_record", "G", record), ("strict_review_head", "G", record)]
    expected = {("goal", "G"): store.fingerprint(store.get("goal", "G")),
                ("strict_review_record", "G"): None, ("strict_review_head", "G"): None}
    events = [(e.event_id, e.to_dict()) for e in (source, companion)]
    attestation = {"goal_id": "G", "round_number": 1,
                   "source_event_id": source.event_id, "companion_event": companion.to_dict()}
    return changes, expected, events, attestation


def commit(store, values, **options):
    changes, expected, events, attestation = values
    return store.commit_review_bound(
        changes, expected, events, valid_until=LATER, clock=options.get("clock", lambda: NOW),
        lease_factory=options.get("lease_factory", lease), expected_runtime_digest="runtime",
        expected_lease_generation=1, attestation=attestation,
    )


@pytest.mark.parametrize("version", [1, 2])
def test_review_schema_is_additive_and_never_promotes_legacy_rows(tmp_path, version):
    path = tmp_path / "old.db"
    if version == 1:
        with sqlite3.connect(path) as db:
            WorkflowStoreCore._create_base_schema(db)
            db.execute("PRAGMA user_version=1")
    else:
        WorkflowStoreCore(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO records VALUES('review','old','{}')")
    store = WorkflowStore(path)
    assert store.get("review", "old") == {}
    assert store.get_review_attestation("G", 1) is None
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM strict_review_attestations").fetchone()[0] == 0


def test_atomic_attestation_reopen_and_idempotent_postimage_validation(tmp_path):
    store = ready_store(tmp_path)
    values = batch(store)
    before_goal = store.get("goal", "G")
    assert commit(store, values) is True
    reopened = WorkflowStore(store.path)
    assert reopened.get_review_attestation("G", 1) == values[3]
    assert reopened.get("goal", "G") == before_goal
    assert commit(reopened, values) is False
    assert len(store.audit()) == 3
    assert store.get("strict_review_head", "G") == values[0][1][2]


@pytest.mark.parametrize("sql", [
    "UPDATE strict_review_attestations SET goal_id='other'",
    "DELETE FROM strict_review_attestations",
    "INSERT OR REPLACE INTO strict_review_attestations SELECT * FROM strict_review_attestations",
])
def test_attestation_table_is_append_only_even_with_recursive_triggers_off(tmp_path, sql):
    store = ready_store(tmp_path)
    values = batch(store)
    commit(store, values)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA recursive_triggers=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    assert store.get_review_attestation("G", 1) == values[3]


def test_replay_rejects_changed_projection_without_repair(tmp_path):
    store = ready_store(tmp_path)
    values = batch(store)
    commit(store, values)
    old = store.get("strict_review_record", "G")
    damaged = {**old, "status": "CORRUPTED"}
    store.commit([("strict_review_record", "G", damaged)],
                 {("strict_review_record", "G"): store.fingerprint(old)}, "corrupt", {})
    with pytest.raises(ContractValidationError):
        commit(store, values)
    assert store.get("strict_review_record", "G") == damaged


def test_partial_event_replay_is_rejected_atomically(tmp_path):
    store = ready_store(tmp_path)
    values = batch(store)
    event_id, event = values[2][0]
    store.commit([], {}, event_id, event)
    with pytest.raises(ContractValidationError):
        commit(store, values)
    assert store.get_review_attestation("G", 1) is None
    assert store.get("strict_review_record", "G") is None
    assert store.get_event(values[2][1][0]) is None


def test_missing_table_read_and_reopen_do_not_repair_attested_history(tmp_path):
    store = ready_store(tmp_path)
    values = batch(store)
    commit(store, values)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE strict_review_attestations")
    with pytest.raises(ContractValidationError):
        store.get_review_attestation("G", 1)
    with pytest.raises(ContractValidationError):
        WorkflowStore(store.path)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='strict_review_attestations'").fetchone() is None


def test_orphaned_attestation_row_is_detected_without_repair(tmp_path):
    store = ready_store(tmp_path)
    values = batch(store)
    commit(store, values)
    with sqlite3.connect(store.path) as db:
        trigger = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='strict_review_attestations_no_delete'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER strict_review_attestations_no_delete")
        db.execute("DELETE FROM strict_review_attestations")
        db.execute(trigger)
    with pytest.raises(ContractValidationError):
        store.get_review_attestation("G", 1)
    with pytest.raises(ContractValidationError):
        commit(store, values)


def test_record_failure_rolls_back_both_events_and_attestation(tmp_path, monkeypatch):
    store = ready_store(tmp_path)
    values = batch(store)
    original = store._put

    def fail(db, kind, key, payload):
        original(db, kind, key, payload)
        raise RuntimeError("injected after first projection write")

    monkeypatch.setattr(store, "_put", fail)
    with pytest.raises(RuntimeError):
        commit(store, values)
    assert store.get("strict_review_record", "G") is None
    assert store.get_review_attestation("G", 1) is None
    assert len(store.audit()) == 1


def test_cas_contenders_commit_exactly_one_complete_round(tmp_path):
    store = ready_store(tmp_path)
    inputs = [batch(store, "A"), batch(store, "B")]
    barrier = threading.Barrier(2)

    def attempt(values):
        other = WorkflowStore(store.path)
        barrier.wait(timeout=3)
        try:
            return commit(other, values)
        except WorkflowStoreConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(attempt, inputs)).count(True) == 1
    proof = store.get_review_attestation("G", 1)
    assert store.get("strict_review_record", "G")["source_event_id"] == proof["source_event_id"]
    assert len(store.audit()) == 3


@pytest.mark.parametrize("change", ["expiry", "identity"])
def test_waiting_writer_rechecks_deadline_and_lease(tmp_path, change):
    store = ready_store(tmp_path)
    values = batch(store)
    now = [NOW]
    generation = [1]
    entered = threading.Event()
    lease_calls = []

    @contextmanager
    def rotating_lease(expected):
        lease_calls.append(True)
        yield {"binding_digest": expected, "generation": generation[0]}

    def writer():
        entered.set()
        return commit(store, values, clock=lambda: now[0], lease_factory=rotating_lease)

    with sqlite3.connect(store.path, isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(writer)
            assert entered.wait(timeout=3)
            assert not result.done()
            assert not lease_calls
            if change == "expiry":
                now[0] = LATER
            else:
                generation[0] = 2
            blocker.rollback()
            with pytest.raises(WorkflowStoreConflict) as caught:
                result.result(timeout=5)
            assert caught.value.code == "STALE"
    assert store.get_review_attestation("G", 1) is None
    assert len(store.audit()) == 1


def test_expiry_after_writes_rolls_back_all_outputs(tmp_path, monkeypatch):
    store = ready_store(tmp_path)
    values = batch(store)
    now = [NOW]
    original = store._put

    def expire(db, kind, key, payload):
        original(db, kind, key, payload)
        now[0] = LATER

    monkeypatch.setattr(store, "_put", expire)
    with pytest.raises(WorkflowStoreConflict) as caught:
        commit(store, values, clock=lambda: now[0])
    assert caught.value.code == "STALE"
    assert store.get_review_attestation("G", 1) is None
    assert len(store.audit()) == 1


def test_same_events_with_different_payload_or_companion_cannot_replay(tmp_path):
    store = ready_store(tmp_path)
    values = batch(store)
    commit(store, values)
    events = list(values[2])
    raw = dict(events[0][1])
    raw["payload"] = {"round_number": 2}
    events[0] = (events[0][0], raw)
    with pytest.raises(ContractValidationError):
        commit(store, (values[0], values[1], events, values[3]))


def test_attestation_requires_companion_and_source_in_same_batch(tmp_path):
    store = ready_store(tmp_path)
    values = batch(store)
    with pytest.raises(ContractValidationError):
        commit(store, (values[0], values[1], values[2][:1], values[3]))
    assert len(store.audit()) == 1


def test_offline_bound_write_preserves_goal_generation_and_replay(tmp_path):
    store = ready_store(tmp_path)
    goal = store.get("goal", "G")
    event = AUTHORITY.sign("controller.question.created", "Q", {"goal_id": "G"}, at=NOW)
    values = ([("strict_question", "Q", {"goal_id": "G", "state": "WAITING"})],
              {("strict_question", "Q"): None, ("goal", "G"): store.fingerprint(goal)},
              [(event.event_id, event.to_dict())], None)
    assert commit(store, values)
    assert store.get("goal", "G")["generation"] == goal["generation"] + 1
    assert commit(store, values) is False


def test_row_columns_must_match_payload_and_source_event(tmp_path):
    store = ready_store(tmp_path)
    values = batch(store)
    commit(store, values)
    with sqlite3.connect(store.path) as db:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='strict_review_attestations_no_update'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER strict_review_attestations_no_update")
        corrupt = {**values[3], "source_event_id": "wrong"}
        db.execute("UPDATE strict_review_attestations SET payload=?", (canonical(corrupt),))
        db.execute(sql)
    with pytest.raises(ContractValidationError):
        store.get_review_attestation("G", 1)


def test_attested_transaction_cannot_write_another_goals_review(tmp_path):
    store = ready_store(tmp_path)
    changes, expected, events, attestation = batch(store)
    changed = [(kind, key, {**value, "goal_id": "other"}) for kind, key, value in changes]
    with pytest.raises(ContractValidationError):
        commit(store, (changed, expected, events, attestation))
    assert len(store.audit()) == 1


@pytest.mark.parametrize("failure", ["expired", "identity", "dependency"])
def test_event_replay_still_requires_live_deadline_identity_and_dependency(tmp_path, failure):
    store = ready_store(tmp_path)
    values = batch(store)
    commit(store, values)
    options = {}
    if failure == "expired":
        options["clock"] = lambda: LATER
    elif failure == "identity":
        @contextmanager
        def wrong_identity(expected):
            yield {"binding_digest": expected, "generation": 2}
        options["lease_factory"] = wrong_identity
    else:
        old = store.get("goal", "G")
        store.commit([("goal", "G", {**old, "state": "CHANGED"})],
                     {("goal", "G"): store.fingerprint(old)}, "dependency", {})
    before = store.audit()
    with pytest.raises(ContractValidationError):
        commit(store, values, **options)
    assert store.audit() == before
    assert store.get_review_attestation("G", 1) == values[3]


def test_missing_append_only_guard_is_not_silently_recreated(tmp_path):
    store = ready_store(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER strict_review_attestations_no_update")
    with pytest.raises(ContractValidationError):
        store.get_review_attestation("G", 1)
    with pytest.raises(ContractValidationError):
        WorkflowStore(store.path)
    with sqlite3.connect(store.path) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='strict_review_attestations_no_update'"
        ).fetchone() is None


def test_native_companion_cannot_be_replayed_as_offline_batch(tmp_path):
    store = ready_store(tmp_path)
    changes, expected, events, attestation = batch(store)
    commit(store, (changes, expected, events, attestation))
    with pytest.raises(ContractValidationError):
        commit(store, (changes, expected, events, None))


def test_wrong_source_event_digest_is_rejected_before_writes(tmp_path):
    store = ready_store(tmp_path)
    changes, expected, events, attestation = batch(store)
    old = attestation["companion_event"]
    companion = AUTHORITY.sign("controller.review.attested", old["subject"],
                               {**old["payload"], "source_event_digest": "0" * 64},
                               event_id=old["event_id"], at=NOW)
    events[-1] = (companion.event_id, companion.to_dict())
    attestation["companion_event"] = companion.to_dict()
    with pytest.raises(ContractValidationError):
        commit(store, (changes, expected, events, attestation))
    assert len(store.audit()) == 1


def process_batch(store, suffix):
    changes, expected, events, attestation = batch(store, suffix)
    chain_key = "G:1"
    chain = {"goal_id": "G", "round_number": 1, "source_event_id": events[0][0]}
    changes = [
        (kind, key, {**value, "chain_key": chain_key, "round_number": 1})
        for kind, key, value in changes
    ]
    changes.append(("strict_review_chain", chain_key, chain))
    expected[("strict_review_chain", chain_key)] = None
    return changes, expected, events, attestation


def compete_review_attestation_process(path, suffix, ready, start, results):
    """Each spawned interpreter owns its Store and fixture-only runtime lease."""
    store = WorkflowStore(path)
    values = process_batch(store, suffix)
    ready.put(suffix)
    if not start.wait(timeout=20):
        raise RuntimeError("competition start barrier timed out")
    try:
        won = commit(store, values)
        results.put((suffix, "COMMITTED" if won else "REPLAYED", None))
    except WorkflowStoreConflict as exc:
        results.put((suffix, "REJECTED", exc.code))


def crash_review_attestation_process(path):
    """Exit after every write, before SQLite COMMIT; no exception-driven rollback."""
    store = WorkflowStore(path)
    transaction = store._strict_transaction

    @contextmanager
    def crash_before_commit(*args, **kwargs):
        with transaction(*args, **kwargs) as state:
            yield state
            db, _lease = state
            # Exit 73 proves the process reached a fully written, uncommitted batch.
            if (
                db.execute("SELECT COUNT(*) FROM strict_review_attestations").fetchone()[0] != 1
                or db.execute("SELECT COUNT(*) FROM events").fetchone()[0] != 3
                or db.execute(
                    "SELECT COUNT(*) FROM records WHERE kind IN "
                    "('strict_review_record','strict_review_head','strict_review_chain')"
                ).fetchone()[0] != 3
            ):
                os._exit(74)
            os._exit(73)

    store._strict_transaction = crash_before_commit
    commit(store, process_batch(store, "crash"))
    raise AssertionError("the injected process exit must happen before commit returns")


def test_two_spawned_processes_commit_exactly_one_complete_attestation(tmp_path):
    store = ready_store(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(
        target=compete_review_attestation_process,
        args=(store.path, suffix, ready, start, results),
    ) for suffix in ("process-A", "process-B")]
    try:
        for worker in workers:
            worker.start()
        # Both CAS preimages are frozen before either process may write.
        assert {ready.get(timeout=30) for _ in workers} == {"process-A", "process-B"}
        start.set()
        outcomes = [results.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        winners = [item for item in outcomes if item[1] == "COMMITTED"]
        losers = [item for item in outcomes if item[1] == "REJECTED"]
        assert len(winners) == len(losers) == 1
        assert losers[0][2] in {"CONFLICT", "STALE"}

        reopened = WorkflowStore(store.path)
        attestation = reopened.get_review_attestation("G", 1)
        assert attestation["source_event_id"] == "review-" + winners[0][0]
        assert reopened.get_event("review-" + losers[0][0]) is None
        assert reopened.get_event("attested-" + losers[0][0]) is None
        assert {entry["event_id"] for entry in reopened.audit()} == {
            "seed", "review-" + winners[0][0], "attested-" + winners[0][0],
        }
        chains = reopened.list("strict_review_chain", "G")
        head = reopened.get("strict_review_head", "G")
        record = reopened.get("strict_review_record", "G")
        assert len(chains) == 1 and chains[0]["round_number"] == head["round_number"] == 1
        assert head["chain_key"] == "G:1"
        assert chains[0]["source_event_id"] == head["source_event_id"] == record["source_event_id"]
        assert record["source_event_id"] == attestation["source_event_id"]
        with sqlite3.connect(store.path) as db:
            assert db.execute("SELECT COUNT(*) FROM strict_review_attestations").fetchone()[0] == 1
    finally:
        start.set()
        for worker in workers:
            if worker.pid is not None:
                worker.join(timeout=5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
        for channel in (ready, results):
            channel.close()
            channel.join_thread()


def test_process_crash_before_commit_rolls_back_sources_proof_and_projections(tmp_path):
    store = ready_store(tmp_path)
    original_goal = store.get("goal", "G")
    context = multiprocessing.get_context("spawn")
    worker = context.Process(target=crash_review_attestation_process, args=(store.path,))
    try:
        worker.start()
        worker.join(timeout=30)
        assert worker.exitcode == 73
    finally:
        if worker.pid is not None and worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
    reopened = WorkflowStore(store.path)
    assert reopened.get("goal", "G") == original_goal
    assert reopened.get_review_attestation("G", 1) is None
    assert reopened.get("strict_review_record", "G") is None
    assert reopened.get("strict_review_head", "G") is None
    assert reopened.list("strict_review_chain", "G") == ()
    assert reopened.get_event("review-crash") is None
    assert reopened.get_event("attested-crash") is None
    assert [entry["event_id"] for entry in reopened.audit()] == ["seed"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM strict_review_attestations").fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
