from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.workflow_cli import _read_database
from goalanchor.workflow_protocol import fingerprint as protocol_fingerprint
from goalanchor.workflow_store import WorkflowStore, WorkflowStoreConflict

_NOW = "2026-10-16T01:00:00+08:00"
_LATER = "2026-10-20T01:00:00+08:00"


def create_v1(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE records (kind TEXT, key TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,key))")
        db.execute(
            "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('legacy-kind','row-1','{\"kept\":true}')"
        )
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_proofs','legacy-row','{\"arbitrary\":true}')"
        )
        db.execute("INSERT INTO events(event_id,payload) VALUES('legacy-event','{\"type\":\"kept\"}')")
        db.execute("PRAGMA user_version=1")


def schema_version(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        return int(db.execute("PRAGMA user_version").fetchone()[0])
    finally:
        db.close()


def journal_mode(path: Path) -> str:
    db = sqlite3.connect(path)
    try:
        return str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        db.close()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def strict_rows(path: Path):
    with sqlite3.connect(path) as db:
        return tuple(
            tuple(db.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall())
            for table in ("records", "events", "strict_proofs", "strict_heads")
        )


def baseline_data():
    return {
        "goal_id": "goal-1",
        "revision": 1,
        "objective": "Persisted test baseline",
        "plan": "Verify the baseline proof",
        "criteria": [
            {
                "criterion_id": "artifact",
                "description": "artifact exists",
                "expected": "done",
                "object_ref": "artifact.txt",
            }
        ],
        "constraints": [],
        "required_operations": [],
    }


def baseline_binding(baseline, runtime_digest: str):
    return {
        "goal_id": baseline["goal_id"],
        "baseline_digest": WorkflowStore.fingerprint(baseline),
        "runtime_binding_digest": runtime_digest,
        "plan_revision": baseline["revision"],
        "objective": baseline["objective"],
        "criteria_digest": protocol_fingerprint(baseline["criteria"]),
    }


def seed_v2_proof(path: Path) -> str:
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("BEGIN IMMEDIATE")
        baseline = baseline_data()
        baseline_digest = WorkflowStore.fingerprint(baseline)
        event = {
            "event_id": "proof-event",
            "kind": "user.baseline",
            "source": "trusted-ui",
            "subject": baseline_digest,
            "payload": baseline,
            "created_at": _NOW,
            "signature": "a" * 64,
        }
        cursor = db.execute(
            "INSERT INTO events(event_id,payload) VALUES(?,?)",
            (event["event_id"], json.dumps(event, sort_keys=True, separators=(",", ":"))),
        )
        source_seq = int(cursor.lastrowid)
        trusted_proof = {
            "actor_type": "USER",
            "principal_id": "principal",
            "account_id": "account",
            "session_id": "session",
            "source_event_id": "proof-event",
            "purpose": "user.baseline",
            "event_kind": event["kind"],
            "subject": event["subject"],
            "binding_digest": WorkflowStore.fingerprint(
                baseline_binding(baseline, "runtime-digest")
            ),
            "expires_at": _LATER,
            "nonce": "nonce-1",
        }
        trusted_proof["proof_digest"] = hashlib.sha256(b"proof").hexdigest()
        values = {
            "proof_id": "",
            "kind": "baseline",
            "goal_id": "goal-1",
            "scope_digest": "",
            "subject_id": "current",
            "source_event_id": "proof-event",
            "source_seq": source_seq,
            "source_event_digest": WorkflowStore.fingerprint(event),
            "proof_digest": trusted_proof["proof_digest"],
            "actor_type": "USER",
            "principal_id": "principal",
            "account_id": "account",
            "session_id": "session",
            "expires_at": _LATER,
            "policy_digest": "policy-digest",
            "runtime_binding_digest": "runtime-digest",
            "lease_generation": 1,
        }
        proof_payload = {
            **values,
            "baseline_digest": baseline_digest,
            "trusted_proof": trusted_proof,
            "payload": {
                "baseline": baseline,
                "goal_projection": {
                    "goal_id": "goal-1",
                    "revision": baseline["revision"],
                    "baseline": baseline,
                    "baseline_digest": baseline_digest,
                    "baseline_event_id": "proof-event",
                    "policy_digest": "policy-digest",
                    "runtime_binding_digest": "runtime-digest",
                    "trusted_identity": {
                        "principal_id": "principal",
                        "account_id": "account",
                        "session_id": "session",
                        "source_event_id": "proof-event",
                        "proof_digest": trusted_proof["proof_digest"],
                    }
                }
            },
        }
        values["proof_id"] = WorkflowStore.strict_proof_identifier(proof_payload)
        proof_payload["proof_id"] = values["proof_id"]
        proof_payload["payload"]["goal_projection"]["strict_baseline_proof_id"] = values["proof_id"]
        goal_projection = {
            **proof_payload["payload"]["goal_projection"],
            "generation": 1,
            "state": "PLAN",
            "baseline_created_at": event["created_at"],
            "supersedes": None,
        }
        db.execute(
            "INSERT INTO strict_proofs(" + ",".join(values) + ",payload) VALUES(" +
            ",".join("?" for _ in range(len(values) + 1)) + ")",
            (*values.values(), json.dumps(proof_payload, sort_keys=True, separators=(",", ":"))),
        )
        db.execute(
            "INSERT INTO strict_heads(kind,goal_id,scope_digest,subject_id,proof_id,source_seq,generation) "
            "VALUES('baseline','goal-1','','current',?,?,1)",
            (values["proof_id"], source_seq),
        )
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('baseline',?,?)",
            (baseline_digest, json.dumps(baseline, sort_keys=True, separators=(",", ":"))),
        )
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('goal','goal-1',?)",
            (json.dumps(goal_projection, sort_keys=True, separators=(",", ":")),),
        )
        db.commit()
        return values["proof_id"]
    finally:
        db.close()


