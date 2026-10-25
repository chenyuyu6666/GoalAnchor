"""Strict workflow persistence over the shared SQLite core."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import RLock
from typing import Any

from .core import ContractValidationError
from .workflow_protocol import Baseline, EvidenceSnapshot, SignedEvent, parse_time, strict_loads
from .workflow_protocol import fingerprint as protocol_fingerprint
from .workflow_store_core import (
    _GOAL_GENERATION_RECORD_KINDS,
    Expected,
    Payload,
    WorkflowStoreConflict,
    WorkflowStoreCore,
    _json,
    _key_component,
    _time,
)
from .workflow_store_review import (
    _require_schema,
    commit_review_bound,
    initialize_review_schema,
    read_review_attestation,
)

_LOGGER = logging.getLogger("goalanchor.workflow_store")
_STRICT_EVENT_HISTORY_MAX_ROWS = 100_000
_STRICT_EVENT_HISTORY_MAX_BYTES = 64 * 1024 * 1024
_STRICT_EVENT_HISTORY_MAX_ROW_BYTES = 16 * 1024 * 1024
_STRICT_EVENT_DECODE_CACHE_MAX_ENTRIES = 100_000
_STRICT_EVENT_DECODE_CACHE_MAX_BYTES = 64 * 1024 * 1024


class WorkflowStore(WorkflowStoreCore):
    """Strict heads, proofs, snapshots, and lease-bound commits."""

    def __init__(self, path: str | Path, timeout: float = 5) -> None:
        super().__init__(path, timeout)
        self._strict_event_decode_cache: OrderedDict[
            tuple[int, str, str], tuple[SignedEvent, int]
        ] = OrderedDict()
        self._strict_event_decode_cache_bytes = 0
        self._strict_event_decode_cache_lock = RLock()
        initialize_review_schema(self)

    def get_review_attestation(self, goal_id: str, round_number: int) -> Payload | None:
        return read_review_attestation(self, goal_id, round_number)

    def commit_review_bound(
        self, changes: Sequence[tuple[str, str, Payload]], expected: Expected,
        events: Sequence[tuple[str, Payload]], *, valid_until: str, clock: Callable[[], str],
        lease_factory: Callable[[str], Any], expected_runtime_digest: str, expected_lease_generation: int,
        attestation: Payload | None = None,
    ) -> bool:
        return commit_review_bound(
            self, changes, expected, events, valid_until=valid_until, clock=clock,
            lease_factory=lease_factory, expected_runtime_digest=expected_runtime_digest,
            expected_lease_generation=expected_lease_generation, attestation=attestation,
        )

    def dispatch_review_bound(
        self, expected: Expected, *, valid_until: str, clock: Callable[[], str],
        lease_factory: Callable[[str], Any], expected_runtime_digest: str,
        expected_lease_generation: int, dispatch: Callable[[], Payload],
    ) -> Payload:
        """Linearize admission under the writer lock, retain identity through dispatch.

        Consumption is already durable. Release the SQLite writer before calling
        external code; no provider callbacks run inside this final admission.
        """
        with self._strict_transaction(lease_factory, expected_runtime_digest,
                                      expected_lease_generation, valid_until, clock) as (db, _lease):
            _require_schema(db)
            self._strict_check(db, expected)
            for (raw,) in db.execute("SELECT payload FROM events ORDER BY seq"):
                source = strict_loads(raw)
                if source.get("kind") == "user.cancel" and ("goal", source.get("subject")) in expected:
                    raise WorkflowStoreConflict("Strict goal has been cancelled", "STALE")
            # Revocation events cannot be erased by resetting a scope projection.
            for (kind, key), _digest in expected.items():
                if kind != "strict_grant":
                    continue
                for (raw,) in db.execute("SELECT payload FROM events ORDER BY seq"):
                    event = strict_loads(raw)
                    if event.get("kind") == "user.revoke" and event.get("payload", {}).get("grant_id") == key:
                        raise WorkflowStoreConflict("Strict dispatch grant was revoked", "STALE")
            db.commit()
            if _time(clock()) >= _time(valid_until):
                raise WorkflowStoreConflict("Strict proof expired at dispatch boundary", "STALE")
            return dispatch()

    def _cached_strict_source_event(
        self, seq: int, event_id: str, payload_digest: str
    ) -> SignedEvent | None:
        key = (seq, event_id, payload_digest)
        try:
            with self._strict_event_decode_cache_lock:
                entry = self._strict_event_decode_cache.get(key)
                if entry is None:
                    return None
                self._strict_event_decode_cache.move_to_end(key)
                event, size_bytes = entry
                if not isinstance(event, SignedEvent) or type(size_bytes) is not int or size_bytes < 0:
                    raise RuntimeError("invalid Strict event decode cache entry")
                return event
        except Exception as exc:
            raise WorkflowStoreConflict('Strict event decode cache lookup failed', 'UNAVAILABLE') from exc

    def _cache_strict_source_event(
        self, seq: int, event_id: str, payload_digest: str, event: SignedEvent, size_bytes: int
    ) -> SignedEvent:
        if size_bytes > _STRICT_EVENT_DECODE_CACHE_MAX_BYTES:
            return event
        key = (seq, event_id, payload_digest)
        try:
            with self._strict_event_decode_cache_lock:
                existing = self._strict_event_decode_cache.get(key)
                if existing is not None:
                    self._strict_event_decode_cache.move_to_end(key)
                    return existing[0]
                self._strict_event_decode_cache[key] = (event, size_bytes)
                self._strict_event_decode_cache_bytes += size_bytes
                while (
                    len(self._strict_event_decode_cache) > _STRICT_EVENT_DECODE_CACHE_MAX_ENTRIES
                    or self._strict_event_decode_cache_bytes > _STRICT_EVENT_DECODE_CACHE_MAX_BYTES
                ):
                    _old_key, (_old_event, old_size) = self._strict_event_decode_cache.popitem(last=False)
                    self._strict_event_decode_cache_bytes -= old_size
        except Exception as exc:
            raise WorkflowStoreConflict('Strict event decode cache insertion failed', 'UNAVAILABLE') from exc
        return event


    @staticmethod
    def _read_strict_head(db: sqlite3.Connection, key: tuple[str, str, str, str]) -> Payload | None:
        row = db.execute(
            'SELECT kind,goal_id,scope_digest,subject_id,proof_id,source_seq,generation '
            'FROM strict_heads WHERE kind=? AND goal_id=? AND scope_digest=? AND subject_id=?', key
        ).fetchone()
        if row is None:
            return None
        names = ('kind', 'goal_id', 'scope_digest', 'subject_id', 'proof_id', 'source_seq', 'generation')
        return WorkflowStore._strict_head_payload(row, names)

    @staticmethod
    def _read_strict_head_history(db: sqlite3.Connection, head: Mapping[str, Any]) -> list[Payload]:
        rows = db.execute(
            'SELECT proof_id,source_seq FROM strict_proofs WHERE kind=? AND goal_id=? '
            'AND scope_digest=? AND subject_id=? ORDER BY source_seq',
            (head['kind'], head['goal_id'], head['scope_digest'], head['subject_id']),
        ).fetchall()
        if not rows or len(rows) != head['generation']:
            raise WorkflowStoreConflict('Strict head generation does not match its proof history', 'UNTRUSTED')
        history: list[Payload] = []
        previous_seq = 0
        for proof_id, source_seq in rows:
            if type(source_seq) is not int or source_seq <= previous_seq:
                raise WorkflowStoreConflict('Strict proof history sequence is malformed', 'UNTRUSTED')
            history.append({'proof_id': proof_id, 'source_seq': source_seq})
            previous_seq = source_seq
        if (
            history[-1]['source_seq'] != head['source_seq']
            or history[-1]['proof_id'] != head['proof_id']
        ):
            raise WorkflowStoreConflict('Strict head is not the latest proof in its history', 'UNTRUSTED')
        return history

    @staticmethod
    def _strict_head_payload(row: Sequence[Any], names: Sequence[str] | None = None) -> Payload:
        fields = names or ('kind', 'goal_id', 'scope_digest', 'subject_id', 'proof_id', 'source_seq', 'generation')
        head = {name: value for name, value in zip(fields, row, strict=True)}
        for field in ('source_seq', 'generation'):
            value = head[field]
            if type(value) is not int or value < 1:
                raise WorkflowStoreConflict(f'Strict head {field} is malformed', 'UNTRUSTED')
        return head

    @staticmethod
    def _strict_json_object(raw: Any, label: str) -> Payload:
        try:
            payload = strict_loads(raw)
        except (ContractValidationError, RecursionError, TypeError, ValueError) as exc:
            raise WorkflowStoreConflict(f'Strict {label} is malformed JSON', 'UNTRUSTED') from exc
        if not isinstance(payload, dict):
            raise WorkflowStoreConflict(f'Strict {label} is not an object', 'UNTRUSTED')
        return payload

    @staticmethod
    def _strict_source_event(payload: Mapping[str, Any]) -> Payload:
        try:
            event = SignedEvent.from_dict(payload)
        except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowStoreConflict('Strict source event is malformed', 'UNTRUSTED') from exc
        return event.to_dict()

    @staticmethod
    def _read_strict_source_event(
        db: sqlite3.Connection, event_id: str, label: str
    ) -> Payload:
        row = db.execute(
            'SELECT seq,event_id,payload FROM events WHERE event_id=?', (event_id,)
        ).fetchone()
        if (
            row is None
            or type(row[0]) is not int
            or row[0] < 1
            or row[1] != event_id
        ):
            raise WorkflowStoreConflict(f'Strict {label} source event is missing or moved', 'UNTRUSTED')
        event = WorkflowStore._strict_source_event(
            WorkflowStore._strict_json_object(row[2], f'{label} source event')
        )
        if event.get('event_id') != event_id:
            raise WorkflowStoreConflict(f'Strict {label} source event identity does not match', 'UNTRUSTED')
        return {'seq': row[0], 'event_id': row[1], 'payload': event}

    @staticmethod
    def _strict_get(db: sqlite3.Connection, kind: str, key: str) -> Payload | None:
        row = db.execute('SELECT payload FROM records WHERE kind=? AND key=?', (kind, key)).fetchone()
        return WorkflowStore._strict_json_object(row[0], f'{kind} projection') if row is not None else None

    @staticmethod
    def strict_evidence_key_prefix(goal_id: str) -> str:
        """Return an injective, versioned prefix for strict evidence records."""
        return 'v1:' + _key_component(goal_id) + ':'

    @staticmethod
    def strict_evidence_key(goal_id: str, evidence_id: str) -> str:
        return WorkflowStore.strict_evidence_key_prefix(goal_id) + _key_component(evidence_id)

    @staticmethod
    def legacy_evidence_key(goal_id: str, evidence_id: str) -> str:
        """Return the historical key used by the compatibility Workflow API."""
        return goal_id + ':' + evidence_id

    @staticmethod
    def expected_baseline_goal_projection(proof: Mapping[str, Any], event: Mapping[str, Any]) -> Payload:
        proof_payload = proof.get('payload')
        baseline_data = proof_payload.get('baseline') if isinstance(proof_payload, Mapping) else None
        try:
            if not isinstance(baseline_data, Mapping):
                raise ContractValidationError('Strict baseline payload is missing')
            baseline = Baseline.from_dict(baseline_data)
            normalized_baseline = baseline.to_dict()
        except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowStoreConflict('Strict baseline payload is malformed', 'UNTRUSTED') from exc
        if not WorkflowStore.same_json(baseline_data, normalized_baseline):
            raise WorkflowStoreConflict('Strict baseline payload is not canonical', 'UNTRUSTED')
        source_event = WorkflowStore._strict_source_event(event)
        if (
            source_event.get('event_id') != proof.get('source_event_id')
            or source_event.get('kind') != 'user.baseline'
            or source_event.get('subject') != baseline.fingerprint
            or not WorkflowStore.same_json(source_event.get('payload'), normalized_baseline)
            or WorkflowStore.fingerprint(source_event) != proof.get('source_event_digest')
            or baseline.goal_id != proof.get('goal_id')
            or baseline.fingerprint != proof.get('baseline_digest')
        ):
            raise WorkflowStoreConflict('Strict baseline source event does not match its proof', 'UNTRUSTED')
        attestation = WorkflowStore._strict_attestation(proof, source_event)
        proof_id = WorkflowStore.strict_proof_identifier(proof)
        if proof.get('proof_id') not in (None, proof_id):
            raise WorkflowStoreConflict('Strict baseline proof ID is not canonical', 'UNTRUSTED')
        expected = {
            'goal_id': baseline.goal_id,
            'revision': baseline.revision,
            'baseline': normalized_baseline,
            'baseline_digest': baseline.fingerprint,
            'policy_digest': proof.get('policy_digest'),
            'baseline_event_id': source_event['event_id'],
            'strict_baseline_proof_id': proof_id,
            'runtime_binding_digest': proof.get('runtime_binding_digest'),
            'trusted_identity': {
                'principal_id': attestation['principal_id'],
                'account_id': attestation['account_id'],
                'session_id': attestation['session_id'],
                'source_event_id': attestation['source_event_id'],
                'proof_digest': attestation['proof_digest'],
            },
        }
        if not isinstance(proof_payload, Mapping) or not WorkflowStore.same_json(
            proof_payload.get('goal_projection'), expected
        ):
            raise WorkflowStoreConflict('Strict baseline proof projection is not bound to its source', 'UNTRUSTED')
        return expected

    @staticmethod
    def expected_evidence_projection(proof: Mapping[str, Any], event: Mapping[str, Any]) -> Payload:
        proof_payload = proof.get('payload')
        if not isinstance(proof_payload, Mapping):
            raise WorkflowStoreConflict('Strict evidence proof payload is malformed', 'UNTRUSTED')
        snapshot_data = proof_payload.get('snapshot')
        try:
            if not isinstance(snapshot_data, Mapping):
                raise ContractValidationError('Strict evidence snapshot is missing')
            snapshot = EvidenceSnapshot.from_dict(snapshot_data)
            normalized_snapshot = snapshot.to_dict()
        except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowStoreConflict('Strict evidence snapshot is malformed', 'UNTRUSTED') from exc
        if not WorkflowStore.same_json(snapshot_data, normalized_snapshot):
            raise WorkflowStoreConflict('Strict evidence snapshot is not canonical', 'UNTRUSTED')
        source_event = WorkflowStore._strict_source_event(event)
        goal_id = proof.get('goal_id')
        event_payload = {'goal_id': goal_id, 'snapshot': normalized_snapshot}
        if (
            source_event.get('event_id') != proof.get('source_event_id')
            or source_event.get('kind') != 'observer.evidence'
            or source_event.get('subject') != snapshot.fingerprint
            or not WorkflowStore.same_json(source_event.get('payload'), event_payload)
            or WorkflowStore.fingerprint(source_event) != proof.get('source_event_digest')
            or snapshot.evidence_id != proof.get('subject_id')
            or snapshot.fingerprint != proof_payload.get('snapshot_fingerprint')
            or proof.get('scope_digest') != proof.get('baseline_digest')
        ):
            raise WorkflowStoreConflict('Strict evidence source event does not match its proof', 'UNTRUSTED')
        attestation = WorkflowStore._strict_attestation(proof, source_event)
        proof_id = WorkflowStore.strict_proof_identifier(proof)
        if proof.get('proof_id') not in (None, proof_id):
            raise WorkflowStoreConflict('Strict evidence proof ID is not canonical', 'UNTRUSTED')
        expected = {
            'goal_id': goal_id,
            'snapshot': normalized_snapshot,
            'source_event_id': source_event['event_id'],
            'strict_proof_id': proof_id,
            'trusted_source': {
                'principal_id': attestation['principal_id'],
                'account_id': attestation['account_id'],
                'session_id': attestation['session_id'],
                'proof_digest': attestation['proof_digest'],
            },
        }
        if not isinstance(proof_payload, Mapping) or not WorkflowStore.same_json(
            proof_payload.get('projection'), expected
        ):
            raise WorkflowStoreConflict('Strict evidence proof projection is not bound to its source', 'UNTRUSTED')
        return expected

    @staticmethod
    def _validate_strict_goal_record(
        db: sqlite3.Connection, goal: Mapping[str, Any] | None, head: Mapping[str, Any],
        proof: Mapping[str, Any],
    ) -> tuple[Baseline, Payload | None]:
        if goal is None:
            raise WorkflowStoreConflict('Strict goal projection is missing', 'UNTRUSTED')
        event_record = WorkflowStore._read_strict_source_event(
            db, proof['source_event_id'], 'baseline'
        )
        expected = WorkflowStore.expected_baseline_goal_projection(proof, event_record['payload'])
        baseline = Baseline.from_dict(proof['payload']['baseline'])
        baseline_projection = WorkflowStore._strict_get(db, 'baseline', proof['baseline_digest'])
        if (
            baseline_projection is None
            or not WorkflowStore.same_json(baseline_projection, baseline.to_dict())
            or WorkflowStore.fingerprint(baseline_projection) != proof['baseline_digest']
        ):
            raise WorkflowStoreConflict('Strict baseline projection does not match its proof', 'UNTRUSTED')
        allowed_goal_fields = set(expected) | {
            'state', 'generation', 'baseline_created_at', 'supersedes',
        }
        if (
            set(goal) != allowed_goal_fields
            or not WorkflowStore.same_json(
                {key: goal[key] for key in expected}, expected
            )
            or goal.get('state') != 'PLAN'
            or type(goal.get('generation')) is not int
            or goal.get('generation', -1) < 0
            or goal.get('baseline_created_at') != event_record['payload'].get('created_at')
        ):
            raise WorkflowStoreConflict('Strict goal projection is malformed or mismatched', 'UNTRUSTED')
        supersedes = goal.get('supersedes')
        if supersedes is not None and (
            not isinstance(supersedes, str)
            or len(supersedes) != 64
            or supersedes != supersedes.lower()
            or any(character not in '0123456789abcdef' for character in supersedes)
        ):
            raise WorkflowStoreConflict('Strict supersedes digest is malformed', 'UNTRUSTED')
        history = WorkflowStore._read_strict_head_history(db, head)
        previous_proof: Payload | None = None
        if head['generation'] > 1:
            previous_proof = WorkflowStore._read_strict_proof(db, history[-2]['proof_id'])
            if previous_proof is None:
                raise WorkflowStoreConflict('Strict baseline predecessor proof is missing', 'UNTRUSTED')
            previous_event = WorkflowStore._read_strict_source_event(
                db, previous_proof['source_event_id'], 'baseline predecessor'
            )
            previous_projection = WorkflowStore.expected_baseline_goal_projection(
                previous_proof, previous_event['payload']
            )
            if (
                goal.get('supersedes') != previous_proof.get('baseline_digest')
                or previous_projection['revision'] >= baseline.revision
            ):
                raise WorkflowStoreConflict('Strict baseline predecessor binding is invalid', 'UNTRUSTED')
        return baseline, previous_proof

    @staticmethod
    def _validate_evidence_projection_record(
        db: sqlite3.Connection, goal_id: str, key: str, projection: Mapping[str, Any], *,
        strict_namespace: bool,
        legacy_key_identities: Mapping[str, set[tuple[str, str]]] | None = None,
    ) -> tuple[Payload, Payload, Payload, bool]:
        if projection.get('goal_id') != goal_id:
            raise WorkflowStoreConflict('Strict evidence projection references the wrong goal', 'UNTRUSTED')
        snapshot_data = projection.get('snapshot')
        try:
            if not isinstance(snapshot_data, Mapping):
                raise ContractValidationError('Evidence snapshot is missing')
            snapshot = EvidenceSnapshot.from_dict(snapshot_data)
        except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowStoreConflict('Strict evidence projection snapshot is malformed', 'UNTRUSTED') from exc
        expected_key = (
            WorkflowStore.strict_evidence_key(goal_id, snapshot.evidence_id)
            if strict_namespace else WorkflowStore.legacy_evidence_key(goal_id, snapshot.evidence_id)
        )
        if key != expected_key:
            raise WorkflowStoreConflict('Strict evidence projection key does not match its payload', 'UNTRUSTED')
        proof_id = projection.get('strict_proof_id')
        if not isinstance(proof_id, str) or not proof_id:
            raise WorkflowStoreConflict('Strict evidence projection has no proof pointer', 'UNTRUSTED')
        if not strict_namespace:
            identities = (
                legacy_key_identities.get(key, set())
                if legacy_key_identities is not None
                else {
                    (row[0], row[1])
                    for row in db.execute(
                        "SELECT DISTINCT goal_id,subject_id FROM strict_proofs WHERE kind='evidence'"
                    ).fetchall()
                    if isinstance(row[0], str) and isinstance(row[1], str) and row[0] + ':' + row[1] == key
                }
            )
            if len(identities) > 1:
                raise WorkflowStoreConflict('Legacy evidence key is ambiguous', 'UNTRUSTED')
        proof = WorkflowStore._read_strict_proof(db, proof_id)
        if proof is None or (
            proof.get('kind') != 'evidence'
            or proof.get('goal_id') != goal_id
            or proof.get('subject_id') != snapshot.evidence_id
        ):
            raise WorkflowStoreConflict(
                'Strict evidence projection references a missing or mismatched proof', 'UNTRUSTED'
            )
        head_key = ('evidence', goal_id, proof['scope_digest'], snapshot.evidence_id)
        head = WorkflowStore._read_strict_head(db, head_key)
        if head is None:
            raise WorkflowStoreConflict('Strict evidence proof has no current head', 'UNTRUSTED')
        history = WorkflowStore._read_strict_head_history(db, head)
        if not any(
            item['proof_id'] == proof_id and item['source_seq'] == proof['source_seq']
            for item in history
        ):
            raise WorkflowStoreConflict('Strict evidence proof is outside its head history', 'UNTRUSTED')
        event_record = WorkflowStore._read_strict_source_event(db, proof['source_event_id'], 'evidence')
        if event_record['seq'] != proof['source_seq']:
            raise WorkflowStoreConflict('Strict evidence event sequence does not match its proof', 'UNTRUSTED')
        expected_projection = WorkflowStore.expected_evidence_projection(
            proof, event_record['payload']
        )
        if not WorkflowStore.same_json(projection, expected_projection):
            raise WorkflowStoreConflict('Strict evidence projection is not bound to its source', 'UNTRUSTED')
        active = proof_id == head['proof_id'] and proof['source_seq'] == head['source_seq']
        if strict_namespace and not active:
            raise WorkflowStoreConflict('Strict evidence projection references a superseded proof', 'UNTRUSTED')
        return proof, head, event_record, active

    @staticmethod
    def _validate_active_baseline(
        db: sqlite3.Connection, goal_id: str,
    ) -> tuple[Payload, Payload, Payload, Baseline]:
        head = WorkflowStore._read_strict_head(db, ('baseline', goal_id, '', 'current'))
        goal = WorkflowStore._strict_get(db, 'goal', goal_id)
        if head is None or goal is None:
            raise WorkflowStoreConflict('Strict baseline is unavailable', 'UNTRUSTED')
        proof = WorkflowStore._read_strict_proof(db, head['proof_id'])
        if proof is None or proof.get('goal_id') != goal_id or proof.get('source_seq') != head['source_seq']:
            raise WorkflowStoreConflict('Strict baseline head references a missing or mismatched proof', 'UNTRUSTED')
        baseline, _previous = WorkflowStore._validate_strict_goal_record(db, goal, head, proof)
        return head, proof, goal, baseline

    @staticmethod
    def _validate_active_evidence_record(
        db: sqlite3.Connection, proof: Mapping[str, Any],
    ) -> tuple[Payload, Payload, Payload]:
        goal_id = proof.get('goal_id')
        evidence_id = proof.get('subject_id')
        scope_digest = proof.get('scope_digest')
        if (
            not isinstance(goal_id, str)
            or not isinstance(evidence_id, str)
            or not isinstance(scope_digest, str)
        ):
            raise WorkflowStoreConflict('Strict evidence identity is malformed', 'UNTRUSTED')
        head_key = ('evidence', goal_id, scope_digest, evidence_id)
        head = WorkflowStore._read_strict_head(db, head_key)
        if head is None or head.get('proof_id') != proof.get('proof_id'):
            raise WorkflowStoreConflict('Strict evidence proof is no longer the active head', 'STALE')
        WorkflowStore._read_strict_head_history(db, head)
        strict_key = WorkflowStore.strict_evidence_key(goal_id, evidence_id)
        strict_projection = WorkflowStore._strict_get(db, 'strict_evidence', strict_key)
        legacy_key = WorkflowStore.legacy_evidence_key(goal_id, evidence_id)
        legacy_projection = WorkflowStore._strict_get(db, 'evidence', legacy_key)
        projection: Payload | None
        if strict_projection is not None:
            if (
                isinstance(legacy_projection, Mapping)
                and legacy_projection.get('goal_id') == goal_id
                and legacy_projection.get('strict_proof_id') == proof.get('proof_id')
            ):
                raise WorkflowStoreConflict('Strict evidence has duplicate active projections', 'UNTRUSTED')
            projection = strict_projection
            namespace = True
        else:
            projection = legacy_projection
            namespace = False
        if projection is None:
            raise WorkflowStoreConflict('Strict evidence active projection is missing', 'UNTRUSTED')
        validated_proof, validated_head, event_record, active = (
            WorkflowStore._validate_evidence_projection_record(
                db, goal_id, strict_key if namespace else legacy_key, projection,
                strict_namespace=namespace,
            )
        )
        if (
            not active
            or validated_proof.get('proof_id') != proof.get('proof_id')
            or validated_head.get('proof_id') != head.get('proof_id')
        ):
            raise WorkflowStoreConflict('Strict evidence projection is not the active proof', 'UNTRUSTED')
        return head, projection, event_record

    @staticmethod
    def _validate_new_baseline_commit(
        db: sqlite3.Connection, proof: Mapping[str, Any], event: Mapping[str, Any],
        changes: Sequence[tuple[str, str, Payload]], current_head: Mapping[str, Any] | None,
    ) -> None:
        baseline_data = proof.get('payload', {}).get('baseline')
        try:
            if not isinstance(baseline_data, Mapping):
                raise ContractValidationError('Baseline proof payload is missing')
            baseline = Baseline.from_dict(baseline_data)
        except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowStoreConflict('Strict baseline payload is malformed', 'UNTRUSTED') from exc
        if not WorkflowStore.same_json(baseline_data, baseline.to_dict()):
            raise WorkflowStoreConflict('Strict baseline payload is not canonical', 'UNTRUSTED')
        goal_change = next((payload for kind, _key, payload in changes if kind == 'goal'), None)
        baseline_change = next((payload for kind, _key, payload in changes if kind == 'baseline'), None)
        if goal_change is None or baseline_change is None:
            raise WorkflowStoreConflict('Strict baseline projections are incomplete', 'UNTRUSTED')
        old_goal = WorkflowStore._strict_get(db, 'goal', proof['goal_id'])
        if old_goal is None:
            expected_goal_generation = 0
        else:
            old_generation = old_goal.get('generation', 0)
            if type(old_generation) is not int or old_generation < 0:
                raise WorkflowStoreConflict('Legacy goal generation is malformed', 'UNTRUSTED')
            expected_goal_generation = old_generation
        if current_head is not None:
            current_proof = WorkflowStore._read_strict_proof(db, current_head['proof_id'])
            if current_proof is None:
                raise WorkflowStoreConflict('Current strict baseline proof is missing', 'UNTRUSTED')
            current_baseline, _previous = WorkflowStore._validate_strict_goal_record(
                db, old_goal, current_head, current_proof
            )
            if baseline.revision <= current_baseline.revision:
                raise WorkflowStoreConflict('Strict baseline revision must advance', 'STALE')
            expected_supersedes = current_proof['baseline_digest']
        else:
            expected_supersedes = None
            if old_goal is not None:
                old_revision = old_goal.get('revision', 0)
                if type(old_revision) is not int or old_revision < 0:
                    raise WorkflowStoreConflict('Legacy baseline revision is malformed', 'UNTRUSTED')
                old_digest = old_goal.get('baseline_digest')
                if old_digest is not None:
                    if (
                        not isinstance(old_digest, str)
                        or len(old_digest) != 64
                        or old_digest != old_digest.lower()
                        or any(character not in '0123456789abcdef' for character in old_digest)
                        or old_revision < 1
                    ):
                        raise WorkflowStoreConflict('Legacy baseline root is malformed', 'UNTRUSTED')
                    expected_supersedes = old_digest
                if baseline.revision <= old_revision:
                    raise WorkflowStoreConflict('Strict baseline revision must advance', 'STALE')
        if goal_change.get('generation') != expected_goal_generation:
            raise WorkflowStoreConflict(
                'Strict baseline goal generation does not match its transaction preimage', 'UNTRUSTED'
            )
        if goal_change.get('supersedes') != expected_supersedes:
            raise WorkflowStoreConflict('Strict baseline supersedes value does not match its current root', 'UNTRUSTED')
        expected_projection = WorkflowStore.expected_baseline_goal_projection(proof, event)
        mutable_fields = {'state', 'generation', 'baseline_created_at', 'supersedes'}
        if (
            not WorkflowStore.same_json(
                {key: goal_change[key] for key in expected_projection}, expected_projection
            )
            or set(goal_change) != set(expected_projection) | mutable_fields
            or not WorkflowStore.same_json(baseline_change, baseline.to_dict())
            or goal_change.get('state') != 'PLAN'
            or type(goal_change.get('generation')) is not int
            or goal_change.get('generation', -1) < 0
            or goal_change.get('baseline_created_at') != event.get('created_at')
        ):
            raise WorkflowStoreConflict('Strict baseline commit projection is not source-bound', 'UNTRUSTED')

    @staticmethod
    def _validate_new_evidence_commit(
        db: sqlite3.Connection, proof: Mapping[str, Any], event: Mapping[str, Any],
        changes: Sequence[tuple[str, str, Payload]],
    ) -> None:
        _baseline_head, baseline_proof, _goal, baseline = WorkflowStore._validate_active_baseline(
            db, proof['goal_id']
        )
        if (
            proof.get('baseline_digest') != baseline.fingerprint
            or proof.get('scope_digest') != baseline.fingerprint
            or proof.get('policy_digest') != baseline_proof.get('policy_digest')
            or proof.get('runtime_binding_digest') != baseline_proof.get('runtime_binding_digest')
        ):
            raise WorkflowStoreConflict('Strict evidence does not match the active baseline', 'STALE')
        evidence_head = WorkflowStore._read_strict_head(
            db, ('evidence', proof['goal_id'], baseline.fingerprint, proof['subject_id'])
        )
        if evidence_head is not None:
            WorkflowStore._read_strict_head_history(db, evidence_head)
            previous_proof = WorkflowStore._read_strict_proof(db, evidence_head['proof_id'])
            if previous_proof is None:
                raise WorkflowStoreConflict('Current strict evidence proof is missing', 'UNTRUSTED')
            WorkflowStore._validate_active_evidence_record(db, previous_proof)
        projection = next((payload for kind, _key, payload in changes if kind == 'strict_evidence'), None)
        if projection is None:
            raise WorkflowStoreConflict('Strict evidence projection is missing', 'UNTRUSTED')
        expected_projection = WorkflowStore.expected_evidence_projection(proof, event)
        if not WorkflowStore.same_json(projection, expected_projection):
            raise WorkflowStoreConflict('Strict evidence commit projection is not source-bound', 'UNTRUSTED')

    @staticmethod
    def _legacy_evidence_key_identities(
        db: sqlite3.Connection,
    ) -> dict[str, set[tuple[str, str]]]:
        identities: dict[str, set[tuple[str, str]]] = {}
        for goal_id, evidence_id in db.execute(
            "SELECT DISTINCT goal_id,subject_id FROM strict_proofs WHERE kind='evidence'"
        ).fetchall():
            if isinstance(goal_id, str) and isinstance(evidence_id, str):
                identities.setdefault(goal_id + ':' + evidence_id, set()).add((goal_id, evidence_id))
        return identities

    @staticmethod
    def _strict_attestation(proof: Mapping[str, Any], event: Mapping[str, Any] | None = None) -> Payload:
        required = {
            'actor_type', 'principal_id', 'account_id', 'session_id', 'source_event_id', 'purpose',
            'event_kind', 'subject', 'binding_digest', 'expires_at', 'nonce', 'proof_digest',
        }
        attestation = proof.get('trusted_proof')
        if not isinstance(attestation, Mapping) or set(attestation) != required:
            raise WorkflowStoreConflict('Malformed strict provider attestation', 'UNTRUSTED')
        values = dict(attestation)
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise WorkflowStoreConflict('Malformed strict provider attestation', 'UNTRUSTED')
        for field in ('actor_type', 'principal_id', 'account_id', 'session_id', 'source_event_id', 'expires_at',
                      'proof_digest'):
            if values[field] != proof.get(field):
                raise WorkflowStoreConflict('Strict provider attestation does not match its proof', 'UNTRUSTED')
        try:
            _time(values['expires_at'])
        except ContractValidationError as exc:
            raise WorkflowStoreConflict('Malformed strict provider expiry', 'UNTRUSTED') from exc
        for field in ('binding_digest', 'proof_digest'):
            digest = values[field]
            if len(digest) != 64 or digest != digest.lower() or any(
                character not in '0123456789abcdef' for character in digest
            ):
                raise WorkflowStoreConflict('Malformed strict provider digest', 'UNTRUSTED')
        proof_payload = proof.get('payload')
        if not isinstance(proof_payload, Mapping):
            raise WorkflowStoreConflict('Strict proof binding payload is malformed', 'UNTRUSTED')
        kind = proof.get('kind')
        if kind == 'baseline':
            baseline = proof_payload.get('baseline')
            if not isinstance(baseline, Mapping):
                raise WorkflowStoreConflict('Strict baseline binding payload is malformed', 'UNTRUSTED')
            try:
                baseline_digest = WorkflowStore.fingerprint(baseline)
                expected_binding = {
                    'goal_id': proof['goal_id'],
                    'baseline_digest': proof['baseline_digest'],
                    'runtime_binding_digest': proof['runtime_binding_digest'],
                    'plan_revision': baseline['revision'],
                    'objective': baseline['objective'],
                    'criteria_digest': protocol_fingerprint(baseline['criteria']),
                }
            except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
                raise WorkflowStoreConflict('Strict baseline binding fields are malformed', 'UNTRUSTED') from exc
            if (
                values['actor_type'] != 'USER'
                or values['purpose'] != 'user.baseline'
                or values['event_kind'] != 'user.baseline'
                or proof.get('scope_digest') != ''
                or proof.get('subject_id') != 'current'
                or baseline.get('goal_id') != proof.get('goal_id')
                or baseline_digest != proof.get('baseline_digest')
            ):
                raise WorkflowStoreConflict('Strict baseline binding does not match its proof', 'UNTRUSTED')
            expected_subject = proof.get('baseline_digest')
        elif kind == 'evidence':
            snapshot = proof_payload.get('snapshot')
            snapshot_digest = proof_payload.get('snapshot_fingerprint')
            if not isinstance(snapshot, Mapping) or not isinstance(snapshot_digest, str):
                raise WorkflowStoreConflict('Strict evidence binding payload is malformed', 'UNTRUSTED')
            try:
                expected_binding = {
                    'goal_id': proof['goal_id'],
                    'baseline_digest': proof['baseline_digest'],
                    'runtime_binding_digest': proof['runtime_binding_digest'],
                    'evidence_id': proof['subject_id'],
                    'evidence_digest': snapshot['sha256'],
                    'snapshot_fingerprint': snapshot_digest,
                }
                actual_snapshot_digest = protocol_fingerprint(snapshot)
            except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
                raise WorkflowStoreConflict('Strict evidence binding fields are malformed', 'UNTRUSTED') from exc
            if (
                values['actor_type'] != 'SERVICE'
                or values['purpose'] != 'observer.evidence'
                or values['event_kind'] != 'observer.evidence'
                or proof.get('scope_digest') != proof.get('baseline_digest')
                or snapshot.get('evidence_id') != proof.get('subject_id')
                or actual_snapshot_digest != snapshot_digest
            ):
                raise WorkflowStoreConflict('Strict evidence binding does not match its proof', 'UNTRUSTED')
            expected_subject = snapshot_digest
        else:
            raise WorkflowStoreConflict('Unsupported strict provider proof kind', 'UNTRUSTED')
        expected_binding_digest = protocol_fingerprint(expected_binding)
        if values['binding_digest'] != expected_binding_digest:
            raise WorkflowStoreConflict('Strict provider binding digest does not match its proof', 'UNTRUSTED')
        if event is not None and (
            values['source_event_id'] != event.get('event_id')
            or values['event_kind'] != event.get('kind')
            or values['subject'] != event.get('subject')
            or values['subject'] != expected_subject
        ):
            raise WorkflowStoreConflict('Strict provider attestation does not match its source event', 'UNTRUSTED')
        return values

    @staticmethod
    def _read_strict_proof(db: sqlite3.Connection, proof_id: str) -> Payload | None:
        columns = (
            'proof_id', 'kind', 'goal_id', 'scope_digest', 'subject_id', 'source_event_id', 'source_seq',
            'source_event_digest', 'proof_digest', 'actor_type', 'principal_id', 'account_id', 'session_id',
            'expires_at', 'policy_digest', 'runtime_binding_digest', 'lease_generation', 'payload',
        )
        row = db.execute(
            'SELECT ' + ','.join(columns) + ' FROM strict_proofs WHERE proof_id=?', (proof_id,)
        ).fetchone()
        if row is None:
            return None
        values = dict(zip(columns, row, strict=True))
        payload = WorkflowStore._strict_json_object(values.pop('payload'), 'proof payload')
        if set(payload) != set(values) | {'baseline_digest', 'trusted_proof', 'payload'}:
            raise WorkflowStoreConflict('Strict proof has an invalid persisted schema', 'UNTRUSTED')
        if any(
            type(payload.get(name)) is not type(value) or payload.get(name) != value
            for name, value in values.items()
        ):
            raise WorkflowStoreConflict('Strict proof metadata does not match its canonical payload', 'UNTRUSTED')
        if type(values['source_seq']) is not int or values['source_seq'] < 1:
            raise WorkflowStoreConflict('Strict proof source sequence is malformed', 'UNTRUSTED')
        if type(values['lease_generation']) is not int or values['lease_generation'] < 1:
            raise WorkflowStoreConflict('Strict proof lease generation is malformed', 'UNTRUSTED')
        try:
            expected_proof_id = WorkflowStore.strict_proof_identifier(payload)
        except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowStoreConflict('Strict proof identity is malformed', 'UNTRUSTED') from exc
        if payload.get('proof_id') != expected_proof_id:
            raise WorkflowStoreConflict('Strict proof ID is not canonical', 'UNTRUSTED')
        WorkflowStore._strict_attestation(payload)
        return payload

    @staticmethod
    def _read_strict_proof_by_event(db: sqlite3.Connection, event_id: str) -> Payload | None:
        row = db.execute('SELECT proof_id FROM strict_proofs WHERE source_event_id=?', (event_id,)).fetchone()
        return WorkflowStore._read_strict_proof(db, row[0]) if row is not None else None

    def read_strict_snapshot(
        self,
        goal_id: str,
        candidate_event_id: str | None = None,
        candidate_baseline_digest: str | None = None,
        candidate_evidence_id: str | None = None,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> Payload:
        """Read all strict heads, proofs, events and projections from one read-only snapshot."""
        transaction = self._read_transaction() if _connection is None else nullcontext(_connection)
        with transaction as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 2:
                raise ContractValidationError('Strict workflow requires schema version 2')
            goal = self._strict_get(db, 'goal', goal_id)
            baseline_head = self._read_strict_head(db, ('baseline', goal_id, '', 'current'))
            if baseline_head is None and goal is not None and goal.get('strict_baseline_proof_id') is not None:
                raise WorkflowStoreConflict('Strict goal references a missing baseline head', 'UNTRUSTED')
            baseline_proof = self._read_strict_proof(db, baseline_head['proof_id']) if baseline_head else None
            source_events: dict[str, Payload] = {}
            if baseline_head is not None:
                if baseline_proof is None:
                    raise WorkflowStoreConflict('Strict baseline head references a missing proof', 'UNTRUSTED')
                if goal is None or goal.get('strict_baseline_proof_id') != baseline_head['proof_id']:
                    raise WorkflowStoreConflict('Strict baseline head and goal projection do not match', 'UNTRUSTED')
                source_events[baseline_proof['source_event_id']] = self._read_strict_source_event(
                    db, baseline_proof['source_event_id'], 'baseline'
                )
            baseline_digest = (
                baseline_proof.get('baseline_digest') if baseline_proof is not None
                else goal.get('baseline_digest') if goal is not None else None
            )
            baseline_projection = (
                self._strict_get(db, 'baseline', baseline_digest) if isinstance(baseline_digest, str) else None
            )
            candidate_baseline_projection = (
                self._strict_get(db, 'baseline', candidate_baseline_digest)
                if candidate_baseline_digest is not None else baseline_projection
            )
            if baseline_proof is not None:
                baseline_data = baseline_proof['payload'].get('baseline')
                if (
                    baseline_projection is None
                    or not isinstance(baseline_data, Mapping)
                    or self.fingerprint(baseline_projection) != self.fingerprint(baseline_data)
                    or self.fingerprint(baseline_projection) != baseline_proof['baseline_digest']
                    or goal is None
                    or goal.get('baseline_digest') != baseline_proof['baseline_digest']
                ):
                    raise WorkflowStoreConflict('Strict baseline projection does not match its proof', 'UNTRUSTED')
            evidence_heads: list[Payload] = []
            evidence_proofs: list[Payload] = []
            evidence_projections: dict[str, Payload | None] = {}
            evidence_projection_kinds: dict[str, str] = {}
            validated_strict_evidence: dict[str, tuple[Payload, Payload, Payload, Payload, bool]] = {}
            legacy_key_identities = self._legacy_evidence_key_identities(db)
            if baseline_proof is not None:
                rows = db.execute(
                    'SELECT kind,goal_id,scope_digest,subject_id,proof_id,source_seq,generation '
                    'FROM strict_heads WHERE kind=? AND goal_id=? AND scope_digest=? ORDER BY subject_id',
                    ('evidence', goal_id, baseline_proof['baseline_digest']),
                ).fetchall()
                for row in rows:
                    head = self._strict_head_payload(
                        row,
                        ('kind', 'goal_id', 'scope_digest', 'subject_id', 'proof_id', 'source_seq', 'generation'),
                    )
                    proof = self._read_strict_proof(db, head['proof_id'])
                    if proof is None:
                        raise WorkflowStoreConflict('Strict evidence head references a missing proof', 'UNTRUSTED')
                    if (
                        proof.get('kind') != 'evidence'
                        or proof.get('goal_id') != goal_id
                        or proof.get('scope_digest') != baseline_proof['baseline_digest']
                        or proof.get('subject_id') != head['subject_id']
                        or proof.get('source_seq') != head['source_seq']
                    ):
                        raise WorkflowStoreConflict('Strict evidence head and proof do not match', 'UNTRUSTED')
                    self._read_strict_head_history(db, head)
                    evidence_heads.append(head)
                    evidence_proofs.append(proof)
                    strict_key = self.strict_evidence_key(goal_id, head['subject_id'])
                    strict_projection = self._strict_get(db, 'strict_evidence', strict_key)
                    legacy_key = self.legacy_evidence_key(goal_id, head['subject_id'])
                    legacy_projection = self._strict_get(db, 'evidence', legacy_key)
                    if strict_projection is not None:
                        strict_proof, strict_head, strict_event, active = self._validate_evidence_projection_record(
                            db, goal_id, strict_key, strict_projection, strict_namespace=True,
                            legacy_key_identities=legacy_key_identities,
                        )
                        if (
                            not active
                            or strict_proof['proof_id'] != proof['proof_id']
                            or strict_head['proof_id'] != head['proof_id']
                            or strict_head['scope_digest'] != baseline_proof['baseline_digest']
                        ):
                            raise WorkflowStoreConflict(
                                'Strict evidence record does not match its active head', 'UNTRUSTED'
                            )
                        validated_strict_evidence[strict_key] = (
                            strict_projection, strict_proof, strict_head, strict_event, active
                        )
                        source_events[strict_event['event_id']] = strict_event
                        projection = strict_projection
                        evidence_projection_kinds[head['subject_id']] = 'strict_evidence'
                    else:
                        if legacy_projection is None:
                            raise WorkflowStoreConflict(
                                'Strict evidence projection does not match its proof', 'UNTRUSTED'
                            )
                        legacy_proof, legacy_head, legacy_event, active = self._validate_evidence_projection_record(
                            db, goal_id, legacy_key, legacy_projection, strict_namespace=False,
                            legacy_key_identities=legacy_key_identities,
                        )
                        if (
                            not active
                            or legacy_proof['proof_id'] != proof['proof_id']
                            or legacy_head['proof_id'] != head['proof_id']
                            or legacy_head['scope_digest'] != baseline_proof['baseline_digest']
                        ):
                            raise WorkflowStoreConflict('Legacy strict evidence is not the active proof', 'UNTRUSTED')
                        source_events[legacy_event['event_id']] = legacy_event
                        projection = legacy_projection
                        evidence_projection_kinds[head['subject_id']] = 'evidence'
                    evidence_projections[head['subject_id']] = projection
            candidate_strict_evidence_projection = None
            candidate_legacy_evidence_projection = None
            if candidate_evidence_id is not None:
                candidate_strict_evidence_projection = self._strict_get(
                    db, 'strict_evidence', self.strict_evidence_key(goal_id, candidate_evidence_id)
                )
                raw_legacy_candidate = self._strict_get(
                    db, 'evidence', self.legacy_evidence_key(goal_id, candidate_evidence_id)
                )
                if (
                    isinstance(raw_legacy_candidate, Mapping)
                    and raw_legacy_candidate.get('goal_id') == goal_id
                    and isinstance(raw_legacy_candidate.get('snapshot'), Mapping)
                    and raw_legacy_candidate['snapshot'].get('evidence_id') == candidate_evidence_id
                ):
                    candidate_legacy_evidence_projection = raw_legacy_candidate
                if candidate_evidence_id not in evidence_projections:
                    candidate_projection = candidate_strict_evidence_projection
                    if (
                        candidate_projection is None
                        and candidate_legacy_evidence_projection is not None
                    ):
                        candidate_projection = candidate_legacy_evidence_projection
                    evidence_projections[candidate_evidence_id] = candidate_projection
            if baseline_head is not None and baseline_proof is not None:
                self._read_strict_head_history(db, baseline_head)
            evidence_head_by_id = {head['subject_id']: head for head in evidence_heads}
            legacy_prefix = goal_id + ':'
            for key, raw in db.execute("SELECT key,payload FROM records WHERE kind='evidence' ORDER BY key"):
                if not key.startswith(legacy_prefix):
                    continue
                projection = self._strict_json_object(raw, 'evidence projection')
                strict_proof_id = projection.get('strict_proof_id')
                if strict_proof_id is None:
                    continue
                if projection.get('goal_id') != goal_id:
                    continue
                snapshot_data = projection.get('snapshot')
                if not isinstance(snapshot_data, Mapping) or not isinstance(snapshot_data.get('evidence_id'), str):
                    raise WorkflowStoreConflict('Strict evidence projection has no evidence identity', 'UNTRUSTED')
                evidence_id = snapshot_data['evidence_id']
                if key != self.legacy_evidence_key(goal_id, evidence_id):
                    raise WorkflowStoreConflict('Legacy strict evidence key does not match its payload', 'UNTRUSTED')
                historical_proof, historical_head, historical_event, active = (
                    self._validate_evidence_projection_record(
                        db, goal_id, key, projection, strict_namespace=False,
                        legacy_key_identities=legacy_key_identities,
                    )
                )
                source_events[historical_event['event_id']] = historical_event
                current_scope = baseline_proof['baseline_digest'] if baseline_proof is not None else None
                if historical_proof['scope_digest'] == current_scope:
                    current_head = evidence_head_by_id.get(evidence_id)
                    if current_head is None:
                        raise WorkflowStoreConflict(
                            'Legacy strict evidence has no head for the current baseline', 'UNTRUSTED'
                        )
                    if active:
                        if evidence_projection_kinds.get(evidence_id) == 'evidence':
                            continue
                        raise WorkflowStoreConflict(
                            'Legacy and strict evidence projections both claim the active proof', 'UNTRUSTED'
                        )
                    if historical_proof['source_seq'] >= current_head['source_seq']:
                        raise WorkflowStoreConflict(
                            'Historical legacy evidence is not older than its active head', 'UNTRUSTED'
                        )
                    continue
                if baseline_proof is None:
                    raise WorkflowStoreConflict(
                        'Strict evidence exists without a verified baseline', 'UNTRUSTED'
                    )
                if not active and historical_proof['source_seq'] >= historical_head['source_seq']:
                    raise WorkflowStoreConflict('Historical evidence sequence is inconsistent', 'UNTRUSTED')
            strict_prefix = self.strict_evidence_key_prefix(goal_id)
            for key, raw in db.execute(
                "SELECT key,payload FROM records WHERE kind='strict_evidence' ORDER BY key"
            ):
                key_belongs_to_goal = isinstance(key, str) and key.startswith(strict_prefix)
                validated = validated_strict_evidence.get(key) if key_belongs_to_goal else None
                try:
                    projection = (
                        validated[0]
                        if validated is not None
                        else self._strict_json_object(raw, 'strict evidence projection')
                    )
                except WorkflowStoreConflict:
                    if key_belongs_to_goal:
                        raise
                    continue
                payload_belongs_to_goal = projection.get('goal_id') == goal_id
                if not key_belongs_to_goal and not payload_belongs_to_goal:
                    continue
                if not key_belongs_to_goal or not payload_belongs_to_goal:
                    raise WorkflowStoreConflict(
                        'Strict evidence key owner does not match its goal', 'UNTRUSTED'
                    )
                snapshot_data = projection.get('snapshot')
                if not isinstance(snapshot_data, Mapping) or not isinstance(snapshot_data.get('evidence_id'), str):
                    raise WorkflowStoreConflict('Strict evidence projection has no evidence identity', 'UNTRUSTED')
                evidence_id = snapshot_data['evidence_id']
                if validated is None:
                    strict_proof, strict_head, strict_event, active = (
                        self._validate_evidence_projection_record(
                            db, goal_id, key, projection, strict_namespace=True,
                            legacy_key_identities=legacy_key_identities,
                        )
                    )
                else:
                    _, strict_proof, strict_head, strict_event, active = validated
                    history = self._read_strict_head_history(db, strict_head)
                    if (
                        key != self.strict_evidence_key(goal_id, evidence_id)
                        or strict_proof.get('kind') != 'evidence'
                        or strict_proof.get('goal_id') != goal_id
                        or strict_proof.get('subject_id') != evidence_id
                        or strict_head.get('kind') != 'evidence'
                        or strict_head.get('goal_id') != goal_id
                        or strict_head.get('subject_id') != evidence_id
                        or strict_head.get('scope_digest') != strict_proof.get('scope_digest')
                        or strict_head.get('proof_id') != strict_proof.get('proof_id')
                        or strict_head.get('source_seq') != strict_proof.get('source_seq')
                        or strict_event.get('event_id') != strict_proof.get('source_event_id')
                        or strict_event.get('seq') != strict_proof.get('source_seq')
                        or not any(
                            item['proof_id'] == strict_proof.get('proof_id')
                            and item['source_seq'] == strict_proof.get('source_seq')
                            for item in history
                        )
                        or not active
                    ):
                        raise WorkflowStoreConflict(
                            'Cached strict evidence validation no longer matches its active head', 'UNTRUSTED'
                        )
                source_events[strict_event['event_id']] = strict_event
                current_scope = baseline_proof['baseline_digest'] if baseline_proof is not None else None
                if strict_proof['scope_digest'] == current_scope:
                    active_current_head = evidence_head_by_id.get(evidence_id)
                    if (
                        not active
                        or active_current_head is None
                        or strict_head['proof_id'] != active_current_head['proof_id']
                        or evidence_projection_kinds.get(evidence_id) != 'strict_evidence'
                    ):
                        raise WorkflowStoreConflict(
                            'Strict evidence projection is orphaned from its active head', 'UNTRUSTED'
                        )
            proof_records = ([baseline_proof] if baseline_proof is not None else []) + evidence_proofs
            event_ids = {item['source_event_id'] for item in proof_records}
            if candidate_event_id is not None:
                event_ids.add(candidate_event_id)
            for event_id in sorted(event_ids):
                if event_id in source_events:
                    continue
                row = db.execute(
                    'SELECT seq,event_id,payload FROM events WHERE event_id=?', (event_id,)
                ).fetchone()
                if row is not None:
                    source_events[event_id] = {
                        'seq': row[0], 'event_id': row[1],
                        'payload': self._strict_source_event(
                            self._strict_json_object(row[2], 'source event')
                        ),
                    }
            for proof in proof_records:
                source = source_events.get(proof['source_event_id'])
                if (
                    source is None
                    or source['event_id'] != proof['source_event_id']
                    or source['seq'] != proof['source_seq']
                ):
                    raise WorkflowStoreConflict('Strict proof source event is missing or moved', 'UNTRUSTED')
                event_payload = self._strict_source_event(source['payload'])
                if (
                    event_payload.get('event_id') != source['event_id']
                    or self.fingerprint(event_payload) != proof['source_event_digest']
                ):
                    raise WorkflowStoreConflict('Strict proof source event digest does not match', 'UNTRUSTED')
                self._strict_attestation(proof, event_payload)
            if baseline_proof is not None:
                if baseline_head is None:
                    raise WorkflowStoreConflict('Strict baseline proof has no head', 'UNTRUSTED')
                self._validate_strict_goal_record(db, goal, baseline_head, baseline_proof)
            candidate_proof = None
            if candidate_event_id is not None:
                candidate_event = source_events.get(candidate_event_id)
                candidate_proof = self._read_strict_proof_by_event(db, candidate_event_id)
                if candidate_event is None:
                    if candidate_proof is not None:
                        raise WorkflowStoreConflict('Strict candidate proof has no source event', 'UNTRUSTED')
                else:
                    if candidate_proof is None:
                        raise WorkflowStoreConflict('Strict candidate event has no persisted proof', 'UNTRUSTED')
                    candidate_payload = self._strict_source_event(candidate_event['payload'])
                    if (
                        candidate_event['event_id'] != candidate_event_id
                        or candidate_event['seq'] != candidate_proof['source_seq']
                        or candidate_proof['source_event_id'] != candidate_event_id
                        or self.fingerprint(candidate_payload) != candidate_proof['source_event_digest']
                    ):
                        raise WorkflowStoreConflict('Strict candidate proof and event do not match', 'UNTRUSTED')
                    self._strict_attestation(candidate_proof, candidate_payload)
            return {
                'goal': goal,
                'baseline_head': baseline_head,
                'baseline_proof': baseline_proof,
                'baseline_projection': baseline_projection,
                'candidate_baseline_projection': candidate_baseline_projection,
                'evidence_heads': evidence_heads,
                'evidence_proofs': evidence_proofs,
                'evidence_projections': evidence_projections,
                'candidate_strict_evidence_projection': candidate_strict_evidence_projection,
                'candidate_legacy_evidence_projection': candidate_legacy_evidence_projection,
                'candidate_evidence_projection': (
                    evidence_projections.get(candidate_evidence_id) if candidate_evidence_id is not None else None
                ),
                'source_events': source_events,
                'candidate_event': source_events.get(candidate_event_id) if candidate_event_id else None,
                'candidate_proof': candidate_proof,
            }

    def _read_strict_baseline_snapshot(self, db: sqlite3.Connection, goal_id: str) -> Payload:
        """Read the validated baseline projection using the caller's SQLite snapshot."""
        return self.read_strict_snapshot(goal_id, _connection=db)

    @staticmethod
    def _preflight_strict_event_history(db: sqlite3.Connection) -> None:
        """Bound rows and serialized bytes before loading Baseline/evidence projections."""
        row = db.execute(
            'SELECT COUNT(*), COALESCE(SUM(length(CAST(payload AS BLOB))), 0), '
            'COALESCE(MAX(length(CAST(payload AS BLOB))), 0) FROM events'
        ).fetchone()
        if (
            row is None
            or len(row) != 3
            or any(type(value) is not int or value < 0 for value in row)
        ):
            raise WorkflowStoreConflict('Strict event-history limits are malformed', 'UNTRUSTED')
        if (
            row[0] > _STRICT_EVENT_HISTORY_MAX_ROWS
            or row[1] > _STRICT_EVENT_HISTORY_MAX_BYTES
            or row[2] > _STRICT_EVENT_HISTORY_MAX_ROW_BYTES
        ):
            raise WorkflowStoreConflict('Strict event history exceeds the supported size limits', 'UNAVAILABLE')

    def read_strict_question_snapshot(self, goal_id: str) -> Payload:
        """Read Baseline, Strict questions, heads, and creation events from one read-only snapshot."""
        with self._read_transaction() as db:
            self._preflight_strict_event_history(db)
            baseline_snapshot = self._read_strict_baseline_snapshot(db, goal_id)
            baseline_proof = baseline_snapshot.get('baseline_proof')
            baseline_digest = (
                baseline_proof.get('baseline_digest') if isinstance(baseline_proof, Mapping) else None
            )
            baseline_projection = baseline_snapshot.get('baseline_projection')
            raw_dependencies = (
                baseline_projection.get('decision_dependencies', {})
                if isinstance(baseline_projection, Mapping) else {}
            )
            if not isinstance(raw_dependencies, Mapping):
                raise WorkflowStoreConflict('Strict Baseline dependencies are malformed', 'UNTRUSTED')
            active_head_keys = {
                self.fingerprint({
                    'schema': 'strict-direction-question-head/v1',
                    'goal_id': goal_id,
                    'baseline_digest': baseline_digest,
                    'decision_key': decision_key,
                    'kind': 'DIRECTION',
                })
                for decision_key in raw_dependencies
                if isinstance(decision_key, str)
            } if isinstance(baseline_digest, str) else set()
            question_heads: list[Payload] = []
            referenced_question_ids: set[str] = set()
            for key, raw in db.execute(
                "SELECT key,payload FROM records WHERE kind='strict_question_request_index' ORDER BY key"
            ):
                head = self._strict_json_object(raw, 'Strict question head')
                active_key = key in active_head_keys
                if (
                    not active_key
                    and (not isinstance(baseline_digest, str) or head.get('baseline_digest') != baseline_digest)
                ):
                    continue
                question_heads.append({'key': key, 'head': head, 'referenced': active_key})
                question_id = head.get('question_id')
                if isinstance(question_id, str):
                    referenced_question_ids.add(question_id)

            question_events: list[Payload] = []
            event_history: list[Payload] = []
            for seq, event_id, raw in db.execute('SELECT seq,event_id,payload FROM events ORDER BY seq'):
                if type(seq) is not int or seq < 1:
                    raise WorkflowStoreConflict('Signed workflow event sequence is malformed', 'UNTRUSTED')
                if not isinstance(event_id, str) or not event_id:
                    raise WorkflowStoreConflict('Signed workflow event row ID is malformed', 'UNTRUSTED')
                if event_id.startswith('store:'):
                    event_data = self._strict_json_object(raw, 'workflow event')
                    if 'signature' in event_data:
                        raise WorkflowStoreConflict('Signed workflow event uses a reserved store ID', 'UNTRUSTED')
                    action = event_data.get('action')
                    if (
                        set(event_data) != {'action', 'record', 'at'}
                        or not isinstance(action, str)
                        or action not in {'reserve', 'consume', 'revoke', 'finish', 'recover'}
                        or not isinstance(event_data.get('record'), Mapping)
                        or not isinstance(event_data.get('at'), str)
                    ):
                        raise WorkflowStoreConflict('Internal workflow journal row is malformed', 'UNTRUSTED')
                    try:
                        parse_time(event_data['at'])
                    except ContractValidationError as exc:
                        raise WorkflowStoreConflict(
                            'Internal workflow journal timestamp is malformed', 'UNTRUSTED'
                        ) from exc
                    continue

                if isinstance(raw, str):
                    try:
                        raw_bytes = raw.encode('utf-8')
                    except UnicodeEncodeError as exc:
                        raise WorkflowStoreConflict(
                            'Signed workflow event storage bytes are malformed', 'UNTRUSTED'
                        ) from exc
                elif isinstance(raw, bytes):
                    raw_bytes = raw
                else:
                    raise WorkflowStoreConflict('Signed workflow event storage bytes are malformed', 'UNTRUSTED')
                payload_digest = hashlib.sha256(raw_bytes).hexdigest()
                event = self._cached_strict_source_event(seq, event_id, payload_digest)
                if event is None:
                    event_data = self._strict_json_object(raw, 'workflow event')
                    if 'signature' not in event_data:
                        candidate_payload = event_data.get('payload')
                        claims_active_baseline = (
                            isinstance(candidate_payload, Mapping)
                            and (
                                candidate_payload.get('baseline_digest') == baseline_digest
                                or candidate_payload.get('baseline_proof_id') == (
                                    baseline_proof.get('proof_id')
                                    if isinstance(baseline_proof, Mapping) else None
                                )
                            )
                        )
                        if (
                            any(name in event_data for name in ('event_id', 'source', 'subject', 'created_at'))
                            or event_data.get('kind') == 'controller.question.created'
                            or claims_active_baseline
                        ):
                            raise WorkflowStoreConflict(
                                'Signed workflow event is missing its signature', 'UNTRUSTED'
                            )
                        # Legacy non-source event envelopes have no signed-event shape
                        # and cannot authorize or resolve a Strict question.
                        continue
                    try:
                        event = SignedEvent.from_dict(event_data)
                    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
                        raise WorkflowStoreConflict('Strict source event is malformed', 'UNTRUSTED') from exc
                    if event.event_id != event_id:
                        raise WorkflowStoreConflict('Signed workflow event ID does not match its row', 'UNTRUSTED')
                    event = self._cache_strict_source_event(
                        seq, event_id, payload_digest, event, len(raw_bytes)
                    )
                elif event.event_id != event_id:
                    raise WorkflowStoreConflict('Signed workflow event ID does not match its row', 'UNTRUSTED')

                history_row = {
                    'seq': seq,
                    'event_id': event_id,
                    'event': event,
                    'payload_digest': payload_digest,
                }
                event_history.append(history_row)
                if event.kind != 'controller.question.created':
                    continue
                payload = event.payload
                if not isinstance(payload, Mapping):
                    raise WorkflowStoreConflict('Strict question event payload is malformed', 'UNTRUSTED')
                # Preserve the question-event mapping shape while the full signed history
                # carries the immutable parsed model for StrictWorkflow verification.
                question_events.append({**history_row, 'event': event.to_dict()})
                if payload.get('baseline_digest') == baseline_digest:
                    question_id = payload.get('question_id')
                    if isinstance(question_id, str):
                        referenced_question_ids.add(question_id)

            questions: list[Payload] = []
            for key, raw in db.execute(
                "SELECT key,payload FROM records WHERE kind='strict_question' ORDER BY key"
            ):
                envelope = self._strict_json_object(raw, 'Strict question envelope')
                record = envelope.get('record')
                question_data = record.get('question') if isinstance(record, Mapping) else None
                outer_question_id = envelope.get('question_id')
                inner_question_id = question_data.get('question_id') if isinstance(question_data, Mapping) else None
                outer_id_referenced = (
                    isinstance(outer_question_id, str) and outer_question_id in referenced_question_ids
                )
                inner_id_referenced = (
                    isinstance(inner_question_id, str) and inner_question_id in referenced_question_ids
                )
                relevant = (
                    key in referenced_question_ids
                    or outer_id_referenced
                    or inner_id_referenced
                    or envelope.get('baseline_digest') == baseline_digest
                    or (
                        isinstance(question_data, Mapping)
                        and question_data.get('baseline_digest') == baseline_digest
                    )
                )
                if not relevant:
                    continue
                if envelope.get('question_id') != key:
                    raise WorkflowStoreConflict('Strict question key does not match its payload', 'UNTRUSTED')
                questions.append({
                    'key': key,
                    'envelope': envelope,
                    'referenced': key in referenced_question_ids
                    or outer_id_referenced
                    or inner_id_referenced,
                })

            return {
                **baseline_snapshot,
                'baseline_digest': baseline_digest,
                'questions': tuple(questions),
                'question_heads': tuple(question_heads),
                'question_events': tuple(question_events),
                'event_history': tuple(event_history),
            }

    def commit_strict_question(
        self,
        changes: Sequence[tuple[str, str, Payload]],
        expected: Expected,
        event_id: str,
        event: Mapping[str, Any],
        expected_baseline_head: Mapping[str, Any],
        lease_factory: Callable[[str], Any],
        expected_runtime_digest: str,
        expected_lease_generation: int,
        *,
        valid_until: str,
        clock: Callable[[], str],
    ) -> bool:
        """Atomically create one Strict question/head and append its signed event.

        This deliberately does not use ``commit_many``: a prior event ID must not
        short-circuit the expected-record CAS for question creation.
        """
        if not isinstance(event_id, str) or not event_id or event_id.startswith('store:'):
            raise WorkflowStoreConflict('Invalid or reserved question event ID', 'UNTRUSTED')
        signed_event = SignedEvent.from_dict(event)
        if signed_event.event_id != event_id or signed_event.kind != 'controller.question.created':
            raise WorkflowStoreConflict('Strict question creation event is malformed', 'UNTRUSTED')
        encoded_event = _json(signed_event.to_dict())
        if len(changes) != 2:
            raise ContractValidationError('Strict question creation requires one record and one head')
        by_kind: dict[str, tuple[str, Payload]] = {}
        goal_ids: set[str] = set()
        for kind, key, payload in changes:
            if kind not in {'strict_question', 'strict_question_request_index'} or kind in by_kind:
                raise ContractValidationError('Strict question creation has invalid record kinds')
            if not isinstance(key, str) or not key or not isinstance(payload, dict):
                raise ContractValidationError('Strict question records require keys and object payloads')
            if (kind, key) not in expected:
                raise ContractValidationError('Strict question record is missing an expected version')
            if payload.get('goal_id') is not None:
                goal_ids.add(payload['goal_id'])
            by_kind[kind] = (key, payload)
        if set(by_kind) != {'strict_question', 'strict_question_request_index'} or len(goal_ids) != 1:
            raise ContractValidationError('Strict question creation records do not share one goal')
        goal_id = next(iter(goal_ids))
        question_key, question = by_kind['strict_question']
        if question.get('question_id') != question_key:
            raise WorkflowStoreConflict('Strict question key does not match its payload', 'UNTRUSTED')
        if expected_baseline_head.get('kind') != 'baseline' or expected_baseline_head.get('goal_id') != goal_id:
            raise WorkflowStoreConflict('Expected strict baseline head is malformed', 'UNTRUSTED')

        with self._strict_transaction(
            lease_factory,
            expected_runtime_digest,
            expected_lease_generation,
            valid_until,
            clock,
        ) as (db, _lease):
            if self._strict_get(db, 'strict_question', question_key) is not None:
                raise WorkflowStoreConflict('Strict question ID is already in use', 'CONFLICT')
            self._strict_check(db, expected)
            current_baseline_head = self._read_strict_head(db, ('baseline', goal_id, '', 'current'))
            if not self.same_json(current_baseline_head, dict(expected_baseline_head)):
                raise WorkflowStoreConflict('Strict baseline head changed during question creation', 'STALE')
            existing_event = db.execute(
                'SELECT payload FROM events WHERE event_id=?', (event_id,)
            ).fetchone()
            if existing_event is not None:
                raise WorkflowStoreConflict('Strict question event ID is already in use', 'CONFLICT')
            for kind in ('strict_question', 'strict_question_request_index'):
                key, payload = by_kind[kind]
                self._put(db, kind, key, payload)
            self._strict_touch_goal(db, goal_id)
            db.execute('INSERT INTO events(event_id,payload) VALUES(?,?)', (event_id, encoded_event))
        return True

    @contextmanager
    def _strict_transaction(
        self,
        lease_factory: Callable[[str], Any],
        expected_runtime_digest: str,
        expected_lease_generation: int,
        valid_until: str,
        clock: Callable[[], str],
    ) -> Iterator[tuple[sqlite3.Connection, Payload]]:
        committed = False
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise ContractValidationError('SQLite foreign-key enforcement is unavailable')
            db.execute('PRAGMA recursive_triggers=ON')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            if _time(clock()) >= _time(valid_until):
                raise WorkflowStoreConflict('Strict proof expired while waiting for the write lock', 'STALE')
            try:
                with lease_factory(expected_runtime_digest) as lease:
                    if not isinstance(lease, Mapping):
                        raise WorkflowStoreConflict('Runtime lease is malformed', 'UNTRUSTED')
                    lease_binding = lease.get('binding_digest')
                    lease_generation = lease.get('generation')
                    if (
                        not isinstance(lease_binding, str)
                        or not lease_binding
                        or type(lease_generation) is not int
                        or lease_generation < 1
                    ):
                        raise WorkflowStoreConflict('Runtime lease is malformed', 'UNTRUSTED')
                    if (
                        lease_binding != expected_runtime_digest
                        or lease_generation != expected_lease_generation
                    ):
                        raise WorkflowStoreConflict('Runtime lease does not match the strict proof', 'STALE')
                    if _time(clock()) >= _time(valid_until):
                        raise WorkflowStoreConflict('Strict proof expired before writes', 'STALE')
                    yield db, dict(lease)
                    if _time(clock()) >= _time(valid_until):
                        raise WorkflowStoreConflict('Strict proof expired before commit', 'STALE')
                    db.commit()
                    committed = True
            except Exception:
                if not committed:
                    raise
                _LOGGER.warning(
                    'Strict transaction committed but runtime lease release failed; committed state is retained'
                )
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def strict_proof_identifier(proof: Mapping[str, Any]) -> str:
        return WorkflowStore.fingerprint({
            'kind': proof['kind'],
            'goal_id': proof['goal_id'],
            'scope_digest': proof['scope_digest'],
            'subject_id': proof['subject_id'],
            'source_event_id': proof['source_event_id'],
            'source_event_digest': proof['source_event_digest'],
            'actor_type': proof['actor_type'],
            'principal_id': proof['principal_id'],
            'account_id': proof['account_id'],
            'session_id': proof['session_id'],
            'policy_digest': proof['policy_digest'],
            'runtime_binding_digest': proof['runtime_binding_digest'],
            'lease_generation': proof['lease_generation'],
        })

    @staticmethod
    def _strict_proof_replay_matches(
        saved: Payload, fresh: Payload, event: Mapping[str, Any] | None = None
    ) -> bool:
        saved_attestation = WorkflowStore._strict_attestation(saved, event)
        fresh_attestation = WorkflowStore._strict_attestation(fresh, event)
        stable_fields = (
            'proof_id', 'source_seq', 'kind', 'goal_id', 'scope_digest', 'subject_id', 'baseline_digest',
            'source_event_id', 'source_event_digest', 'actor_type', 'principal_id', 'account_id', 'session_id',
            'policy_digest', 'runtime_binding_digest', 'lease_generation',
        )
        if any(saved.get(name) != fresh.get(name) for name in stable_fields):
            return False
        immutable_attestation = (
            'actor_type', 'principal_id', 'account_id', 'session_id', 'source_event_id',
            'purpose', 'event_kind', 'subject', 'binding_digest',
        )
        if any(saved_attestation.get(name) != fresh_attestation.get(name) for name in immutable_attestation):
            return False
        try:
            saved_payload = WorkflowStore._strict_json_object(_json(saved['payload']), 'replay payload')
            fresh_payload = WorkflowStore._strict_json_object(_json(fresh['payload']), 'replay payload')
            projection_key, identity_key = (
                ('goal_projection', 'trusted_identity')
                if saved['kind'] == 'baseline'
                else ('projection', 'trusted_source')
            )
            for payload in (saved_payload, fresh_payload):
                projection = payload.get(projection_key)
                if not isinstance(projection, dict):
                    raise WorkflowStoreConflict('Malformed strict replay projection', 'UNTRUSTED')
                trusted_identity = projection.get(identity_key)
                if not isinstance(trusted_identity, dict):
                    raise WorkflowStoreConflict('Malformed strict replay identity', 'UNTRUSTED')
                trusted_identity.pop('proof_digest', None)
            return WorkflowStore.same_json(saved_payload, fresh_payload)
        except WorkflowStoreConflict:
            raise
        except (KeyError, TypeError, ValueError, ContractValidationError) as exc:
            raise WorkflowStoreConflict('Malformed strict replay payload', 'UNTRUSTED') from exc

    def commit_strict(
        self,
        changes: Sequence[tuple[str, str, Payload]],
        expected: Expected,
        event_id: str,
        event: Payload,
        proof: Payload,
        expected_head: Payload | None,
        lease_factory: Callable[[str], Any],
        *,
        valid_until: str,
        clock: Callable[[], str],
    ) -> tuple[bool, Payload]:
        """Atomically append a strict event proof, advance its head and write projections under a host lease."""
        required = {
            'kind', 'goal_id', 'scope_digest', 'subject_id', 'baseline_digest', 'source_event_id',
            'source_event_digest', 'proof_digest', 'actor_type', 'principal_id', 'account_id', 'session_id',
            'expires_at', 'policy_digest', 'runtime_binding_digest', 'lease_generation', 'trusted_proof', 'payload',
        }
        if not isinstance(event_id, str) or not event_id or event_id.startswith('store:'):
            raise ContractValidationError('Invalid or reserved strict source event ID')
        if set(proof) != required or proof['source_event_id'] != event_id:
            raise ContractValidationError('Strict proof has an invalid schema or event binding')
        if proof['kind'] not in {'baseline', 'evidence'}:
            raise WorkflowStoreConflict('Unsupported strict proof kind', 'UNTRUSTED')
        event = self._strict_source_event(event)
        if self.fingerprint(event) != proof['source_event_digest']:
            raise ContractValidationError('Strict proof source event digest mismatch')
        self._strict_attestation(proof, event)
        encoded_event = _json(event)
        encoded_proof_base = _json(proof)
        expected_runtime_digest = proof['runtime_binding_digest']
        lease_generation = proof['lease_generation']
        if not isinstance(lease_generation, int) or isinstance(lease_generation, bool) or lease_generation < 1:
            raise ContractValidationError('Strict proof lease generation must be positive')
        _time(valid_until)
        _time(proof['expires_at'])
        allowed_change_kinds = (
            {'baseline', 'goal'} if proof['kind'] == 'baseline' else {'strict_evidence'}
        )
        required_change_kinds = allowed_change_kinds
        actual_change_kinds = {kind for kind, _key, _payload in changes}
        if len(changes) != len(required_change_kinds) or actual_change_kinds != required_change_kinds:
            raise WorkflowStoreConflict('Strict proof requires a complete projection set', 'UNTRUSTED')
        expected_proof_id = self.strict_proof_identifier(proof)
        for kind, key, payload in changes:
            if not isinstance(payload, Mapping):
                raise WorkflowStoreConflict('Strict projection is not an object', 'UNTRUSTED')
            if kind not in allowed_change_kinds:
                raise WorkflowStoreConflict('Strict proof cannot write this projection kind', 'UNTRUSTED')
            if payload.get('goal_id') != proof['goal_id']:
                raise WorkflowStoreConflict('Strict projection is not bound to the proof goal', 'UNTRUSTED')
            if kind == 'goal' and key != proof['goal_id']:
                raise WorkflowStoreConflict('Strict goal projection key does not match its goal ID', 'UNTRUSTED')
            if kind == 'goal':
                proof_payload = proof.get('payload')
                goal_projection = (
                    proof_payload.get('goal_projection') if isinstance(proof_payload, Mapping) else None
                )
                baseline_data = proof_payload.get('baseline') if isinstance(proof_payload, Mapping) else None
                if not isinstance(goal_projection, Mapping) or not isinstance(baseline_data, Mapping):
                    raise WorkflowStoreConflict('Strict goal proof payload is incomplete', 'UNTRUSTED')
                try:
                    baseline = Baseline.from_dict(baseline_data)
                except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
                    raise WorkflowStoreConflict('Strict baseline proof payload is malformed', 'UNTRUSTED') from exc
                projection_fields = {
                    'goal_id', 'revision', 'baseline', 'baseline_digest', 'policy_digest', 'baseline_event_id',
                    'strict_baseline_proof_id', 'runtime_binding_digest', 'trusted_identity',
                }
                trusted_identity = goal_projection.get('trusted_identity')
                if (
                    set(goal_projection) != projection_fields
                    or not isinstance(trusted_identity, Mapping)
                    or set(trusted_identity) != {
                        'principal_id', 'account_id', 'session_id', 'source_event_id', 'proof_digest',
                    }
                    or not self.same_json(baseline_data, baseline.to_dict())
                    or baseline.goal_id != proof['goal_id']
                    or baseline.fingerprint != proof['baseline_digest']
                    or event.get('kind') != 'user.baseline'
                    or event.get('subject') != baseline.fingerprint
                    or not self.same_json(event.get('payload'), baseline.to_dict())
                    or set(payload) != set(goal_projection) | {
                        'state', 'generation', 'baseline_created_at', 'supersedes',
                    }
                    or not self.same_json({name: payload[name] for name in goal_projection}, goal_projection)
                    or payload.get('state') != 'PLAN'
                    or type(payload.get('generation')) is not int
                    or payload.get('generation', -1) < 0
                    or payload.get('baseline_created_at') != event.get('created_at')
                ):
                    raise WorkflowStoreConflict('Strict goal projection does not reference its proof', 'UNTRUSTED')
                supersedes = payload.get('supersedes')
                if supersedes is not None and (
                    not isinstance(supersedes, str)
                    or len(supersedes) != 64
                    or supersedes != supersedes.lower()
                    or any(character not in '0123456789abcdef' for character in supersedes)
                ):
                    raise WorkflowStoreConflict('Strict supersedes digest is malformed', 'UNTRUSTED')
            if kind == 'baseline' and key != proof['baseline_digest']:
                raise WorkflowStoreConflict('Strict baseline projection key does not match its proof', 'UNTRUSTED')
            if kind == 'baseline':
                baseline_payload = proof.get('payload', {}).get('baseline')
                if (
                    not isinstance(baseline_payload, Mapping)
                    or not self.same_json(payload, baseline_payload)
                ):
                    raise WorkflowStoreConflict(
                        'Strict baseline projection content does not match its proof', 'UNTRUSTED'
                    )
                baseline = Baseline.from_dict(baseline_payload)
                if not self.same_json(baseline_payload, baseline.to_dict()):
                    raise WorkflowStoreConflict('Strict baseline projection is not canonical', 'UNTRUSTED')
            if kind == 'strict_evidence':
                proof_payload = proof.get('payload')
                proof_projection = (
                    proof_payload.get('projection') if isinstance(proof_payload, Mapping) else None
                )
                snapshot = proof_payload.get('snapshot') if isinstance(proof_payload, Mapping) else None
                if (
                    key != self.strict_evidence_key(proof['goal_id'], proof['subject_id'])
                    or not isinstance(proof_projection, Mapping)
                    or set(payload) != {
                        'goal_id', 'snapshot', 'source_event_id', 'strict_proof_id', 'trusted_source',
                    }
                    or not self.same_json(payload, proof_projection)
                    or not isinstance(snapshot, Mapping)
                    or not self.same_json(payload.get('snapshot'), snapshot)
                    or payload.get('strict_proof_id') != expected_proof_id
                    or payload.get('source_event_id') != event_id
                    or not self.same_json(event.get('payload'), {
                        'goal_id': proof['goal_id'], 'snapshot': snapshot,
                    })
                ):
                    raise WorkflowStoreConflict('Strict evidence projection key does not match its proof', 'UNTRUSTED')
        head_key = (proof['kind'], proof['goal_id'], proof['scope_digest'], proof['subject_id'])
        affected_goals = {proof['goal_id']}
        for kind, _key, payload in changes:
            if kind in _GOAL_GENERATION_RECORD_KINDS and payload.get('goal_id'):
                affected_goals.add(payload['goal_id'])
        with self._strict_transaction(
            lease_factory, expected_runtime_digest, lease_generation, valid_until, clock
        ) as (db, _lease):
            old_event = db.execute(
                'SELECT seq,payload FROM events WHERE event_id=?', (event_id,)
            ).fetchone()
            if old_event is not None:
                if old_event[1] != encoded_event:
                    raise WorkflowStoreConflict('Strict source event identity conflict', 'UNTRUSTED')
                source_seq = old_event[0]
                if type(source_seq) is not int or source_seq < 1:
                    raise WorkflowStoreConflict('Strict source event sequence is malformed', 'UNTRUSTED')
                proof_id = self.strict_proof_identifier(proof)
                candidate = {**proof, 'proof_id': proof_id, 'source_seq': source_seq}
                old_proof = self._read_strict_proof_by_event(db, event_id)
                if old_proof is None:
                    raise WorkflowStoreConflict('Strict source event has no persisted proof', 'UNTRUSTED')
                if (
                    old_proof['source_event_id'] != event_id
                    or old_proof['source_seq'] != source_seq
                    or old_proof['source_event_digest'] != self.fingerprint(event)
                ):
                    raise WorkflowStoreConflict('Strict persisted proof does not match its source event', 'UNTRUSTED')
                self._strict_attestation(old_proof, event)
                old_head_key = (
                    old_proof['kind'], old_proof['goal_id'], old_proof['scope_digest'], old_proof['subject_id']
                )
                current_head = self._read_strict_head(db, old_head_key)
                if current_head is None:
                    raise WorkflowStoreConflict('Strict source event proof has no active head', 'UNTRUSTED')
                self._read_strict_head_history(db, current_head)
                if current_head['proof_id'] != old_proof['proof_id']:
                    raise WorkflowStoreConflict('Strict source event proof was superseded', 'STALE')
                if current_head['source_seq'] != old_proof['source_seq']:
                    raise WorkflowStoreConflict('Strict source event head sequence is inconsistent', 'UNTRUSTED')
                if old_proof['kind'] == 'baseline':
                    old_goal = self._strict_get(db, 'goal', old_proof['goal_id'])
                    self._validate_strict_goal_record(db, old_goal, current_head, old_proof)
                else:
                    baseline_head, baseline_proof, _goal, baseline = self._validate_active_baseline(
                        db, old_proof['goal_id']
                    )
                    if (
                        baseline_proof['baseline_digest'] != old_proof['baseline_digest']
                        or old_proof['scope_digest'] != baseline.fingerprint
                    ):
                        raise WorkflowStoreConflict('Strict evidence replay baseline changed', 'STALE')
                    self._validate_active_evidence_record(db, old_proof)
                if not self._strict_proof_replay_matches(old_proof, candidate, event):
                    raise WorkflowStoreConflict('Strict source event replay does not match its proof', 'STALE')
                if _time(old_proof['expires_at']) > _time(candidate['expires_at']):
                    raise WorkflowStoreConflict('Fresh provider proof does not cover the saved proof expiry', 'STALE')
                if _time(clock()) >= _time(old_proof['expires_at']):
                    raise WorkflowStoreConflict('Persisted strict proof has expired', 'STALE')
                return False, old_proof

            missing_goal_cas = sorted(goal for goal in affected_goals if ('goal', goal) not in expected)
            if missing_goal_cas:
                raise ContractValidationError('Every affected goal requires an expected version')
            missing_projection_cas = sorted(
                (kind, key) for kind, key, _payload in changes if (kind, key) not in expected
            )
            if missing_projection_cas:
                raise ContractValidationError('Every strict projection change requires an expected version')
            self._strict_check(db, expected)
            current_head = self._read_strict_head(db, head_key)
            current_digest = self.fingerprint(current_head) if current_head is not None else None
            expected_digest = self.fingerprint(expected_head) if expected_head is not None else None
            if current_digest != expected_digest:
                raise WorkflowStoreConflict('Strict proof head changed concurrently', 'STALE')
            if proof['kind'] == 'baseline':
                self._validate_new_baseline_commit(db, proof, event, changes, current_head)
            else:
                self._validate_new_evidence_commit(db, proof, event, changes)
            cursor = db.execute('INSERT INTO events(event_id,payload) VALUES(?,?)', (event_id, encoded_event))
            inserted_source_seq = cursor.lastrowid
            if inserted_source_seq is None:
                raise WorkflowStoreConflict('SQLite did not return a strict event sequence', 'UNAVAILABLE')
            source_seq = inserted_source_seq
            if current_head is not None and source_seq <= current_head['source_seq']:
                raise WorkflowStoreConflict('Strict proof source sequence did not advance', 'STALE')
            proof_id = self.strict_proof_identifier(proof)
            stored_proof = {**proof, 'proof_id': proof_id, 'source_seq': source_seq}
            db.execute(
                'INSERT INTO strict_proofs('
                'proof_id,kind,goal_id,scope_digest,subject_id,source_event_id,source_seq,source_event_digest,'
                'proof_digest,actor_type,principal_id,account_id,session_id,expires_at,policy_digest,'
                'runtime_binding_digest,lease_generation,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    proof_id, proof['kind'], proof['goal_id'], proof['scope_digest'], proof['subject_id'],
                    event_id, source_seq, proof['source_event_digest'], proof['proof_digest'], proof['actor_type'],
                    proof['principal_id'], proof['account_id'], proof['session_id'], proof['expires_at'],
                    proof['policy_digest'], proof['runtime_binding_digest'], lease_generation, _json(stored_proof),
                ),
            )
            generation = 1 if current_head is None else current_head['generation'] + 1
            if current_head is None:
                db.execute(
                    'INSERT INTO strict_heads(kind,goal_id,scope_digest,subject_id,proof_id,source_seq,generation) '
                    'VALUES(?,?,?,?,?,?,?)', (*head_key, proof_id, source_seq, generation)
                )
            else:
                db.execute(
                    'UPDATE strict_heads SET proof_id=?,source_seq=?,generation=? '
                    'WHERE kind=? AND goal_id=? AND scope_digest=? AND subject_id=?',
                    (proof_id, source_seq, generation, *head_key),
                )
            seen: set[tuple[str, str]] = set()
            for kind, key, payload in changes:
                if (kind, key) in seen:
                    raise ContractValidationError('Duplicate strict projection change')
                seen.add((kind, key))
                self._put(db, kind, key, payload)
                if kind in _GOAL_GENERATION_RECORD_KINDS and payload.get('goal_id'):
                    affected_goals.add(payload['goal_id'])
            for affected_goal in affected_goals:
                self._strict_touch_goal(db, affected_goal)
            if _json(proof) != encoded_proof_base:
                raise ContractValidationError('Strict proof changed during commit')
            return True, stored_proof

    def _strict_check(self, db: sqlite3.Connection, expected: Expected) -> None:
        for (kind, key), digest in expected.items():
            current = self._strict_get(db, kind, key)
            actual = self.fingerprint(current) if current is not None else None
            if actual != digest:
                raise WorkflowStoreConflict('Strict workflow record changed concurrently', 'STALE')

    def _strict_touch_goal(self, db: sqlite3.Connection, goal_id: str) -> None:
        """Advance a goal generation after its strict CAS value has been checked."""
        goal = self._strict_get(db, 'goal', goal_id)
        if goal is not None:
            generation = goal.get('generation', 0)
            if type(generation) is not int or generation < 0:
                raise WorkflowStoreConflict('Strict goal generation is malformed', 'UNTRUSTED')
            goal['generation'] = generation + 1
            self._put(db, 'goal', goal_id, goal)
