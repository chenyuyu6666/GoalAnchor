"""Append-only review attestations and lease-bound atomic controller commits.

This module verifies persistence structure, never signatures or native-host claims.
The caller must authenticate every event/proof before invoking the commit API.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .workflow_protocol import SignedEvent, fingerprint, strict_loads
from .workflow_store_core import (
    _GOAL_GENERATION_RECORD_KINDS,
    Expected,
    Payload,
    WorkflowStoreConflict,
    _json,
)

if TYPE_CHECKING:
    from .workflow_store_strict import WorkflowStore

_KIND = "controller.review.attested"
_TABLE = "strict_review_attestations"
_SCHEMA = {
    _TABLE: (
        "CREATE TABLE strict_review_attestations ("
        "goal_id TEXT NOT NULL, round_number INTEGER NOT NULL CHECK(round_number > 0), "
        "source_event_id TEXT NOT NULL UNIQUE, companion_event_id TEXT NOT NULL UNIQUE, "
        "payload TEXT NOT NULL, PRIMARY KEY(goal_id,round_number), "
        "FOREIGN KEY(source_event_id) REFERENCES events(event_id) DEFERRABLE INITIALLY DEFERRED, "
        "FOREIGN KEY(companion_event_id) REFERENCES events(event_id) DEFERRABLE INITIALLY DEFERRED)"
    ),
    "strict_review_attestations_no_update": (
        "CREATE TRIGGER strict_review_attestations_no_update BEFORE UPDATE ON strict_review_attestations "
        "BEGIN SELECT RAISE(ABORT,'strict review attestations are append-only'); END"
    ),
    "strict_review_attestations_no_delete": (
        "CREATE TRIGGER strict_review_attestations_no_delete BEFORE DELETE ON strict_review_attestations "
        "BEGIN SELECT RAISE(ABORT,'strict review attestations are append-only'); END"
    ),
    "strict_review_attestations_no_replace": (
        "CREATE TRIGGER strict_review_attestations_no_replace BEFORE INSERT ON strict_review_attestations "
        "WHEN EXISTS(SELECT 1 FROM strict_review_attestations WHERE "
        "(goal_id=NEW.goal_id AND round_number=NEW.round_number) OR source_event_id=NEW.source_event_id "
        "OR companion_event_id=NEW.companion_event_id) "
        "BEGIN SELECT RAISE(ABORT,'strict review attestations are append-only'); END"
    ),
}
_EVENT_GUARDS = {
    "events_no_update": "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
        "BEGIN SELECT RAISE(ABORT,'workflow event history is append-only'); END",
    "events_no_delete": "CREATE TRIGGER events_no_delete BEFORE DELETE ON events "
        "BEGIN SELECT RAISE(ABORT,'workflow event history is append-only'); END",
    "events_no_replace": "CREATE TRIGGER events_no_replace BEFORE INSERT ON events "
        "WHEN EXISTS(SELECT 1 FROM events WHERE event_id=NEW.event_id OR (NEW.seq>0 AND seq=NEW.seq)) "
        "BEGIN SELECT RAISE(ABORT,'workflow event history is append-only'); END",
}


def _error(message: str, code: str = "UNTRUSTED") -> WorkflowStoreConflict:
    return WorkflowStoreConflict(message, code)


def _object(raw: Any) -> Payload:
    value = strict_loads(raw)
    if not isinstance(value, dict):
        raise _error("Review storage payload must be an object")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _error("Review storage identifier is malformed")
    return value


def _round(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise _error("Review round must be a positive integer")
    return value


def _event(raw: Mapping[str, Any]) -> SignedEvent:
    event = SignedEvent.from_dict(raw)
    if event.event_id.startswith("store:") or _json(event.to_dict()) != _json(raw):
        raise _error("Review event is reserved or noncanonical")
    return event


def _attestation(raw: Mapping[str, Any]) -> Payload:
    value = _object(_json(raw))
    if set(value) != {"goal_id", "round_number", "source_event_id", "companion_event"}:
        raise _error("Review attestation has an invalid schema")
    goal_id = _identifier(value["goal_id"])
    round_number = _round(value["round_number"])
    source_id = _identifier(value["source_event_id"])
    companion = _event(value["companion_event"])
    payload = companion.payload
    if (
        source_id.startswith("store:") or companion.event_id == source_id or companion.kind != _KIND
        or payload.get("goal_id") != goal_id or type(payload.get("round_number")) is not int
        or payload.get("round_number") != round_number or payload.get("source_event_id") != source_id
    ):
        raise _error("Review attestation identity does not match its companion event")
    return value


def _require_schema(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] != 2:
        raise _error("Review attestation storage requires schema v2")
    for name, sql in _SCHEMA.items():
        found = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if found is None or not isinstance(found[0], str) or " ".join(found[0].split()) != " ".join(sql.split()):
            raise _error("Review attestation schema or append-only guard is missing or changed")
    for name, sql in _EVENT_GUARDS.items():
        found = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if found is None or not isinstance(found[0], str) or " ".join(found[0].split()) != " ".join(sql.split()):
            raise _error("Immutable workflow event history guard is missing or changed")


def _companion_events(db: sqlite3.Connection) -> list[SignedEvent]:
    result = []
    for event_id, raw in db.execute("SELECT event_id,payload FROM events ORDER BY seq"):
        data = _object(raw)
        if data.get("kind") != _KIND:
            continue
        signed = _event(data)
        if signed.event_id != event_id:
            raise _error("Review companion event row identity changed")
        result.append(signed)
    return result


def initialize_review_schema(store: WorkflowStore) -> None:
    """Add review-only structures to v2; never manufacture attestations from old rows."""
    db = sqlite3.connect(store.path, timeout=store.timeout, isolation_level=None)
    try:
        # Initialization preserves the existing journal mode, including DELETE.
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise _error("Review attestation storage requires schema v2")
        present = db.execute("SELECT name FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone()
        if present is None:
            if _companion_events(db):
                raise _error("Attested review events exist without their immutable table")
            for sql in _SCHEMA.values():
                db.execute(sql)
        _require_schema(db)
        db.commit()
    except sqlite3.DatabaseError as exc:
        raise _error("Review attestation schema is unavailable", "UNAVAILABLE") from exc
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()


def _match_source(attestation: Payload, source: SignedEvent) -> None:
    companion = _event(attestation["companion_event"])
    if (
        source.kind != "observer.review" or source.event_id != attestation["source_event_id"]
        or type(source.payload.get("round_number")) is not int
        or source.payload.get("round_number") != attestation["round_number"]
        or ("goal_id" in source.payload and source.payload["goal_id"] != attestation["goal_id"])
        or companion.payload.get("source_event_digest") != fingerprint(source.to_dict())
    ):
        raise _error("Review attestation does not reference its exact source event")


def _read(db: sqlite3.Connection, goal_id: str, round_number: int) -> Payload | None:
    row = db.execute(
        "SELECT goal_id,round_number,source_event_id,companion_event_id,payload "
        "FROM strict_review_attestations WHERE goal_id=? AND round_number=?", (goal_id, round_number),
    ).fetchone()
    if row is None:
        if any(e.payload.get("goal_id") == goal_id and e.payload.get("round_number") == round_number
               for e in _companion_events(db)):
            raise _error("Review companion event has no immutable attestation row")
        return None
    value = _attestation(_object(row[4]))
    companion = _event(value["companion_event"])
    if (
        row[:4] != (value["goal_id"], value["round_number"], value["source_event_id"], companion.event_id)
        or value["goal_id"] != goal_id or value["round_number"] != round_number
    ):
        raise _error("Review attestation columns and payload differ")
    source: SignedEvent | None = None
    for event_id in (value["source_event_id"], companion.event_id):
        event_row = db.execute("SELECT payload FROM events WHERE event_id=?", (event_id,)).fetchone()
        if event_row is None:
            raise _error("Review attestation source or companion event is missing")
        saved = _event(_object(event_row[0]))
        if saved.event_id != event_id:
            raise _error("Review attestation event row identity differs")
        if event_id == companion.event_id:
            if saved != companion:
                raise _error("Review companion event differs from immutable attestation")
        else:
            source = saved
    if source is None:
        raise _error("Review source event is missing")
    _match_source(value, source)
    return value


def read_review_attestation(store: WorkflowStore, goal_id: str, round_number: int) -> Payload | None:
    goal_id, round_number = _identifier(goal_id), _round(round_number)
    try:
        with store._read_transaction() as db:
            _require_schema(db)
            return _read(db, goal_id, round_number)
    except sqlite3.DatabaseError as exc:
        raise _error("Review attestation storage is unavailable", "UNAVAILABLE") from exc


def _changes(changes: Sequence[tuple[str, str, Payload]], expected: Expected) -> list[tuple[str, str, Payload]]:
    values = []
    seen = set()
    for kind, key, raw in changes:
        identity = (_identifier(kind), _identifier(key))
        if identity in seen or identity not in expected:
            raise _error("Every review projection requires one unique expected version")
        seen.add(identity)
        values.append((kind, key, _object(_json(raw))))
    return values


def _replay_expected(
    store: WorkflowStore, db: sqlite3.Connection, changes: Sequence[tuple[str, str, Payload]],
    expected: Expected, affected_goals: set[str],
) -> dict[tuple[str, str], str | None]:
    """Require saved postimages; original preimages cannot be used after one commit."""
    versions = dict(expected)
    written = {(kind, key): value for kind, key, value in changes}
    for identity, value in written.items():
        if identity[0] == "goal" and identity[1] in affected_goals:
            generation = value.get("generation", 0)
            if type(generation) is not int or generation < 0:
                raise _error("Goal generation in replay is malformed")
            value = {**value, "generation": generation + 1}
        versions[identity] = store.fingerprint(value)
    for goal_id in affected_goals:
        identity = ("goal", goal_id)
        if identity in written:
            continue
        current = store._strict_get(db, *identity)
        if current is None:
            if versions.get(identity) is not None:
                raise _error("Replay goal dependency is missing", "STALE")
            continue
        generation = current.get("generation")
        if type(generation) is not int or generation < 1:
            raise _error("Replay goal generation is malformed")
        before = {**current, "generation": generation - 1}
        if store.fingerprint(before) != versions.get(identity):
            raise _error("Replay goal dependency changed", "STALE")
        versions[identity] = store.fingerprint(current)
    return versions


def commit_review_bound(
    store: WorkflowStore, changes: Sequence[tuple[str, str, Payload]], expected: Expected,
    events: Sequence[tuple[str, Payload]], *, valid_until: str, clock: Callable[[], str],
    lease_factory: Callable[[str], Any], expected_runtime_digest: str, expected_lease_generation: int,
    attestation: Payload | None = None,
) -> bool:
    """Persist projections, signed events and optional attestation as one lease-bound transaction."""
    values = _changes(changes, expected)
    versions = dict(expected)
    encoded_events: dict[str, str] = {}
    signed_events: dict[str, SignedEvent] = {}
    if not events:
        raise _error("Bound review commit requires signed events")
    for event_id, raw in events:
        signed = _event(raw)
        if event_id != signed.event_id or event_id in encoded_events:
            raise _error("Review event batch identities are invalid")
        encoded_events[event_id] = _json(signed.to_dict())
        signed_events[event_id] = signed
    attested = None if attestation is None else _attestation(attestation)
    companions = [event for event in signed_events.values() if event.kind == _KIND]
    if attested is None:
        if companions:
            raise _error("Companion events require an immutable attestation")
    else:
        companion = _event(attested["companion_event"])
        source = signed_events.get(attested["source_event_id"])
        if len(companions) != 1 or companions[0] != companion or source is None:
            raise _error("Source and companion must be in the same attestation transaction")
        _match_source(attested, source)
        if ("goal", attested["goal_id"]) not in versions:
            raise _error("Attested review requires the expected goal version")
        for _kind, _key, value in values:
            if "goal_id" in value and value["goal_id"] != attested["goal_id"]:
                raise _error("Attested review projections must have the same goal owner")
            if "round_number" in value and (
                type(value["round_number"]) is not int or value["round_number"] != attested["round_number"]
            ):
                raise _error("Attested review projections must have the same round")
    affected_goals = {
        _identifier(value["goal_id"]) for kind, _key, value in values
        if kind in _GOAL_GENERATION_RECORD_KINDS and value.get("goal_id")
    }
    if any(("goal", goal_id) not in versions for goal_id in affected_goals):
        raise _error("Affected goal requires an expected version")
    try:
        with store._strict_transaction(
            lease_factory, expected_runtime_digest, expected_lease_generation, valid_until, clock,
        ) as (db, _lease):
            _require_schema(db)
            present = []
            for event_id, encoded in encoded_events.items():
                old = db.execute("SELECT payload FROM events WHERE event_id=?", (event_id,)).fetchone()
                if old is not None and _json(_object(old[0])) != encoded:
                    raise _error("Review event identity conflicts with stored content", "CONFLICT")
                present.append(old is not None)
            if any(present) and not all(present):
                raise _error("Partial review event batch replay", "CONFLICT")
            if all(present):
                store._strict_check(db, _replay_expected(store, db, values, versions, affected_goals))
                if attested is not None:
                    saved = _read(db, attested["goal_id"], attested["round_number"])
                    if saved is None or not store.same_json(saved, attested):
                        raise _error("Replay cannot repair or replace an immutable attestation")
                return False
            store._strict_check(db, versions)
            for (raw,) in db.execute("SELECT payload FROM events ORDER BY seq"):
                cancellation = _object(raw)
                if cancellation.get("kind") == "user.cancel" and ("goal", cancellation.get("subject")) in versions:
                    raise _error("Strict goal has been cancelled", "STALE")
            store._check_strict_grant_lifecycle(db, values)
            if attested is not None and _read(db, attested["goal_id"], attested["round_number"]) is not None:
                raise _error("Review round already has an immutable attestation", "CONFLICT")
            for event_id, encoded in encoded_events.items():
                db.execute("INSERT INTO events(event_id,payload) VALUES(?,?)", (event_id, encoded))
            for kind, key, value in values:
                store._put(db, kind, key, value)
            for goal_id in affected_goals:
                store._strict_touch_goal(db, goal_id)
            if attested is not None:
                db.execute(
                    "INSERT INTO strict_review_attestations"
                    "(goal_id,round_number,source_event_id,companion_event_id,payload) VALUES(?,?,?,?,?)",
                    (attested["goal_id"], attested["round_number"], attested["source_event_id"],
                     attested["companion_event"]["event_id"], _json(attested)),
                )
            return True
    except sqlite3.IntegrityError as exc:
        raise _error("Review attestation transaction violates immutable identity", "CONFLICT") from exc
    except sqlite3.DatabaseError as exc:
        raise _error("Review attestation transaction is unavailable", "UNAVAILABLE") from exc