def strict_commit_inputs(store: WorkflowStore, *, event_id: str = "strict-event", revision: int = 1):
    baseline = baseline_data()
    baseline["revision"] = revision
    baseline_digest = store.fingerprint(baseline)
    event = {
        "event_id": event_id,
        "kind": "user.baseline",
        "source": "trusted-ui",
        "subject": baseline_digest,
        "payload": baseline,
        "created_at": _NOW,
        "signature": "a" * 64,
    }
    proof_digest = hashlib.sha256(b"trusted proof").hexdigest()
    runtime_digest = "runtime-digest"
    binding = baseline_binding(baseline, runtime_digest)
    trusted_proof = {
        "actor_type": "USER",
        "principal_id": "principal",
        "account_id": "account",
        "session_id": "session",
        "source_event_id": event_id,
        "purpose": "user.baseline",
        "event_kind": event["kind"],
        "subject": event["subject"],
        "binding_digest": store.fingerprint(binding),
        "expires_at": _LATER,
        "nonce": "nonce-1",
        "proof_digest": proof_digest,
    }
    proof = {
        "kind": "baseline",
        "goal_id": "goal-1",
        "scope_digest": "",
        "subject_id": "current",
        "baseline_digest": baseline_digest,
        "source_event_id": event_id,
        "source_event_digest": store.fingerprint(event),
        "proof_digest": proof_digest,
        "actor_type": "USER",
        "principal_id": "principal",
        "account_id": "account",
        "session_id": "session",
        "expires_at": _LATER,
        "policy_digest": "policy-digest",
        "runtime_binding_digest": runtime_digest,
        "lease_generation": 1,
        "trusted_proof": trusted_proof,
        "payload": {
            "baseline": baseline,
            "goal_projection": {
                "goal_id": "goal-1",
                "revision": baseline["revision"],
                "baseline": baseline,
                "baseline_digest": baseline_digest,
                "baseline_event_id": event_id,
                "policy_digest": "policy-digest",
                "runtime_binding_digest": runtime_digest,
                "trusted_identity": {
                    "principal_id": "principal",
                    "account_id": "account",
                    "session_id": "session",
                    "source_event_id": event_id,
                    "proof_digest": proof_digest,
                }
            }
        },
    }
    proof_id = WorkflowStore.strict_proof_identifier(proof)
    proof["payload"]["goal_projection"]["strict_baseline_proof_id"] = proof_id

    @contextmanager
    def lease(expected_digest: str):
        yield {"binding_digest": expected_digest, "generation": 1}

    return event, proof, lease


_UNSET_GENERATION = object()


def commit_strict(
    store: WorkflowStore, event, proof, lease, *, expected=None, expected_head=None,
    candidate_generation=_UNSET_GENERATION,
):
    baseline = proof["payload"]["baseline"]
    try:
        old_goal = store.get("goal", proof["goal_id"])
    except (ContractValidationError, ValueError):
        old_goal = None
    goal_projection = {
        **proof["payload"]["goal_projection"],
        "generation": 0 if old_goal is None else old_goal.get("generation", 0),
        "state": "PLAN",
        "baseline_created_at": event["created_at"],
        "supersedes": None if old_goal is None else old_goal.get("baseline_digest"),
    }
    if candidate_generation is not _UNSET_GENERATION:
        goal_projection["generation"] = candidate_generation
    return store.commit_strict(
        [
            ("baseline", proof["baseline_digest"], baseline),
            ("goal", proof["goal_id"], goal_projection),
        ],
        {
            ("baseline", proof["baseline_digest"]): None,
            ("goal", proof["goal_id"]): None,
        } if expected is None else expected,
        proof["source_event_id"],
        event,
        proof,
        expected_head,
        lease,
        valid_until=_LATER,
        clock=lambda: _NOW,
    )


@pytest.mark.parametrize(
    ("kind", "payload", "malformed_attestation"),
    [
        ("baseline", None, False),
        ("baseline", {"goal_projection": {"trusted_identity": None}}, False),
        ("evidence", {"projection": {"trusted_source": None}}, False),
        ("evidence", {"projection": {"trusted_source": {}}}, True),
    ],
)
def test_strict_proof_replay_classifies_malformed_shapes_as_untrusted(kind, payload, malformed_attestation):
    common = {
        "proof_id": "proof-1",
        "source_seq": 1,
        "kind": kind,
        "goal_id": "goal-1",
        "scope_digest": "scope",
        "subject_id": "subject",
        "baseline_digest": "baseline",
        "source_event_id": "event-1",
        "source_event_digest": "event-digest",
        "actor_type": "USER",
        "principal_id": "principal",
        "account_id": "account",
        "session_id": "session",
        "policy_digest": "policy",
        "runtime_binding_digest": "runtime",
        "lease_generation": 1,
        "trusted_proof": {
            "actor_type": "USER",
            "principal_id": "principal",
            "account_id": "account",
            "session_id": "session",
            "source_event_id": "event-1",
            "purpose": "user.baseline" if kind == "baseline" else "observer.evidence",
            "event_kind": "user.baseline" if kind == "baseline" else "observer.evidence",
            "subject": "subject",
            "binding_digest": "binding",
        },
    }
    if malformed_attestation:
        common["trusted_proof"] = None
    saved = {**common, "payload": payload}
    fresh = {**common, "payload": json.loads(json.dumps(payload))}

    with pytest.raises(WorkflowStoreConflict) as captured:
        WorkflowStore._strict_proof_replay_matches(saved, fresh)

    assert captured.value.code == "UNTRUSTED"


@pytest.mark.parametrize(
    ("replacement", "case_name"),
    [(1.0, "int_to_float"), (True, "int_to_bool")],
)
def test_strict_proof_replay_rejects_nested_json_numeric_type_changes(
    tmp_path, replacement, case_name
):
    store = WorkflowStore(tmp_path / f"nested-json-type-{case_name}.db")
    _, proof, _ = strict_commit_inputs(store)
    saved = json.loads(json.dumps(proof))
    fresh = json.loads(json.dumps(proof))
    fresh["payload"]["goal_projection"]["revision"] = replacement

    assert WorkflowStore._strict_proof_replay_matches(saved, fresh) is False


def test_strict_snapshot_classifies_corrupt_proof_json_as_untrusted(tmp_path):
    path = tmp_path / "corrupt-proof.db"
    store = WorkflowStore(path)
    proof_id = seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER strict_proofs_no_update")
        db.execute("UPDATE strict_proofs SET payload=? WHERE proof_id=?", ("{", proof_id))

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


def test_strict_snapshot_rejects_coordinated_unbound_goal_projection_changes(tmp_path):
    path = tmp_path / "coordinated-goal-projection-tamper.db"
    store = WorkflowStore(path)
    proof_id = seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        proof_payload = json.loads(
            db.execute("SELECT payload FROM strict_proofs WHERE proof_id=?", (proof_id,)).fetchone()[0]
        )
        projection = proof_payload["payload"]["goal_projection"]
        projection["revision"] = 2
        projection["trusted_identity"]["principal_id"] = "unbound-principal"
        goal = json.loads(
            db.execute("SELECT payload FROM records WHERE kind='goal' AND key='goal-1'").fetchone()[0]
        )
        goal["revision"] = projection["revision"]
        goal["trusted_identity"] = projection["trusted_identity"]

        # Inject inconsistent state into an isolated test DB. This checks detection of
        # coordinated record/proof corruption, not protection from a database owner.
        db.execute("DROP TRIGGER strict_proofs_no_update")
        db.execute(
            "UPDATE strict_proofs SET payload=? WHERE proof_id=?",
            (json.dumps(proof_payload, sort_keys=True, separators=(",", ":")), proof_id),
        )
        db.execute(
            "CREATE TRIGGER strict_proofs_no_update BEFORE UPDATE ON strict_proofs "
            "BEGIN SELECT RAISE(ABORT,'strict proof history is append-only'); END"
        )
        db.execute(
            "UPDATE records SET payload=? WHERE kind='goal' AND key='goal-1'",
            (json.dumps(goal, sort_keys=True, separators=(",", ":")),),
        )

    before_read = strict_rows(path)

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"
    assert strict_rows(path) == before_read


