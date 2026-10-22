"""Transactional persistence for trusted workflow controllers, not an authorization API.

Callers validate signed events before storing grants. A repeated reserve never
confers a second dispatch; controllers dispatch only after a newly won reserve.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from .core import ContractValidationError

Payload = dict[str, Any]
Expected = Mapping[tuple[str, str], str | None]
_GOAL_GENERATION_RECORD_KINDS = frozenset({
    'goal', 'baseline', 'evidence', 'strict_evidence', 'strict_operation', 'question', 'observation', 'risk',
    'strict_review', 'strict_question',
    'strict_scope', 'strict_recovery', 'strict_risk',
})
class WorkflowStoreConflict(ContractValidationError):
    """A strict store conflict with a stable fail-closed outcome."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _json(value: Mapping[str, Any]) -> str:
    def check(item: Any) -> None:
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise ContractValidationError('JSON keys must be strings')
            for child in item.values():
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)
    try:
        check(value)
        return json.dumps(dict(value), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContractValidationError('Invalid JSON payload') from exc


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError('timezone required')
        return parsed
    except (TypeError, ValueError) as exc:
        raise ContractValidationError('Timestamp must include timezone') from exc


def _key_component(value: str) -> str:
    if type(value) is not str or not value:
        raise ContractValidationError('Strict evidence key components must be non-empty text')
    try:
        return base64.urlsafe_b64encode(value.encode('utf-8')).decode('ascii').rstrip('=')
    except UnicodeEncodeError as exc:
        raise ContractValidationError('Strict evidence key component is not valid Unicode') from exc



class WorkflowStoreCore:
    """One SQLite connection and explicit writer transaction per operation."""

    def __init__(self, path: str | Path, timeout: float = 5):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ContractValidationError('timeout must be finite and positive')
        self.path = str(path)
        self.timeout = timeout
        if self.path == ':memory:':
            raise ContractValidationError('A persistent filesystem path is required')
        try:
            self._probe_sqlite_capabilities()
            self._initialize_schema()
        except ContractValidationError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ContractValidationError('Cannot initialize workflow database') from exc

    @staticmethod
    def _create_base_schema(db: sqlite3.Connection) -> None:
        db.execute('CREATE TABLE records (kind TEXT, key TEXT, payload TEXT NOT NULL, '
                   'PRIMARY KEY(kind,key))')
        db.execute('CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, '
                   'event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)')

    @staticmethod
    def _create_v2_schema(db: sqlite3.Connection) -> None:
        db.execute('CREATE UNIQUE INDEX events_id_seq ON events(event_id,seq)')
        db.execute(
            'CREATE TABLE strict_proofs ('
            'proof_id TEXT PRIMARY KEY NOT NULL, '
            "kind TEXT NOT NULL CHECK(kind IN ('baseline','evidence')), "
            'goal_id TEXT NOT NULL, scope_digest TEXT NOT NULL, subject_id TEXT NOT NULL, '
            'source_event_id TEXT NOT NULL, source_seq INTEGER NOT NULL CHECK(source_seq > 0), '
            'source_event_digest TEXT NOT NULL, proof_digest TEXT NOT NULL, '
            'actor_type TEXT NOT NULL, principal_id TEXT NOT NULL, account_id TEXT NOT NULL, '
            'session_id TEXT NOT NULL, expires_at TEXT NOT NULL, policy_digest TEXT NOT NULL, '
            'runtime_binding_digest TEXT NOT NULL, lease_generation INTEGER NOT NULL CHECK(lease_generation > 0), '
            'payload TEXT NOT NULL, '
            'UNIQUE(kind,goal_id,scope_digest,subject_id,source_seq), '
            'UNIQUE(proof_id,kind,goal_id,scope_digest,subject_id,source_seq), '
            'UNIQUE(source_event_id), '
            'FOREIGN KEY(source_event_id,source_seq) REFERENCES events(event_id,seq) '
            'DEFERRABLE INITIALLY DEFERRED)'
        )
        db.execute(
            'CREATE TABLE strict_heads ('
            'kind TEXT NOT NULL CHECK(kind IN (\'baseline\',\'evidence\')), '
            'goal_id TEXT NOT NULL, scope_digest TEXT NOT NULL, subject_id TEXT NOT NULL, '
            'proof_id TEXT NOT NULL, source_seq INTEGER NOT NULL CHECK(source_seq > 0), '
            'generation INTEGER NOT NULL CHECK(generation > 0), '
            'PRIMARY KEY(kind,goal_id,scope_digest,subject_id), '
            'FOREIGN KEY(proof_id,kind,goal_id,scope_digest,subject_id,source_seq) '
            'REFERENCES strict_proofs(proof_id,kind,goal_id,scope_digest,subject_id,source_seq) '
            'DEFERRABLE INITIALLY DEFERRED)'
        )
        db.execute('CREATE INDEX strict_proofs_goal ON strict_proofs(goal_id,kind,scope_digest,subject_id,source_seq)')
        db.execute(
            'CREATE TRIGGER strict_proofs_no_update BEFORE UPDATE ON strict_proofs '
            "BEGIN SELECT RAISE(ABORT,'strict proof history is append-only'); END"
        )
        db.execute(
            'CREATE TRIGGER strict_proofs_no_delete BEFORE DELETE ON strict_proofs '
            "BEGIN SELECT RAISE(ABORT,'strict proof history is append-only'); END"
        )
        db.execute(
            'CREATE TRIGGER strict_proofs_no_replace BEFORE INSERT ON strict_proofs '
            'WHEN EXISTS(SELECT 1 FROM strict_proofs WHERE proof_id=NEW.proof_id '
            'OR source_event_id=NEW.source_event_id '
            'OR (kind=NEW.kind AND goal_id=NEW.goal_id AND scope_digest=NEW.scope_digest '
            'AND subject_id=NEW.subject_id AND source_seq=NEW.source_seq)) '
            "BEGIN SELECT RAISE(ABORT,'strict proof history is append-only'); END"
        )
        db.execute(
            'CREATE TRIGGER strict_heads_no_delete BEFORE DELETE ON strict_heads '
            "BEGIN SELECT RAISE(ABORT,'strict proof head cannot be deleted'); END"
        )
        db.execute(
            'CREATE TRIGGER strict_heads_no_replace BEFORE INSERT ON strict_heads '
            'WHEN EXISTS(SELECT 1 FROM strict_heads WHERE kind=NEW.kind AND goal_id=NEW.goal_id '
            'AND scope_digest=NEW.scope_digest AND subject_id=NEW.subject_id) '
            "BEGIN SELECT RAISE(ABORT,'strict proof head must advance with UPDATE'); END"
        )
        db.execute(
            'CREATE TRIGGER strict_heads_monotonic BEFORE UPDATE ON strict_heads '
            'WHEN NEW.kind!=OLD.kind OR NEW.goal_id!=OLD.goal_id OR NEW.scope_digest!=OLD.scope_digest '
            'OR NEW.subject_id!=OLD.subject_id OR NEW.source_seq<=OLD.source_seq '
            'OR NEW.generation!=OLD.generation+1 '
            "BEGIN SELECT RAISE(ABORT,'strict proof head must advance monotonically'); END"
        )
        db.execute(
            'CREATE TRIGGER events_no_update BEFORE UPDATE ON events '
            "BEGIN SELECT RAISE(ABORT,'workflow event history is append-only'); END"
        )
        db.execute(
            'CREATE TRIGGER events_no_delete BEFORE DELETE ON events '
            "BEGIN SELECT RAISE(ABORT,'workflow event history is append-only'); END"
        )
        db.execute(
            'CREATE TRIGGER events_no_replace BEFORE INSERT ON events '
            'WHEN EXISTS(SELECT 1 FROM events WHERE event_id=NEW.event_id OR '
            '(NEW.seq>0 AND seq=NEW.seq)) '
            "BEGIN SELECT RAISE(ABORT,'workflow event history is append-only'); END"
        )

    @classmethod
    def _probe_sqlite_capabilities(cls) -> None:
        if sqlite3.sqlite_version_info < (3, 24, 0):
            raise ContractValidationError('SQLite 3.24 or later is required')
        db = sqlite3.connect(':memory:', isolation_level=None)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise ContractValidationError('SQLite foreign-key enforcement is unavailable')
            db.execute('PRAGMA recursive_triggers=ON')
            cls._create_base_schema(db)
            cls._create_v2_schema(db)
            db.execute('BEGIN')
            db.execute(
                'INSERT INTO strict_heads(kind,goal_id,scope_digest,subject_id,proof_id,source_seq,generation) '
                "VALUES('evidence','probe','baseline','evidence','missing-proof',1,1)"
            )
            try:
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
            else:
                raise ContractValidationError('SQLite deferred foreign-key enforcement is unavailable')
        except sqlite3.DatabaseError as exc:
            if db.in_transaction:
                db.rollback()
            raise ContractValidationError('Required SQLite schema capabilities are unavailable') from exc
        finally:
            db.close()

    def _read_schema_version(self) -> tuple[int, set[str]]:
        path = Path(self.path).resolve()
        if not path.exists():
            return 0, set()
        uri = path.as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True, timeout=self.timeout)
        try:
            version = int(db.execute('PRAGMA user_version').fetchone()[0])
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return version, tables
        finally:
            db.close()

    def _initialize_schema(self) -> None:
        version, tables = self._read_schema_version()
        if version > 2:
            raise ContractValidationError('Unsupported workflow schema')
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=' + str(min(2147483647, max(1, int(self.timeout * 1000)))))
            db.execute('PRAGMA foreign_keys=ON')
            if db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise ContractValidationError('SQLite foreign-key enforcement is unavailable')
            db.execute('PRAGMA recursive_triggers=ON')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            locked_version = int(db.execute('PRAGMA user_version').fetchone()[0])
            if locked_version > 2:
                raise ContractValidationError('Unsupported workflow schema')
            if locked_version == 2:
                db.commit()
                return
            if locked_version == 0:
                if tables or db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise ContractValidationError('Unrecognized existing database')
                self._create_base_schema(db)
            elif locked_version == 1:
                existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'records', 'events'} <= existing:
                    raise ContractValidationError('Unrecognized workflow schema')
            self._create_v2_schema(db)
            db.execute('PRAGMA user_version=2')
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=' + str(min(2147483647, max(1, int(self.timeout * 1000)))))
            db.execute('PRAGMA foreign_keys=ON')
            if db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise ContractValidationError('SQLite foreign-key enforcement is unavailable')
            db.execute('PRAGMA recursive_triggers=ON')
            current_mode = str(db.execute('PRAGMA journal_mode').fetchone()[0]).lower()
            if current_mode != 'wal':
                try:
                    db.execute('PRAGMA journal_mode=WAL')
                except sqlite3.OperationalError:
                    # Journal mode is a performance choice; concurrent writers may keep DELETE.
                    current_mode = str(db.execute('PRAGMA journal_mode').fetchone()[0]).lower()
                    if current_mode not in ('delete', 'wal'):
                        raise
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        uri = Path(self.path).resolve().as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True, timeout=self.timeout, isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=' + str(min(2147483647, max(1, int(self.timeout * 1000)))))
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def fingerprint(payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(_json(payload).encode('utf-8')).hexdigest()

    @staticmethod
    def same_json(left: Any, right: Any) -> bool:
        """Compare JSON values canonically without Python's bool/int equality coercion."""
        try:
            return _json({'value': left}) == _json({'value': right})
        except ContractValidationError:
            return False

    @staticmethod
    def _get(db: sqlite3.Connection, kind: str, key: str) -> Payload | None:
        row = db.execute('SELECT payload FROM records WHERE kind=? AND key=?', (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(db: sqlite3.Connection, kind: str, key: str, payload: Payload) -> None:
        db.execute('INSERT INTO records(kind,key,payload) VALUES(?,?,?) '
                   'ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload', (kind, key, _json(payload)))

    def _check(self, db: sqlite3.Connection, expected: Expected) -> None:
        for (kind, key), digest in expected.items():
            current = self._get(db, kind, key)
            actual = self.fingerprint(current) if current is not None else None
            if actual != digest:
                raise ContractValidationError('Stale workflow record')

    def _touch_goal(self, db: sqlite3.Connection, goal_id: str) -> None:
        """Serialize dependency/effect changes with goal-level CAS decisions."""
        goal = self._get(db, 'goal', goal_id)
        if goal is not None:
            goal['generation'] = goal.get('generation', 0) + 1
            self._put(db, 'goal', goal_id, goal)

    @staticmethod
    def _audit_transition(db: sqlite3.Connection, action: str, payload: Payload, at: str) -> None:
        event = dict(action=action, record=payload, at=at)
        db.execute('INSERT INTO events(event_id,payload) VALUES(?,?)',
                   ('store:' + uuid4().hex, _json(event)))

    def get(self, kind: str, key: str) -> Payload | None:
        with self._transaction() as db:
            return self._get(db, kind, key)

    def list(self, kind: str, goal_id: str | None = None) -> tuple[Payload, ...]:
        with self._transaction() as db:
            values = tuple(json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM records WHERE kind=? ORDER BY key', (kind,)))
            return tuple(v for v in values if goal_id is None or v.get('goal_id') == goal_id)

    def commit(self, changes: Sequence[tuple[str, str, Payload]], expected: Expected,
               event_id: str, event: Payload, *, valid_until: str | None = None,
               clock: Callable[[], str] | None = None,
               pre_commit_check: Callable[[], None] | None = None) -> bool:
        return self.commit_many(changes, expected, ((event_id, event),),
                                valid_until=valid_until, clock=clock,
                                pre_commit_check=pre_commit_check)

    def commit_many(self, changes: Sequence[tuple[str, str, Payload]], expected: Expected,
                    events: Sequence[tuple[str, Payload]], *, valid_until: str | None = None,
                    clock: Callable[[], str] | None = None,
                    pre_commit_check: Callable[[], None] | None = None) -> bool:
        """Atomically commit record changes and source events already verified by the caller."""
        if isinstance(events, (str, bytes)) or not isinstance(events, Sequence) or not events:
            raise ContractValidationError('At least one workflow event is required')
        encoded_events = []
        event_ids = set()
        for item in events:
            if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or len(item) != 2:
                raise ContractValidationError('Each workflow event requires an ID and payload')
            event_id, event = item
            if not isinstance(event_id, str) or not event_id or event_id.startswith('store:'):
                raise ContractValidationError('Invalid or reserved event ID')
            if event_id in event_ids:
                raise ContractValidationError('Duplicate event ID in batch')
            event_ids.add(event_id)
            encoded_events.append((event_id, _json(event)))

        with self._transaction() as db:
            if valid_until is not None:
                if clock is None or _time(clock()) >= _time(valid_until):
                    raise ContractValidationError('Workflow dependency expired before commit')
            if pre_commit_check is not None:
                pre_commit_check()
            existing = []
            for event_id, encoded in encoded_events:
                old = db.execute('SELECT payload FROM events WHERE event_id=?', (event_id,)).fetchone()
                if old is not None:
                    if old[0] != encoded:
                        raise ContractValidationError('Event ID conflict')
                    existing.append(True)
                else:
                    existing.append(False)
            if all(existing):
                return False
            if any(existing):
                raise ContractValidationError('Partial event batch replay')
            self._check(db, expected)
            seen = set()
            affected_goals = set()
            for kind, key, payload in changes:
                if (kind, key) not in expected or (kind, key) in seen:
                    raise ContractValidationError('Every changed record requires unique expected version')
                seen.add((kind, key))
                self._put(db, kind, key, payload)
                if kind in _GOAL_GENERATION_RECORD_KINDS and payload.get('goal_id'):
                    affected_goals.add(payload['goal_id'])
            for goal_id in affected_goals:
                self._touch_goal(db, goal_id)
            for event_id, encoded in encoded_events:
                db.execute('INSERT INTO events(event_id,payload) VALUES(?,?)', (event_id, encoded))
            if pre_commit_check is not None:
                pre_commit_check()
            if valid_until is not None:
                if clock is None or _time(clock()) >= _time(valid_until):
                    raise ContractValidationError('Workflow dependency expired before commit')
            return True

    def audit(self) -> tuple[Payload, ...]:
        with self._transaction() as db:
            return tuple(dict(seq=r[0], event_id=r[1], payload=json.loads(r[2]))
                         for r in db.execute('SELECT seq,event_id,payload FROM events ORDER BY seq'))

    def get_event(self, event_id: str) -> Payload | None:
        with self._transaction() as db:
            row = db.execute('SELECT payload FROM events WHERE event_id=?', (event_id,)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _valid(grant: Payload, at: str) -> None:
        now = _time(at)
        if grant.get('expires_at') is not None and _time(grant['expires_at']) <= now:
            raise ContractValidationError('Grant expired')
        if grant.get('state') == 'REVOKED' or grant.get('revoked_at'):
            raise ContractValidationError('Grant revoked')

    def reserve(self, grant_id: str, attempt_id: str, operation_digest: str, at: str,
                expected: Expected = MappingProxyType({}), *,
                clock: Callable[[], str] | None = None) -> tuple[bool, Payload]:
        with self._transaction() as db:
            if clock is not None:
                at = clock()
            old = self._get(db, 'receipt', attempt_id)
            if old is not None:
                if old['authorization_id'] != grant_id or old['operation_digest'] != operation_digest:
                    raise ContractValidationError('Attempt identity conflict')
                return False, old
            self._check(db, expected)
            grant = self._get(db, 'grant', grant_id)
            if not grant or grant.get('state') != 'ACTIVE' or grant.get('operation_digest') != operation_digest:
                raise ContractValidationError('Grant unavailable')
            self._valid(grant, at)
            receipt = dict(attempt_id=attempt_id, authorization_id=grant_id, goal_id=grant['goal_id'],
                           operation_digest=operation_digest, state='NOT_STARTED', phase='RESERVED',
                           dispatch_right=False, created_at=at)
            grant.update(state='RESERVED', attempt_id=attempt_id)
            self._put(db, 'grant', grant_id, grant)
            self._put(db, 'receipt', attempt_id, receipt)
            self._audit_transition(db, 'reserve', receipt, at)
            return True, receipt

    def consume(self, attempt_id: str, at: str, expected: Expected = MappingProxyType({}), *,
                clock: Callable[[], str] | None = None) -> Payload:
        with self._transaction() as db:
            if clock is not None:
                at = clock()
            self._check(db, expected)
            receipt = self._get(db, 'receipt', attempt_id)
            if receipt is None:
                raise ContractValidationError('Unknown attempt')
            if receipt['dispatch_right']:
                return receipt
            grant = self._get(db, 'grant', receipt['authorization_id'])
            if not grant or grant.get('state') != 'RESERVED' or grant.get('attempt_id') != attempt_id:
                raise ContractValidationError('Grant not reserved by attempt')
            self._valid(grant, at)
            grant['state'] = 'CONSUMED'
            receipt.update(phase='CONSUMED', dispatch_right=True, consumed_at=at)
            self._put(db, 'grant', receipt['authorization_id'], grant)
            self._put(db, 'receipt', attempt_id, receipt)
            self._audit_transition(db, 'consume', receipt, at)
            self._touch_goal(db, receipt['goal_id'])
            return receipt

    def revoke(self, grant_id: str, at: str) -> Payload:
        _time(at)
        with self._transaction() as db:
            grant = self._get(db, 'grant', grant_id)
            if grant is None:
                raise ContractValidationError('Unknown grant')
            if grant['state'] in ('ACTIVE', 'RESERVED'):
                grant['state'] = 'REVOKED'
            if 'revoked_at' in grant:
                return grant
            grant['revoked_at'] = at
            self._put(db, 'grant', grant_id, grant)
            self._audit_transition(db, 'revoke', grant, at)
            self._touch_goal(db, grant['goal_id'])
            return grant

    def finish(self, attempt_id: str, state: str, result: Payload, at: str) -> Payload:
        _time(at)
        _json(result)
        with self._transaction() as db:
            receipt = self._get(db, 'receipt', attempt_id)
            if not receipt or not receipt['dispatch_right']:
                raise ContractValidationError('Attempt has no dispatch right')
            old = receipt['state']
            if old in ('SUCCEEDED', 'FAILED') or old == state == 'UNKNOWN':
                if old == state and receipt.get('result') == result:
                    return receipt
                raise ContractValidationError('Conflicting terminal receipt')
            if state not in ('RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN') or (
                old == 'UNKNOWN' and state not in ('SUCCEEDED', 'FAILED')
            ):
                raise ContractValidationError('Invalid receipt transition')
            receipt.update(state=state, result=result, updated_at=at)
            self._put(db, 'receipt', attempt_id, receipt)
            self._audit_transition(db, 'finish', receipt, at)
            self._touch_goal(db, receipt['goal_id'])
            return receipt

    def recover(self, goal_id: str | None = None, *, at: str) -> tuple[Payload, ...]:
        _time(at)
        with self._transaction() as db:
            changed = []
            for key, raw in db.execute('SELECT key,payload FROM records WHERE kind=?', ('receipt',)).fetchall():
                receipt = json.loads(raw)
                if (goal_id is None or receipt['goal_id'] == goal_id) and receipt['dispatch_right'] and (
                    receipt['state'] in ('NOT_STARTED', 'RUNNING')
                ):
                    receipt.update(state='UNKNOWN', updated_at=at)
                    self._put(db, 'receipt', key, receipt)
                    self._audit_transition(db, 'recover', receipt, at)
                    self._touch_goal(db, receipt['goal_id'])
                    changed.append(receipt)
            return tuple(changed)