def test_strict_snapshot_rejects_unknown_persisted_proof_fields(tmp_path):
    path = tmp_path / "unknown-proof-field.db"
    store = WorkflowStore(path)
    proof_id = seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        payload = json.loads(
            db.execute("SELECT payload FROM strict_proofs WHERE proof_id=?", (proof_id,)).fetchone()[0]
        )
        payload["unrecognized"] = "not part of the strict proof contract"
        db.execute("DROP TRIGGER strict_proofs_no_update")
        db.execute(
            "UPDATE strict_proofs SET payload=? WHERE proof_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), proof_id),
        )

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


@pytest.mark.parametrize("projection", ["goal", "baseline", "evidence"])
def test_strict_snapshot_classifies_corrupt_projection_json_as_untrusted(tmp_path, projection):
    path = tmp_path / f"corrupt-{projection}.db"
    store = WorkflowStore(path)
    if projection == "goal":
        kind, key = "goal", "goal-1"
        arguments = {}
    elif projection == "baseline":
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO records(kind,key,payload) VALUES('goal','goal-1',?)",
                (json.dumps({"goal_id": "goal-1", "baseline_digest": "baseline-digest"}),),
            )
        kind, key = "baseline", "baseline-digest"
        arguments = {}
    else:
        kind, key = "evidence", "goal-1:evidence-1"
        arguments = {"candidate_evidence_id": "evidence-1"}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO records(kind,key,payload) VALUES(?,?,?)", (kind, key, "{"))

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1", **arguments)

    assert captured.value.code == "UNTRUSTED"


def test_strict_snapshot_classifies_corrupt_source_event_json_as_untrusted(tmp_path):
    path = tmp_path / "corrupt-source-event.db"
    store = WorkflowStore(path)
    seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET payload=? WHERE event_id=?", ("{", "proof-event"))

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


@pytest.mark.parametrize("field", ["event_id", "subject"])
def test_strict_snapshot_binds_source_event_identity_and_content(tmp_path, field):
    path = tmp_path / f"source-event-{field}.db"
    store = WorkflowStore(path)
    seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        event = json.loads(db.execute("SELECT payload FROM events WHERE event_id=?", ("proof-event",)).fetchone()[0])
        event[field] = "different-value"
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (json.dumps(event, sort_keys=True, separators=(",", ":")), "proof-event"),
        )

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


def test_strict_snapshot_rejects_a_noncanonical_proof_id(tmp_path):
    path = tmp_path / "noncanonical-proof-id.db"
    store = WorkflowStore(path)
    seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        proof_id, payload = db.execute(
            "SELECT proof_id,payload FROM strict_proofs WHERE source_event_id=?", ("proof-event",)
        ).fetchone()
        proof_payload = json.loads(payload)
        proof_payload["proof_id"] = "noncanonical-proof"
        db.execute("DROP TRIGGER strict_proofs_no_update")
        db.execute("DROP TRIGGER strict_heads_monotonic")
        db.execute(
            "UPDATE strict_proofs SET proof_id=?,payload=? WHERE proof_id=?",
            ("noncanonical-proof", json.dumps(proof_payload, sort_keys=True, separators=(",", ":")), proof_id),
        )
        db.execute("UPDATE strict_heads SET proof_id=? WHERE proof_id=?", ("noncanonical-proof", proof_id))
        goal_raw = db.execute("SELECT payload FROM records WHERE kind='goal' AND key='goal-1'").fetchone()[0]
        goal = json.loads(goal_raw)
        goal["strict_baseline_proof_id"] = "noncanonical-proof"
        db.execute(
            "UPDATE records SET payload=? WHERE kind='goal' AND key='goal-1'",
            (json.dumps(goal, sort_keys=True, separators=(",", ":")),),
        )

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


def test_strict_snapshot_rejects_missing_proof_behind_baseline_head(tmp_path):
    path = tmp_path / "missing-baseline-proof.db"
    store = WorkflowStore(path)
    proof_id = seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TRIGGER strict_proofs_no_delete")
        db.execute("DELETE FROM strict_proofs WHERE proof_id=?", (proof_id,))

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


def test_strict_snapshot_rejects_goal_proof_pointer_without_baseline_head(tmp_path):
    path = tmp_path / "missing-baseline-head.db"
    store = WorkflowStore(path)
    proof_id = seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TRIGGER strict_heads_no_delete")
        db.execute("DELETE FROM strict_heads WHERE proof_id=?", (proof_id,))

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("source_seq", True), ("source_seq", 1.0), ("lease_generation", True)],
)
def test_strict_snapshot_rejects_json_metadata_with_wrong_numeric_type(tmp_path, field, replacement):
    path = tmp_path / f"wrong-json-type-{field}-{type(replacement).__name__}.db"
    store = WorkflowStore(path)
    proof_id = seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        raw = db.execute("SELECT payload FROM strict_proofs WHERE proof_id=?", (proof_id,)).fetchone()[0]
        payload = json.loads(raw)
        payload[field] = replacement
        db.execute("DROP TRIGGER strict_proofs_no_update")
        db.execute(
            "UPDATE strict_proofs SET payload=? WHERE proof_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), proof_id),
        )

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


@pytest.mark.parametrize(
    ("table", "column", "trigger", "lookup", "value"),
    [
        ("strict_proofs", "source_seq", "strict_proofs_no_update", "proof_id", 1.5),
        ("strict_proofs", "lease_generation", "strict_proofs_no_update", "proof_id", 1.5),
        ("strict_heads", "source_seq", "strict_heads_monotonic", "proof_id", 1.5),
        ("strict_heads", "generation", "strict_heads_monotonic", "proof_id", 1.5),
    ],
)
def test_strict_snapshot_rejects_sql_numeric_type_corruption(tmp_path, table, column, trigger, lookup, value):
    path = tmp_path / f"wrong-sql-type-{table}-{column}.db"
    store = WorkflowStore(path)
    proof_id = seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(f"DROP TRIGGER {trigger}")
        db.execute(f"UPDATE {table} SET {column}=? WHERE {lookup}=?", (value, proof_id))

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("baseline_digest", "0" * 64),
        ("trusted_purpose", "observer.evidence"),
        ("binding_digest", "0" * 64),
    ],
)
def test_strict_snapshot_recomputes_provider_binding_and_baseline_identity(
    tmp_path, field, replacement
):
    path = tmp_path / f"bad-binding-{field}.db"
    store = WorkflowStore(path)
    proof_id = seed_v2_proof(path)
    with sqlite3.connect(path) as db:
        raw = db.execute("SELECT payload FROM strict_proofs WHERE proof_id=?", (proof_id,)).fetchone()[0]
        payload = json.loads(raw)
        if field == "baseline_digest":
            payload[field] = replacement
        elif field == "trusted_purpose":
            payload["trusted_proof"]["purpose"] = replacement
        else:
            payload["trusted_proof"][field] = replacement
        db.execute("DROP TRIGGER strict_proofs_no_update")
        db.execute(
            "UPDATE strict_proofs SET payload=? WHERE proof_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), proof_id),
        )

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"


def test_strict_snapshot_rejects_a_matching_noncanonical_digest_when_source_event_differs(tmp_path):
    path = tmp_path / "source-digest-mismatch.db"
    store = WorkflowStore(path)
    event, proof, lease = strict_commit_inputs(store)
    assert commit_strict(store, event, proof, lease)[0]
    with sqlite3.connect(path) as db:
        proof_id, payload = db.execute(
            "SELECT proof_id,payload FROM strict_proofs WHERE source_event_id=?", (event["event_id"],)
        ).fetchone()
        proof_payload = json.loads(payload)
        proof_payload["source_event_digest"] = "0" * 64
        new_proof_id = WorkflowStore.strict_proof_identifier(proof_payload)
        proof_payload["proof_id"] = new_proof_id
        db.execute("DROP TRIGGER strict_proofs_no_update")
        db.execute("DROP TRIGGER strict_heads_monotonic")
        db.execute(
            "UPDATE strict_proofs SET proof_id=?,source_event_digest=?,payload=? WHERE proof_id=?",
            (new_proof_id, "0" * 64,
             json.dumps(proof_payload, sort_keys=True, separators=(",", ":")), proof_id),
        )
        db.execute("UPDATE strict_heads SET proof_id=? WHERE proof_id=?", (new_proof_id, proof_id))
        goal = json.loads(
            db.execute("SELECT payload FROM records WHERE kind='goal' AND key='goal-1'").fetchone()[0]
        )
        goal["strict_baseline_proof_id"] = new_proof_id
        db.execute(
            "UPDATE records SET payload=? WHERE kind='goal' AND key='goal-1'",
            (json.dumps(goal, sort_keys=True, separators=(",", ":")),),
        )
    before = strict_rows(path)

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.read_strict_snapshot("goal-1")

    assert captured.value.code == "UNTRUSTED"
    with pytest.raises(WorkflowStoreConflict) as replay_error:
        commit_strict(store, event, proof, lease)
    assert replay_error.value.code == "UNTRUSTED"
    assert strict_rows(path) == before


def test_exact_replay_rejects_changed_source_event_identity_without_writes(tmp_path):
    path = tmp_path / "changed-source-event-replay.db"
    store = WorkflowStore(path)
    event, proof, lease = strict_commit_inputs(store)
    assert commit_strict(store, event, proof, lease)[0]
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (json.dumps({**event, "subject": "changed"}, sort_keys=True, separators=(",", ":")),
             event["event_id"]),
        )
    before = strict_rows(path)

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(store, event, proof, lease)

    assert captured.value.code == "UNTRUSTED"
    assert strict_rows(path) == before


def write_next_baseline_proof(store, event_id: str):
    current = store.read_strict_snapshot("goal-1")
    current_revision = current["baseline_proof"]["payload"]["baseline"]["revision"]
    event, proof, lease = strict_commit_inputs(
        store, event_id=event_id, revision=current_revision + 1
    )
    expected = {
        ("baseline", proof["baseline_digest"]): None,
        ("goal", proof["goal_id"]): store.fingerprint(store.get("goal", proof["goal_id"])),
    }
    inserted, _ = commit_strict(
        store,
        event,
        proof,
        lease,
        expected=expected,
        expected_head=current["baseline_head"],
    )
    return event, proof, lease, inserted


def test_exact_replay_classifies_valid_superseded_proof_as_stale(tmp_path):
    store = WorkflowStore(tmp_path / "superseded-replay.db")
    first_event, first_proof, first_lease = strict_commit_inputs(store, event_id="baseline-1")
    assert commit_strict(store, first_event, first_proof, first_lease)[0]
    _, _, _, inserted = write_next_baseline_proof(store, "baseline-2")
    assert inserted
    before = strict_rows(Path(store.path))

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(store, first_event, first_proof, first_lease, expected={})

    assert captured.value.code == "STALE"
    assert strict_rows(Path(store.path)) == before


def test_exact_replay_rejects_head_rollback_behind_newer_proof_as_untrusted(tmp_path):
    path = tmp_path / "rolled-back-head.db"
    store = WorkflowStore(path)
    first_event, first_proof, first_lease = strict_commit_inputs(store, event_id="baseline-1")
    assert commit_strict(store, first_event, first_proof, first_lease)[0]
    second_event, second_proof, _, inserted = write_next_baseline_proof(store, "baseline-2")
    assert inserted
    with sqlite3.connect(path) as db:
        first_seq = db.execute("SELECT seq FROM events WHERE event_id=?", (first_event["event_id"],)).fetchone()[0]
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TRIGGER strict_heads_monotonic")
        first_proof_id = WorkflowStore.strict_proof_identifier(first_proof)
        db.execute(
            "UPDATE strict_heads SET proof_id=?,source_seq=?,generation=1 WHERE kind='baseline' AND goal_id='goal-1'",
            (first_proof_id, first_seq),
        )
        goal = json.loads(
            db.execute("SELECT payload FROM records WHERE kind='goal' AND key='goal-1'").fetchone()[0]
        )
        goal["strict_baseline_proof_id"] = first_proof_id
        db.execute(
            "UPDATE records SET payload=? WHERE kind='goal' AND key='goal-1'",
            (json.dumps(goal, sort_keys=True, separators=(",", ":")),),
        )
    before = strict_rows(path)

    with pytest.raises(WorkflowStoreConflict) as snapshot_error:
        store.read_strict_snapshot("goal-1")
    assert snapshot_error.value.code == "UNTRUSTED"
    with pytest.raises(WorkflowStoreConflict) as replay_error:
        commit_strict(store, first_event, first_proof, first_lease, expected={})
    assert replay_error.value.code == "UNTRUSTED"
    assert strict_rows(path) == before
    assert second_proof["source_event_id"] == second_event["event_id"]


def test_exact_replay_reports_missing_persisted_proof_as_untrusted(tmp_path):
    path = tmp_path / "missing-proof-replay.db"
    store = WorkflowStore(path)
    event, proof, lease = strict_commit_inputs(store)
    assert commit_strict(store, event, proof, lease)[0]
    proof_id = WorkflowStore.strict_proof_identifier(proof)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TRIGGER strict_proofs_no_delete")
        db.execute("DELETE FROM strict_proofs WHERE proof_id=?", (proof_id,))
    before = strict_rows(path)
    changed = json.loads(json.dumps(proof))
    changed["principal_id"] = "new-principal"
    changed["trusted_proof"]["principal_id"] = "new-principal"
    changed["payload"]["goal_projection"]["trusted_identity"]["principal_id"] = "new-principal"
    changed["payload"]["goal_projection"]["strict_baseline_proof_id"] = (
        WorkflowStore.strict_proof_identifier(changed)
    )

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(store, event, changed, lease, expected={})

    assert captured.value.code == "UNTRUSTED"
    assert strict_rows(path) == before


def test_exact_replay_with_well_formed_changed_identity_is_stale(tmp_path):
    store = WorkflowStore(tmp_path / "changed-replay-identity.db")
    event, proof, lease = strict_commit_inputs(store)
    assert commit_strict(store, event, proof, lease)[0]
    changed = json.loads(json.dumps(proof))
    changed["principal_id"] = "new-principal"
    changed["trusted_proof"]["principal_id"] = "new-principal"
    changed["payload"]["goal_projection"]["trusted_identity"]["principal_id"] = "new-principal"
    changed["payload"]["goal_projection"]["strict_baseline_proof_id"] = (
        WorkflowStore.strict_proof_identifier(changed)
    )
    before = strict_rows(Path(store.path))

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(store, event, changed, lease, expected={})

    assert captured.value.code == "STALE"
    assert strict_rows(Path(store.path)) == before


@pytest.mark.parametrize("lock_order", ["replay_first", "writer_first"])
def test_exact_replay_and_competing_writer_are_serialized(tmp_path, lock_order):
    store = WorkflowStore(tmp_path / f"replay-race-{lock_order}.db")
    first_event, first_proof, first_lease = strict_commit_inputs(store, event_id="baseline-1")
    assert commit_strict(store, first_event, first_proof, first_lease)[0]
    second_event, second_proof, second_lease = strict_commit_inputs(
        store, event_id="baseline-2", revision=2
    )
    current = store.read_strict_snapshot("goal-1")
    expected = {
        ("baseline", second_proof["baseline_digest"]): None,
        ("goal", "goal-1"): store.fingerprint(store.get("goal", "goal-1")),
    }
    current_goal = store.get("goal", "goal-1")
    next_goal = {
        **second_proof["payload"]["goal_projection"],
        "generation": current_goal["generation"],
        "state": "PLAN",
        "baseline_created_at": second_event["created_at"],
        "supersedes": current_goal["baseline_digest"],
    }
    replay_lock = threading.Event()
    writer_lock = threading.Event()
    release = threading.Event()
    replay_result: list[Any] = []
    writer_result: list[Any] = []
    replay_done = threading.Event()
    writer_done = threading.Event()

    if lock_order == "replay_first":
        original_match = WorkflowStore._strict_proof_replay_matches

        def pause_exact_replay(saved, fresh, event=None):
            if threading.current_thread().name == "strict-replay":
                replay_lock.set()
                if not release.wait(3):
                    raise TimeoutError("replay test barrier timed out")
            return original_match(saved, fresh, event)

        store._strict_proof_replay_matches = pause_exact_replay

        def replay():
            try:
                replay_result.append(commit_strict(store, first_event, first_proof, first_lease, expected={}))
            except BaseException as exc:
                replay_result.append(exc)
            finally:
                replay_done.set()

        def writer():
            writer_lock.set()
            try:
                writer_result.append(
                    commit_strict(
                        store, second_event, second_proof, second_lease,
                        expected=expected, expected_head=current["baseline_head"],
                    )
                )
            except BaseException as exc:
                writer_result.append(exc)
            finally:
                writer_done.set()

        replay_thread = threading.Thread(target=replay, name="strict-replay")
        writer_thread = threading.Thread(target=writer, name="strict-writer")
        replay_thread.start()
        assert replay_lock.wait(2)
        writer_thread.start()
        assert writer_lock.wait(2)
        assert not writer_done.wait(0.2)
        release.set()
    else:
        @contextmanager
        def paused_writer_lease(expected_digest: str):
            with second_lease(expected_digest) as lease:
                writer_lock.set()
                if not release.wait(3):
                    raise TimeoutError("writer test barrier timed out")
                yield lease

        def writer():
            try:
                writer_result.append(
                    store.commit_strict(
                        [
                            ("baseline", second_proof["baseline_digest"], second_proof["payload"]["baseline"]),
                                ("goal", "goal-1", next_goal),
                        ],
                        expected,
                        second_event["event_id"],
                        second_event,
                        second_proof,
                        current["baseline_head"],
                        paused_writer_lease,
                        valid_until=_LATER,
                        clock=lambda: _NOW,
                    )
                )
            except BaseException as exc:
                writer_result.append(exc)
            finally:
                writer_done.set()

        def replay():
            replay_lock.set()
            try:
                replay_result.append(commit_strict(store, first_event, first_proof, first_lease, expected={}))
            except BaseException as exc:
                replay_result.append(exc)
            finally:
                replay_done.set()

        replay_thread = threading.Thread(target=replay, name="strict-replay")
        writer_thread = threading.Thread(target=writer, name="strict-writer")
        writer_thread.start()
        assert writer_lock.wait(2)
        replay_thread.start()
        assert replay_lock.wait(2)
        assert not replay_done.wait(0.2)
        release.set()

    writer_thread.join(timeout=4)
    replay_thread.join(timeout=4)
    assert writer_done.is_set()
    assert replay_done.is_set()
    assert len(writer_result) == 1 and not isinstance(writer_result[0], BaseException)
    if lock_order == "replay_first":
        assert len(replay_result) == 1 and replay_result[0][0] is False
        assert writer_result[0][0] is True
    else:
        assert len(replay_result) == 1 and isinstance(replay_result[0], WorkflowStoreConflict)
        assert replay_result[0].code == "STALE"
        assert writer_result[0][0] is True
    assert [row["event_id"] for row in store.audit()] == ["baseline-1", "baseline-2"]


def test_direct_strict_store_commit_rejects_equal_baseline_revision_without_writes(tmp_path):
    store = WorkflowStore(tmp_path / "equal-baseline-revision.db")
    first_event, first_proof, first_lease = strict_commit_inputs(store, event_id="baseline-first")
    assert commit_strict(store, first_event, first_proof, first_lease)[0]
    current = store.read_strict_snapshot("goal-1")
    second_event, second_proof, second_lease = strict_commit_inputs(store, event_id="baseline-equal")
    old_goal = store.get("goal", "goal-1")
    expected = {
        ("baseline", second_proof["baseline_digest"]): store.fingerprint(
            store.get("baseline", second_proof["baseline_digest"])
        ),
        ("goal", "goal-1"): store.fingerprint(old_goal),
    }
    before = strict_rows(Path(store.path))

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(
            store, second_event, second_proof, second_lease,
            expected=expected, expected_head=current["baseline_head"],
        )

    assert captured.value.code == "STALE"
    assert strict_rows(Path(store.path)) == before


def test_direct_strict_store_commit_rejects_nonzero_generation_for_new_goal(tmp_path):
    store = WorkflowStore(tmp_path / "new-goal-generation.db")
    event, proof, lease = strict_commit_inputs(store, event_id="new-goal-generation")
    before = strict_rows(Path(store.path))

    with pytest.raises(WorkflowStoreConflict):
        commit_strict(
            store, event, proof, lease, candidate_generation=1,
        )

    assert strict_rows(Path(store.path)) == before


@pytest.mark.parametrize("generation_delta", [-1, 1])
def test_direct_strict_store_commit_rejects_generation_mismatch_without_writes(tmp_path, generation_delta):
    store = WorkflowStore(tmp_path / f"generation-mismatch-{generation_delta}.db")
    first_event, first_proof, first_lease = strict_commit_inputs(
        store, event_id="generation-first-baseline"
    )
    assert commit_strict(store, first_event, first_proof, first_lease)[0]
    current = store.read_strict_snapshot("goal-1")
    old_goal = store.get("goal", "goal-1")
    assert old_goal is not None
    candidate_event, candidate_proof, candidate_lease = strict_commit_inputs(
        store, event_id=f"generation-candidate-{generation_delta}", revision=2
    )
    expected = {
        ("baseline", candidate_proof["baseline_digest"]): None,
        ("goal", "goal-1"): store.fingerprint(old_goal),
    }
    before = strict_rows(Path(store.path))

    with pytest.raises(WorkflowStoreConflict):
        commit_strict(
            store,
            candidate_event,
            candidate_proof,
            candidate_lease,
            expected=expected,
            expected_head=current["baseline_head"],
            candidate_generation=old_goal["generation"] + generation_delta,
        )

    assert strict_rows(Path(store.path)) == before


@pytest.mark.parametrize("legacy_generation", [True, 1.0, -1])
def test_direct_strict_store_commit_rejects_malformed_legacy_generation_without_writes(
    tmp_path, legacy_generation
):
    store = WorkflowStore(tmp_path / f"legacy-generation-{legacy_generation}.db")
    old_baseline = baseline_data()
    old_digest = store.fingerprint(old_baseline)
    legacy_goal = {
        "goal_id": "goal-1",
        "state": "PLAN",
        "revision": 1,
        "baseline": old_baseline,
        "baseline_digest": old_digest,
        "generation": legacy_generation,
    }
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('baseline',?,?)",
            (old_digest, json.dumps(old_baseline, sort_keys=True, separators=(",", ":"))),
        )
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('goal','goal-1',?)",
            (json.dumps(legacy_goal, sort_keys=True, separators=(",", ":")),),
        )
    event, proof, lease = strict_commit_inputs(
        store, event_id=f"legacy-generation-{legacy_generation}", revision=2
    )
    expected = {
        ("baseline", proof["baseline_digest"]): None,
        ("goal", "goal-1"): store.fingerprint(legacy_goal),
    }
    before = strict_rows(Path(store.path))

    with pytest.raises(WorkflowStoreConflict):
        commit_strict(
            store,
            event,
            proof,
            lease,
            expected=expected,
            expected_head=None,
            candidate_generation=0,
        )

    assert strict_rows(Path(store.path)) == before


def test_direct_strict_store_commit_defaults_missing_legacy_generation_to_zero(tmp_path):
    store = WorkflowStore(tmp_path / "legacy-generation-default.db")
    old_baseline = baseline_data()
    old_digest = store.fingerprint(old_baseline)
    legacy_goal = {
        "goal_id": "goal-1",
        "state": "PLAN",
        "revision": 1,
        "baseline": old_baseline,
        "baseline_digest": old_digest,
    }
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('baseline',?,?)",
            (old_digest, json.dumps(old_baseline, sort_keys=True, separators=(",", ":"))),
        )
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('goal','goal-1',?)",
            (json.dumps(legacy_goal, sort_keys=True, separators=(",", ":")),),
        )
    event, proof, lease = strict_commit_inputs(
        store, event_id="legacy-generation-default", revision=2
    )
    expected = {
        ("baseline", proof["baseline_digest"]): None,
        ("goal", "goal-1"): store.fingerprint(legacy_goal),
    }

    committed, _ = commit_strict(
        store,
        event,
        proof,
        lease,
        expected=expected,
        expected_head=None,
        candidate_generation=0,
    )

    assert committed
    assert store.get("goal", "goal-1")["generation"] == 1


def test_strict_commit_rejects_malformed_attestation_before_first_write(tmp_path):
    store = WorkflowStore(tmp_path / "malformed-attestation.db")
    event, proof, lease = strict_commit_inputs(store)
    proof["trusted_proof"] = None

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(store, event, proof, lease)

    assert captured.value.code == "UNTRUSTED"
    assert store.audit() == ()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM strict_proofs").fetchone()[0] == 0


@pytest.mark.parametrize("invalid_generation", [True, 1.0])
def test_strict_commit_rejects_noninteger_runtime_lease_generation(tmp_path, invalid_generation):
    store = WorkflowStore(tmp_path / f"invalid-lease-generation-{invalid_generation}.db")
    event, proof, _ = strict_commit_inputs(store)

    @contextmanager
    def malformed_lease(expected_digest: str):
        yield {"binding_digest": expected_digest, "generation": invalid_generation}

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(store, event, proof, malformed_lease)

    assert captured.value.code == "UNTRUSTED"
    assert store.audit() == ()
    assert strict_rows(Path(store.path)) == ((), (), (), ())


def test_strict_commit_rejects_malformed_persisted_attestation_on_replay(tmp_path):
    store = WorkflowStore(tmp_path / "malformed-replay-attestation.db")
    event, proof, lease = strict_commit_inputs(store)
    inserted, stored = commit_strict(store, event, proof, lease)
    assert inserted
    malformed_payload = dict(stored)
    malformed_payload["trusted_proof"] = None
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER strict_proofs_no_update")
        db.execute(
            "UPDATE strict_proofs SET payload=? WHERE proof_id=?",
            (json.dumps(malformed_payload, sort_keys=True, separators=(",", ":")), stored["proof_id"]),
        )
    before = (store.audit(), strict_rows(Path(store.path)))

    with pytest.raises(WorkflowStoreConflict) as snapshot_error:
        store.read_strict_snapshot("goal-1")

    assert snapshot_error.value.code == "UNTRUSTED"

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(store, event, proof, lease)

    assert captured.value.code == "UNTRUSTED"
    assert (store.audit(), strict_rows(Path(store.path))) == before


def test_strict_commit_classifies_well_formed_replay_mismatch_as_stale(tmp_path):
    store = WorkflowStore(tmp_path / "stale-replay-attestation.db")
    event, proof, lease = strict_commit_inputs(store)
    assert commit_strict(store, event, proof, lease)[0]
    changed = json.loads(json.dumps(proof))
    changed["principal_id"] = "other-principal"
    changed["trusted_proof"]["principal_id"] = "other-principal"
    changed["payload"]["goal_projection"]["trusted_identity"]["principal_id"] = "other-principal"
    changed["payload"]["goal_projection"]["strict_baseline_proof_id"] = (
        WorkflowStore.strict_proof_identifier(changed)
    )
    before = (store.audit(), store.read_strict_snapshot("goal-1"))

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(store, event, changed, lease)

    assert captured.value.code == "STALE"
    assert (store.audit(), store.read_strict_snapshot("goal-1")) == before


def test_strict_concurrent_event_replay_cannot_extend_saved_proof_expiry(tmp_path):
    store = WorkflowStore(tmp_path / "shorter-replay-expiry.db")
    event, proof, lease = strict_commit_inputs(store)
    assert commit_strict(store, event, proof, lease)[0]
    fresh = json.loads(json.dumps(proof))
    fresh_expiry = "2026-10-19T01:00:00+08:00"
    fresh["expires_at"] = fresh_expiry
    fresh["trusted_proof"]["expires_at"] = fresh_expiry
    fresh["trusted_proof"]["nonce"] = "refreshed-nonce"
    fresh["trusted_proof"]["proof_digest"] = hashlib.sha256(b"refreshed proof").hexdigest()
    fresh["proof_digest"] = fresh["trusted_proof"]["proof_digest"]
    before = strict_rows(Path(store.path))

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(store, event, fresh, lease, expected={})

    assert captured.value.code == "STALE"
    assert strict_rows(Path(store.path)) == before


@pytest.mark.parametrize("projection_set", ["empty", "baseline_only", "goal_only"])
def test_strict_baseline_commit_requires_complete_projection_set_before_writes(tmp_path, projection_set):
    store = WorkflowStore(tmp_path / f"incomplete-baseline-{projection_set}.db")
    event, proof, lease = strict_commit_inputs(store)
    baseline = proof["payload"]["baseline"]
    goal = {**proof["payload"]["goal_projection"], "generation": 0, "state": "PLAN"}
    if projection_set == "empty":
        changes = []
        expected = {("goal", "goal-1"): None}
    elif projection_set == "baseline_only":
        changes = [("baseline", proof["baseline_digest"], baseline)]
        expected = {("baseline", proof["baseline_digest"]): None, ("goal", "goal-1"): None}
    else:
        changes = [("goal", "goal-1", goal)]
        expected = {("goal", "goal-1"): None}

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.commit_strict(
            changes, expected, event["event_id"], event, proof, None, lease,
            valid_until=_LATER, clock=lambda: _NOW,
        )

    assert captured.value.code == "UNTRUSTED"
    assert store.audit() == ()
    assert strict_rows(Path(store.path)) == ((), (), (), ())


def test_strict_baseline_commit_rejects_goal_projection_closed_state_before_writes(tmp_path):
    store = WorkflowStore(tmp_path / "closed-baseline-projection.db")
    event, proof, lease = strict_commit_inputs(store)
    baseline = proof["payload"]["baseline"]
    goal = {**proof["payload"]["goal_projection"], "generation": 0, "state": "CLOSED"}
    expected = {
        ("baseline", proof["baseline_digest"]): None,
        ("goal", "goal-1"): None,
    }

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.commit_strict(
            [
                ("baseline", proof["baseline_digest"], baseline),
                ("goal", "goal-1", goal),
            ],
            expected,
            event["event_id"],
            event,
            proof,
            None,
            lease,
            valid_until=_LATER,
            clock=lambda: _NOW,
        )

    assert captured.value.code == "UNTRUSTED"
    assert store.audit() == ()
    assert strict_rows(Path(store.path)) == ((), (), (), ())


def test_strict_commit_requires_goal_cas_before_new_writes_but_exact_replay_is_read_only(tmp_path):
    store = WorkflowStore(tmp_path / "goal-cas.db")
    event, proof, lease = strict_commit_inputs(store)

    with pytest.raises(ContractValidationError, match="affected goal requires an expected version"):
        commit_strict(store, event, proof, lease, expected={})

    assert store.audit() == ()
    inserted, _ = commit_strict(store, event, proof, lease)
    assert inserted
    before = (store.audit(), store.read_strict_snapshot("goal-1"))

    replayed, stored = commit_strict(store, event, proof, lease, expected={})

    assert not replayed
    assert stored["proof_id"] == store.read_strict_snapshot("goal-1")["baseline_proof"]["proof_id"]
    assert (store.audit(), store.read_strict_snapshot("goal-1")) == before


@pytest.mark.parametrize(
    ("kind", "key", "payload"),
    [
        ("evidence", "goal-2:evidence-1", {"snapshot": {"evidence_id": "evidence-1"}}),
        ("goal", "goal-2", {"goal_id": "goal-2"}),
        ("goal", "goal-1", {}),
    ],
)
def test_strict_commit_rejects_unbound_or_cross_goal_projection_before_writes(
    tmp_path, kind, key, payload
):
    store = WorkflowStore(tmp_path / f"cross-goal-{kind}.db")
    event, proof, lease = strict_commit_inputs(store)
    expected = {("goal", "goal-1"): None, (kind, key): None}

    with pytest.raises(WorkflowStoreConflict) as captured:
        store.commit_strict(
            [(kind, key, payload)], expected, event["event_id"], event, proof, None, lease,
            valid_until=_LATER, clock=lambda: _NOW,
        )

    assert captured.value.code == "UNTRUSTED"
    assert store.audit() == ()
    assert strict_rows(Path(store.path)) == ((), (), (), ())


def test_strict_commit_decodes_expected_goal_with_untrusted_failure(tmp_path):
    path = tmp_path / "corrupt-expected-goal.db"
    store = WorkflowStore(path)
    event, proof, lease = strict_commit_inputs(store)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO records(kind,key,payload) VALUES('goal','goal-1','{')")

    with pytest.raises(WorkflowStoreConflict) as captured:
        commit_strict(
            store, event, proof, lease,
            expected={
                ("baseline", proof["baseline_digest"]): None,
                ("goal", "goal-1"): "0" * 64,
            },
        )

    assert captured.value.code == "UNTRUSTED"
    assert store.audit() == ()


def test_strict_goal_touch_uses_strict_json_decoder(tmp_path):
    path = tmp_path / "corrupt-touched-goal.db"
    store = WorkflowStore(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO records(kind,key,payload) VALUES('goal','goal-1','{')")
    with store._transaction() as db:
        with pytest.raises(WorkflowStoreConflict) as captured:
            store._strict_touch_goal(db, "goal-1")

    assert captured.value.code == "UNTRUSTED"


def test_strict_goal_touch_rejects_boolean_generation(tmp_path):
    path = tmp_path / "boolean-goal-generation.db"
    store = WorkflowStore(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('goal','goal-1',?)",
            (json.dumps({"goal_id": "goal-1", "generation": True}),),
        )

    with store._transaction() as db:
        with pytest.raises(WorkflowStoreConflict) as captured:
            store._strict_touch_goal(db, "goal-1")

    assert captured.value.code == "UNTRUSTED"
    assert store.get("goal", "goal-1")["generation"] is True


def test_fresh_database_initializes_v2_without_changing_default_journal(tmp_path):
    path = tmp_path / "fresh.db"

    store = WorkflowStore(path)

    assert schema_version(path) == 2
    assert journal_mode(path) == "delete"
    assert store.list("missing") == ()


def test_v1_migration_preserves_legacy_rows_events_and_journal(tmp_path):
    path = tmp_path / "legacy.db"
    create_v1(path)
    before_journal = journal_mode(path)

    store = WorkflowStore(path)

    assert schema_version(path) == 2
    assert journal_mode(path) == before_journal == "delete"
    assert store.get("legacy-kind", "row-1") == {"kept": True}
    assert store.get("strict_proofs", "legacy-row") == {"arbitrary": True}
    assert store.get_event("legacy-event") == {"type": "kept"}


def test_v1_ddl_failure_rolls_back_all_schema_data_and_journal_changes(tmp_path):
    path = tmp_path / "ddl-failure.db"
    create_v1(path)
    with sqlite3.connect(path) as db:
        # An unrelated legacy table name collides with the migration DDL.
        db.execute("CREATE TABLE strict_proofs (legacy TEXT)")
    before = file_digest(path)
    before_journal = journal_mode(path)

    with pytest.raises(ContractValidationError):
        WorkflowStore(path)

    assert file_digest(path) == before
    assert schema_version(path) == 1
    assert journal_mode(path) == before_journal == "delete"
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "strict_heads" not in tables
        assert db.execute("SELECT payload FROM records WHERE kind='legacy-kind'").fetchone()[0] == '{"kept":true}'


def test_unsupported_schema_is_rejected_without_database_or_journal_changes(tmp_path):
    path = tmp_path / "future.db"
    create_v1(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=3")
    before = file_digest(path)
    before_journal = journal_mode(path)

    with pytest.raises(ContractValidationError):
        WorkflowStore(path)

    assert file_digest(path) == before
    assert schema_version(path) == 3
    assert journal_mode(path) == before_journal == "delete"


def test_v2_reopen_is_idempotent_and_does_not_change_journal(tmp_path):
    path = tmp_path / "reopen.db"
    first = WorkflowStore(path)
    first.commit([("legacy-kind", "k", {"v": 1})], {("legacy-kind", "k"): None}, "evt", {"v": 1})
    assert schema_version(path) == 2
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    before_journal = journal_mode(path)

    second = WorkflowStore(path)

    assert schema_version(path) == 2
    assert journal_mode(path) == before_journal
    assert second.get("legacy-kind", "k") == {"v": 1}


def test_concurrent_v1_migrators_recheck_schema_under_the_write_lock(tmp_path):
    path = tmp_path / "race.db"
    create_v1(path)

    def open_store() -> WorkflowStore:
        return WorkflowStore(path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(lambda _: open_store(), range(2)))

    assert schema_version(path) == 2
    assert all(store.get("legacy-kind", "row-1") == {"kept": True} for store in stores)


def test_v2_read_only_cli_status_and_audit_do_not_write_delete_journal_database(tmp_path):
    path = tmp_path / "offline.db"
    store = WorkflowStore(path)
    store.commit([("goal", "g", {"goal_id": "g", "state": "PLAN"})],
                 {("goal", "g"): None}, "event-1", {"kind": "seed"})
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    before = file_digest(path)
    sidecars = {item.name for item in tmp_path.iterdir()}

    status = _read_database(path, "status")
    audit = _read_database(path, "audit")

    assert status["goals"] == [{"goal_id": "g", "state": "PLAN", "generation": 1}]
    assert audit["events"] == [{"seq": 1, "event_id": "event-1", "payload": {"kind": "seed"}}]
    assert file_digest(path) == before
    assert {item.name for item in tmp_path.iterdir()} == sidecars
    assert journal_mode(path) == "delete"


def test_v2_read_only_cli_keeps_wal_refusal(tmp_path):
    path = tmp_path / "wal.db"
    WorkflowStore(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")

    with pytest.raises(ContractValidationError, match="WAL databases are unsupported"):
        _read_database(path, "status")


def test_v2_schema_enforces_deferred_foreign_keys(tmp_path):
    path = tmp_path / "foreign-keys.db"
    WorkflowStore(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        db.execute("BEGIN")
        db.execute(
            "INSERT INTO strict_heads(kind,goal_id,scope_digest,subject_id,proof_id,source_seq,generation) "
            "VALUES('evidence','g','b','e','missing-proof',1,1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.commit()
        db.rollback()


def test_every_store_writer_enables_foreign_keys_before_begin(tmp_path):
    store = WorkflowStore(tmp_path / "writer-fk.db")

    with store._transaction() as db:
        assert db.in_transaction
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE events SET payload='{}' WHERE event_id='proof-event'",
        "DELETE FROM events WHERE event_id='proof-event'",
        "INSERT OR REPLACE INTO events(event_id,payload) VALUES('proof-event','{}')",
        "INSERT OR REPLACE INTO events(seq,event_id,payload) VALUES(1,'replacement-event','{}')",
        "UPDATE strict_proofs SET payload='{}' WHERE proof_id='__proof_id__'",
        "DELETE FROM strict_proofs WHERE proof_id='__proof_id__'",
        "INSERT OR REPLACE INTO strict_proofs SELECT * FROM strict_proofs WHERE proof_id='__proof_id__'",
        "INSERT OR REPLACE INTO strict_proofs SELECT 'proof-2',kind,goal_id,scope_digest,subject_id,"
        "'replacement-event',source_seq,source_event_digest,proof_digest,actor_type,principal_id,account_id,"
        "session_id,expires_at,policy_digest,runtime_binding_digest,lease_generation,payload "
        "FROM strict_proofs WHERE proof_id='__proof_id__'",
        "DELETE FROM strict_heads WHERE proof_id='__proof_id__'",
        "INSERT OR REPLACE INTO strict_heads SELECT * FROM strict_heads WHERE proof_id='__proof_id__'",
        "UPDATE strict_heads SET source_seq=source_seq-1,generation=generation+1 WHERE proof_id='__proof_id__'",
    ],
)
def test_event_and_proof_history_are_append_only_and_heads_cannot_roll_back(tmp_path, statement):
    path = tmp_path / "append-only.db"
    WorkflowStore(path)
    proof_id = seed_v2_proof(path)

    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement.replace("__proof_id__", proof_id))
        db.rollback()


def test_in_memory_preflight_failure_leaves_target_untouched(tmp_path, monkeypatch):
    path = tmp_path / "preflight.db"
    create_v1(path)
    before = file_digest(path)
    before_journal = journal_mode(path)

    def reject_probe():
        raise ContractValidationError("simulated SQLite capability failure")

    monkeypatch.setattr(WorkflowStore, "_probe_sqlite_capabilities", staticmethod(reject_probe))
    with pytest.raises(ContractValidationError, match="simulated SQLite capability failure"):
        WorkflowStore(path)

    assert file_digest(path) == before
    assert schema_version(path) == 1
    assert journal_mode(path) == before_journal

