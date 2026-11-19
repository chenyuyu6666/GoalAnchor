"""Durable, cross-process efficiency admission and budget evidence."""

from __future__ import annotations

import hashlib
import math
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import ContractValidationError
from .efficiency_governance import (
    ComplexityResult,
    EfficiencyAdmissionError,
    EfficiencyDecision,
)
from .workflow_protocol import (
    canonical,
    fingerprint,
    parse_time,
    sha256_text,
    strict_loads,
    utc_now,
)


@dataclass(frozen=True)
class StoredEfficiencyAdmission:
    idempotency_key: str
    scope_id: str
    token_budget: int
    time_budget_seconds: float
    admission_id: str
    request_digest: str
    allowed: bool
    reason: str
    complexity: ComplexityResult
    tokens_used: int | None
    elapsed_seconds: float
    token_status: str
    progress_alert: bool
    recorded_at: str
    generation: int
    cumulative_tokens: int
    cumulative_elapsed_seconds: float

    @property
    def decision(self) -> EfficiencyDecision:
        return EfficiencyDecision(
            allowed=self.allowed,
            reason=self.reason,
            complexity=self.complexity,
            tokens_used=self.tokens_used,
            elapsed_seconds=self.elapsed_seconds,
            progress_alert=self.progress_alert,
            token_status=self.token_status,
        )


@dataclass(frozen=True)
class EfficiencyBudgetState:
    scope_id: str
    generation: int
    tokens_consumed: int
    elapsed_seconds: float
    token_budget: int
    time_budget_seconds: float


_SCHEMA_SQL = {
    "efficiency_admissions": (
        "CREATE TABLE efficiency_admissions ("
        "admission_id TEXT PRIMARY KEY NOT NULL, "
        "idempotency_key TEXT UNIQUE NOT NULL, "
        "request_digest TEXT NOT NULL, "
        "scope_id TEXT NOT NULL, "
        "operation TEXT NOT NULL, "
        "operation_fingerprint TEXT NOT NULL, "
        "risk TEXT NOT NULL CHECK(risk IN ('LOW','MEDIUM','HIGH','CRITICAL')), "
        "allowed INTEGER NOT NULL CHECK(allowed IN (0,1)), "
        "reason TEXT NOT NULL, "
        "complexity TEXT NOT NULL, "
        "tokens_used INTEGER, "
        "elapsed_seconds REAL NOT NULL, "
        "token_status TEXT NOT NULL CHECK(token_status IN ('MEASURED','UNKNOWN')), "
        "progress_alert INTEGER NOT NULL CHECK(progress_alert IN (0,1)), "
        "recorded_at TEXT NOT NULL, "
        "generation INTEGER NOT NULL CHECK(generation > 0), "
        "cumulative_tokens INTEGER NOT NULL CHECK(cumulative_tokens >= 0), "
        "cumulative_elapsed_seconds REAL NOT NULL "
        "CHECK(cumulative_elapsed_seconds >= 0), "
        "payload TEXT NOT NULL)"
    ),
    "efficiency_budget_heads": (
        "CREATE TABLE efficiency_budget_heads ("
        "scope_id TEXT PRIMARY KEY NOT NULL, "
        "generation INTEGER NOT NULL CHECK(generation > 0), "
        "tokens_consumed INTEGER NOT NULL CHECK(tokens_consumed >= 0), "
        "elapsed_seconds REAL NOT NULL CHECK(elapsed_seconds >= 0), "
        "token_budget INTEGER NOT NULL CHECK(token_budget >= 1), "
        "time_budget_seconds REAL NOT NULL CHECK(time_budget_seconds > 0), "
        "last_admission_id TEXT UNIQUE NOT NULL, "
        "updated_at TEXT NOT NULL, "
        "FOREIGN KEY(last_admission_id) REFERENCES efficiency_admissions(admission_id) "
        "DEFERRABLE INITIALLY DEFERRED)"
    ),
    "efficiency_admissions_no_update": (
        "CREATE TRIGGER efficiency_admissions_no_update BEFORE UPDATE ON efficiency_admissions "
        "BEGIN SELECT RAISE(ABORT,'efficiency admission history is append-only'); END"
    ),
    "efficiency_admissions_no_delete": (
        "CREATE TRIGGER efficiency_admissions_no_delete BEFORE DELETE ON efficiency_admissions "
        "BEGIN SELECT RAISE(ABORT,'efficiency admission history is append-only'); END"
    ),
    "efficiency_admissions_no_replace": (
        "CREATE TRIGGER efficiency_admissions_no_replace BEFORE INSERT ON efficiency_admissions "
        "WHEN EXISTS(SELECT 1 FROM efficiency_admissions WHERE admission_id=NEW.admission_id "
        "OR idempotency_key=NEW.idempotency_key) "
        "BEGIN SELECT RAISE(ABORT,'efficiency admission history is append-only'); END"
    ),
    "efficiency_budget_heads_no_delete": (
        "CREATE TRIGGER efficiency_budget_heads_no_delete BEFORE DELETE ON efficiency_budget_heads "
        "BEGIN SELECT RAISE(ABORT,'efficiency budget head cannot be deleted'); END"
    ),
    "efficiency_budget_heads_no_replace": (
        "CREATE TRIGGER efficiency_budget_heads_no_replace BEFORE INSERT ON efficiency_budget_heads "
        "WHEN EXISTS(SELECT 1 FROM efficiency_budget_heads WHERE scope_id=NEW.scope_id) "
        "BEGIN SELECT RAISE(ABORT,'efficiency budget head must advance with UPDATE'); END"
    ),
    "efficiency_budget_heads_monotonic": (
        "CREATE TRIGGER efficiency_budget_heads_monotonic BEFORE UPDATE ON efficiency_budget_heads "
        "WHEN NEW.generation<=OLD.generation "
        "OR NEW.scope_id!=OLD.scope_id "
        "OR NEW.tokens_consumed<OLD.tokens_consumed "
        "OR NEW.elapsed_seconds<OLD.elapsed_seconds "
        "OR NEW.token_budget!=OLD.token_budget "
        "OR NEW.time_budget_seconds!=OLD.time_budget_seconds "
        "OR NEW.last_admission_id=OLD.last_admission_id "
        "BEGIN SELECT RAISE(ABORT,'efficiency budget head must advance monotonically'); END"
    ),
}


class EfficiencyAdmissionStore:
    """Append-only admission history with one cumulative budget head per scope."""

    def __init__(self, path: str | Path, timeout: float = 5, clock: Callable[[], str] = utc_now):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ContractValidationError("timeout must be finite and positive")
        raw_path = str(path)
        if raw_path == ":memory:":
            raise ContractValidationError("A persistent filesystem path is required")
        self.path = str(Path(raw_path).expanduser().resolve())
        self.timeout = timeout
        if not callable(clock):
            raise ContractValidationError("clock must be callable")
        self._clock = clock
        try:
            self._initialize_schema()
        except ContractValidationError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ContractValidationError("Cannot initialize efficiency database") from exc

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        for sql in _SCHEMA_SQL.values():
            db.execute(sql)

    @staticmethod
    def _require_schema(db: sqlite3.Connection) -> None:
        rows = db.execute(
            "SELECT name,type,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        expected_objects = {
            ("efficiency_admissions", "table"),
            ("efficiency_budget_heads", "table"),
            ("efficiency_admissions_no_update", "trigger"),
            ("efficiency_admissions_no_delete", "trigger"),
            ("efficiency_admissions_no_replace", "trigger"),
            ("efficiency_budget_heads_no_delete", "trigger"),
            ("efficiency_budget_heads_no_replace", "trigger"),
            ("efficiency_budget_heads_monotonic", "trigger"),
        }
        if {(row[0], row[1]) for row in rows} != expected_objects:
            raise EfficiencyAdmissionError(
                "efficiency admission schema or append-only guard is missing or changed",
                "UNTRUSTED",
            )
        normalized = {
            row[0]: " ".join(str(row[2]).split()) for row in rows
        }
        for name, expected in _SCHEMA_SQL.items():
            if normalized.get(name) != " ".join(expected.split()):
                raise EfficiencyAdmissionError(
                    "efficiency admission schema or append-only guard is missing or changed",
                    "UNTRUSTED",
                )

    def _initialize_schema(self) -> None:
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise ContractValidationError("SQLite foreign-key enforcement is unavailable")
            db.execute("PRAGMA recursive_triggers=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > 1:
                raise ContractValidationError("Unsupported efficiency schema")
            if version == 0:
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )}
                if tables:
                    raise ContractValidationError("Unrecognized existing efficiency database")
                self._create_schema(db)
                db.execute("PRAGMA user_version=1")
            self._require_schema(db)
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
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise ContractValidationError("SQLite foreign-key enforcement is unavailable")
            db.execute("PRAGMA recursive_triggers=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            self._require_schema(db)
            yield db
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _read_connection(self) -> sqlite3.Connection:
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=self.timeout, isolation_level=None)
        db.execute("PRAGMA query_only=ON")
        return db

    @staticmethod
    def _text(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ContractValidationError(f"{name} must be non-empty text")
        return value.strip()

    @staticmethod
    def _nonnegative_int(value: object, name: str) -> int:
        if type(value) is not int or value < 0:
            raise ContractValidationError(f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _nonnegative_float(value: object, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ContractValidationError(f"{name} must be finite and non-negative")
        return float(value)

    def _request(self, *, idempotency_key: str, scope_id: str, token_budget: int,
                 time_budget_seconds: float, operation: str, operation_fingerprint: str,
                 risk: str, complexity: ComplexityResult,
                 tokens_used: int | None, elapsed_seconds: float) -> dict[str, Any]:
        if type(token_budget) is not int or token_budget < 1:
            raise ContractValidationError("token_budget must be positive")
        self._nonnegative_float(time_budget_seconds, "time_budget_seconds")
        if time_budget_seconds <= 0:
            raise ContractValidationError("time_budget_seconds must be positive")
        self._text(idempotency_key, "idempotency_key")
        self._text(scope_id, "scope_id")
        self._text(operation, "operation")
        sha256_text(operation_fingerprint, "operation_fingerprint")
        if risk not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            raise ContractValidationError("risk must be LOW, MEDIUM, HIGH, or CRITICAL")
        if not isinstance(complexity, ComplexityResult):
            raise ContractValidationError("complexity must be a ComplexityResult")
        if tokens_used is not None:
            self._nonnegative_int(tokens_used, "tokens_used")
        self._nonnegative_float(elapsed_seconds, "elapsed_seconds")
        return {
            "idempotency_key": self._text(idempotency_key, "idempotency_key"),
            "scope_id": self._text(scope_id, "scope_id"),
            "token_budget": token_budget,
            "time_budget_seconds": float(time_budget_seconds),
            "operation": self._text(operation, "operation"),
            "operation_fingerprint": operation_fingerprint,
            "risk": risk,
            "complexity": {
                "score": complexity.score,
                "level": complexity.level,
                "signals": complexity.signals,
            },
            "tokens_used": tokens_used,
            "elapsed_seconds": float(elapsed_seconds),
        }

    _ADMISSION_COLUMNS = (
        "admission_id,idempotency_key,request_digest,scope_id,operation,"
        "operation_fingerprint,risk,allowed,reason,complexity,tokens_used,"
        "elapsed_seconds,token_status,progress_alert,recorded_at,generation,"
        "cumulative_tokens,cumulative_elapsed_seconds,payload"
    )
    _BUDGET_HEAD_COLUMNS = (
        "generation,tokens_consumed,elapsed_seconds,token_budget,time_budget_seconds,"
        "last_admission_id,updated_at"
    )

    @classmethod
    def _request_from_record(cls, record: Mapping[str, Any]) -> dict[str, Any]:
        complexity = record["complexity"]
        if not isinstance(complexity, Mapping):
            raise ValueError("complexity is not an object")
        return {
            "idempotency_key": record["idempotency_key"],
            "scope_id": record["scope_id"],
            "token_budget": record["token_budget"],
            "time_budget_seconds": record["time_budget_seconds"],
            "operation": record["operation"],
            "operation_fingerprint": record["operation_fingerprint"],
            "risk": record["risk"],
            "complexity": {
                "score": complexity["score"],
                "level": complexity["level"],
                "signals": complexity["signals"],
            },
            "tokens_used": record["tokens_used"],
            "elapsed_seconds": record["elapsed_seconds"],
        }

    @classmethod
    def _load(cls, row: Sequence[Any]) -> StoredEfficiencyAdmission:
        payload = row[-1]
        try:
            record = strict_loads(payload)
            if not isinstance(record, dict):
                raise ValueError("payload is not an object")
            required = {
                "admission_id", "idempotency_key", "scope_id", "token_budget",
                "time_budget_seconds", "operation", "operation_fingerprint", "risk",
                "complexity", "request_digest", "allowed", "reason", "tokens_used",
                "elapsed_seconds", "token_status", "progress_alert", "recorded_at",
                "generation", "cumulative_tokens", "cumulative_elapsed_seconds",
            }
            if set(record) != required:
                raise ValueError("persisted admission fields changed")
            if type(record["allowed"]) is not bool or type(record["progress_alert"]) is not bool:
                raise ValueError("persisted admission booleans are invalid")
            if record["risk"] not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
                raise ValueError("persisted risk is invalid")
            if type(record["token_budget"]) is not int or record["token_budget"] < 1:
                raise ValueError("persisted token budget is invalid")
            cls._nonnegative_float(record["time_budget_seconds"], "time_budget_seconds")
            if record["time_budget_seconds"] <= 0:
                raise ValueError("persisted time budget is invalid")
            cls._nonnegative_float(record["elapsed_seconds"], "elapsed_seconds")
            cls._nonnegative_int(record["generation"], "generation")
            cls._nonnegative_int(record["cumulative_tokens"], "cumulative_tokens")
            cls._nonnegative_float(
                record["cumulative_elapsed_seconds"], "cumulative_elapsed_seconds"
            )
            if record["generation"] < 1:
                raise ValueError("persisted generation is invalid")
            if record["tokens_used"] is not None:
                cls._nonnegative_int(record["tokens_used"], "tokens_used")
            for name in (
                "admission_id",
                "idempotency_key",
                "request_digest",
                "scope_id",
                "operation",
            ):
                cls._text(record[name], name)
            for name in ("admission_id", "request_digest"):
                value = record[name]
                if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                    raise ValueError(f"persisted {name} is invalid")
            sha256_text(record["operation_fingerprint"], "operation_fingerprint")
            if record["token_status"] not in {"MEASURED", "UNKNOWN"}:
                raise ValueError("persisted token status is invalid")
            if type(row[7]) is not int or row[7] not in {0, 1}:
                raise ValueError("persisted allowed column is invalid")
            if type(row[13]) is not int or row[13] not in {0, 1}:
                raise ValueError("persisted progress column is invalid")
            cls._nonnegative_int(row[15], "generation")
            if row[15] < 1:
                raise ValueError("persisted generation column is invalid")
            cls._nonnegative_int(row[16], "cumulative_tokens")
            cls._nonnegative_float(row[17], "cumulative_elapsed_seconds")
            if row[10] is not None:
                cls._nonnegative_int(row[10], "tokens_used")
            cls._nonnegative_float(row[11], "elapsed_seconds")
            expected = {
                "admission_id": row[0],
                "idempotency_key": row[1],
                "request_digest": row[2],
                "scope_id": row[3],
                "operation": row[4],
                "operation_fingerprint": row[5],
                "risk": row[6],
                "allowed": bool(row[7]),
                "reason": row[8],
                "tokens_used": row[10],
                "elapsed_seconds": row[11],
                "token_status": row[12],
                "progress_alert": bool(row[13]),
                "recorded_at": row[14],
                "generation": row[15],
                "cumulative_tokens": row[16],
                "cumulative_elapsed_seconds": row[17],
            }
            for name, value in expected.items():
                if record.get(name) != value:
                    raise ValueError(f"persisted {name} does not match the admission row")
            if row[9] != canonical(record["complexity"]):
                raise ValueError("persisted complexity does not match the admission row")
            request = cls._request_from_record(record)
            digest = fingerprint(request)
            if record["request_digest"] != digest or row[2] != digest:
                raise ValueError("persisted request digest is invalid")
            expected_admission_id = hashlib.sha256(
                (digest + "\0" + record["recorded_at"]).encode("utf-8")
            ).hexdigest()
            if record["admission_id"] != expected_admission_id:
                raise ValueError("persisted admission id is invalid")
            parse_time(record["recorded_at"])
            complexity = record["complexity"]
            if not isinstance(complexity, dict) or set(complexity) != {
                "score",
                "level",
                "signals",
            }:
                raise ValueError("complexity is not an object")
            if (
                type(complexity.get("score")) is not int
                or complexity["score"] < 0
                or complexity["level"] not in {"LOW", "MEDIUM", "HIGH"}
                or not isinstance(complexity.get("signals"), dict)
                or any(
                    not isinstance(name, str)
                    or type(value) is not int
                    or value < 0
                    for name, value in complexity["signals"].items()
                )
            ):
                raise ValueError("persisted complexity is invalid")
            if type(record["generation"]) is not int or record["generation"] <= 0:
                raise ValueError("persisted generation is invalid")
            if (record["tokens_used"] is None) != (record["token_status"] == "UNKNOWN"):
                raise ValueError("persisted token status is invalid")
            if record["progress_alert"]:
                if record["allowed"] is not False or record["reason"] != "REPEATED_OPERATION":
                    raise ValueError("persisted progress decision is invalid")
            elif record["allowed"]:
                if record["reason"] != "WITHIN_BUDGET" or record["tokens_used"] is None:
                    raise ValueError("persisted allowed decision is invalid")
            elif record["reason"] == "UNKNOWN_USAGE":
                if record["tokens_used"] is not None or record["token_status"] != "UNKNOWN":
                    raise ValueError("persisted unknown-usage decision is invalid")
            elif record["reason"] in {"TOKEN_BUDGET_EXCEEDED", "TIME_BUDGET_EXCEEDED"}:
                if record["tokens_used"] is None or record["token_status"] != "MEASURED":
                    raise ValueError("persisted budget denial is invalid")
            else:
                raise ValueError("persisted denied decision is invalid")
            if record["allowed"] and (
                record["tokens_used"] is None
                or record["cumulative_tokens"] < record["tokens_used"]
                or record["cumulative_elapsed_seconds"] < record["elapsed_seconds"]
            ):
                raise ValueError("persisted cumulative usage is invalid")
            return StoredEfficiencyAdmission(
                idempotency_key=record["idempotency_key"],
                scope_id=record["scope_id"],
                token_budget=record["token_budget"],
                time_budget_seconds=record["time_budget_seconds"],
                admission_id=record["admission_id"],
                request_digest=record["request_digest"],
                allowed=bool(record["allowed"]),
                reason=record["reason"],
                complexity=ComplexityResult(
                    score=complexity["score"],
                    level=complexity["level"],
                    signals=complexity["signals"],
                ),
                tokens_used=record["tokens_used"],
                elapsed_seconds=record["elapsed_seconds"],
                token_status=record["token_status"],
                progress_alert=bool(record["progress_alert"]),
                recorded_at=record["recorded_at"],
                generation=record["generation"],
                cumulative_tokens=record["cumulative_tokens"],
                cumulative_elapsed_seconds=record["cumulative_elapsed_seconds"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractValidationError("Persisted efficiency admission is malformed") from exc

    def get(self, idempotency_key: str) -> StoredEfficiencyAdmission | None:
        key = self._text(idempotency_key, "idempotency_key")
        db = self._read_connection()
        try:
            db.execute("BEGIN")
            self._require_schema(db)
            row = db.execute(
                "SELECT " + self._ADMISSION_COLUMNS + " FROM efficiency_admissions WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            stored = self._load(row)
            head = db.execute(
                "SELECT " + self._BUDGET_HEAD_COLUMNS
                + " FROM efficiency_budget_heads WHERE scope_id=?",
                (stored.scope_id,),
            ).fetchone()
            self._validate_budget_head(db, stored.scope_id, head)
            db.commit()
            return stored
        except EfficiencyAdmissionError:
            if db.in_transaction:
                db.rollback()
            raise
        except ContractValidationError as exc:
            if db.in_transaction:
                db.rollback()
            raise EfficiencyAdmissionError(
                "efficiency admission evidence is malformed",
                "UNTRUSTED",
            ) from exc
        except sqlite3.DatabaseError as exc:
            if db.in_transaction:
                db.rollback()
            raise ContractValidationError("Cannot read efficiency admission") from exc
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def history(self, scope_id: str) -> tuple[StoredEfficiencyAdmission, ...]:
        scope = self._text(scope_id, "scope_id")
        db = self._read_connection()
        try:
            db.execute("BEGIN")
            self._require_schema(db)
            rows = db.execute(
                "SELECT " + self._ADMISSION_COLUMNS + " FROM efficiency_admissions "
                "WHERE scope_id=? ORDER BY rowid",
                (scope,),
            ).fetchall()
            head = db.execute(
                "SELECT " + self._BUDGET_HEAD_COLUMNS
                + " FROM efficiency_budget_heads WHERE scope_id=?",
                (scope,),
            ).fetchone()
            self._validate_budget_head(db, scope, head)
            result = tuple(self._load(row) for row in rows)
            db.commit()
            return result
        except sqlite3.DatabaseError as exc:
            if db.in_transaction:
                db.rollback()
            raise ContractValidationError("Cannot read efficiency admission history") from exc
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def budget_state(self, scope_id: str) -> EfficiencyBudgetState | None:
        scope = self._text(scope_id, "scope_id")
        db = self._read_connection()
        try:
            db.execute("BEGIN")
            self._require_schema(db)
            row = db.execute(
                "SELECT " + self._BUDGET_HEAD_COLUMNS
                + " FROM efficiency_budget_heads WHERE scope_id=?",
                (scope,),
            ).fetchone()
            self._validate_budget_head(db, scope, row)
            if row is None:
                db.commit()
                return None
            state = EfficiencyBudgetState(
                scope_id=scope,
                generation=row[0],
                tokens_consumed=row[1],
                elapsed_seconds=row[2],
                token_budget=row[3],
                time_budget_seconds=row[4],
            )
            db.commit()
            return state
        except sqlite3.DatabaseError as exc:
            if db.in_transaction:
                db.rollback()
            raise ContractValidationError("Cannot read efficiency budget state") from exc
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _validate_budget_head(
        self,
        db: sqlite3.Connection,
        scope_id: str,
        head: Sequence[Any] | None,
    ) -> tuple[int, float] | None:
        rows = db.execute(
            "SELECT " + self._ADMISSION_COLUMNS + " FROM efficiency_admissions "
            "WHERE scope_id=? ORDER BY rowid",
            (scope_id,),
        ).fetchall()

        allowed_count = 0
        running_tokens = 0
        running_elapsed = 0.0
        last_admission_id = None
        last_allowed_recorded_at = None
        policy: tuple[int, float] | None = None
        for row in rows:
            try:
                stored = self._load(row)
            except ContractValidationError as exc:
                raise EfficiencyAdmissionError(
                    "efficiency budget history contains a malformed admission",
                    "UNTRUSTED",
                ) from exc
            if stored.scope_id != scope_id:
                raise EfficiencyAdmissionError(
                    "efficiency budget history is not a valid allowed chain",
                    "UNTRUSTED",
                )
            stored_policy = (stored.token_budget, stored.time_budget_seconds)
            if policy is None:
                policy = stored_policy
            elif stored_policy != policy:
                raise EfficiencyAdmissionError(
                    "efficiency budget policy changed within one scope",
                    "UNTRUSTED",
                )
            expected_generation = max(1, allowed_count)
            if stored.allowed:
                if stored.tokens_used is None or stored.generation != allowed_count + 1:
                    raise EfficiencyAdmissionError(
                        "efficiency budget history is not a valid allowed chain",
                        "UNTRUSTED",
                    )
                allowed_count += 1
                running_tokens += stored.tokens_used
                running_elapsed += stored.elapsed_seconds
                if (
                    running_tokens > stored.token_budget
                    or running_elapsed > stored.time_budget_seconds
                ):
                    raise EfficiencyAdmissionError(
                        "efficiency allowed history exceeds its budget policy",
                        "UNTRUSTED",
                    )
                last_admission_id = stored.admission_id
                last_allowed_recorded_at = stored.recorded_at
            else:
                if stored.generation != expected_generation:
                    raise EfficiencyAdmissionError(
                        "efficiency denied history has an invalid generation",
                        "UNTRUSTED",
                    )
                candidate_tokens = running_tokens + (stored.tokens_used or 0)
                candidate_elapsed = running_elapsed + stored.elapsed_seconds
                if stored.reason == "TOKEN_BUDGET_EXCEEDED":
                    if candidate_tokens <= stored.token_budget:
                        raise EfficiencyAdmissionError(
                            "efficiency token-denial evidence is inconsistent",
                            "UNTRUSTED",
                        )
                elif stored.reason == "TIME_BUDGET_EXCEEDED":
                    if (
                        candidate_tokens > stored.token_budget
                        or candidate_elapsed <= stored.time_budget_seconds
                    ):
                        raise EfficiencyAdmissionError(
                            "efficiency time-denial evidence is inconsistent",
                            "UNTRUSTED",
                        )
            if (
                stored.cumulative_tokens != running_tokens
                or stored.cumulative_elapsed_seconds != running_elapsed
            ):
                raise EfficiencyAdmissionError(
                    "efficiency admission cumulative history does not match its rows",
                    "UNTRUSTED",
                )

        if head is None:
            if allowed_count:
                raise EfficiencyAdmissionError(
                    "efficiency budget history has no head", "UNTRUSTED"
                )
            return policy
        if (
            type(head[0]) is not int
            or head[0] < 1
            or type(head[1]) is not int
            or head[1] < 0
            or isinstance(head[2], bool)
            or not isinstance(head[2], (int, float))
            or not math.isfinite(head[2])
            or head[2] < 0
            or type(head[3]) is not int
            or head[3] < 1
            or isinstance(head[4], bool)
            or not isinstance(head[4], (int, float))
            or not math.isfinite(head[4])
            or head[4] <= 0
            or not isinstance(head[5], str)
            or not head[5]
            or not isinstance(head[6], str)
            or not head[6]
        ):
            raise EfficiencyAdmissionError(
                "efficiency budget head is malformed", "UNTRUSTED"
            )
        head_policy = (head[3], head[4])
        if policy is not None and head_policy != policy:
            raise EfficiencyAdmissionError(
                "efficiency budget head policy does not match admission history",
                "UNTRUSTED",
            )
        try:
            parse_time(head[6])
        except ContractValidationError as exc:
            raise EfficiencyAdmissionError(
                "efficiency budget head timestamp is invalid", "UNTRUSTED"
            ) from exc
        if (
            head[0] != allowed_count
            or head[1] != running_tokens
            or head[2] != running_elapsed
            or head[5] != last_admission_id
            or head[6] != last_allowed_recorded_at
        ):
            raise EfficiencyAdmissionError(
                "efficiency budget head does not match admission history", "UNTRUSTED"
            )
        return policy

    def admit(
        self,
        *,
        idempotency_key: str,
        scope_id: str,
        token_budget: int,
        time_budget_seconds: float,
        operation: str,
        operation_fingerprint: str,
        risk: str,
        complexity: ComplexityResult,
        progress_alert: bool | Callable[[], bool],
        tokens_used: int | None,
        elapsed_seconds: float,
    ) -> StoredEfficiencyAdmission:
        if not isinstance(progress_alert, bool) and not callable(progress_alert):
            raise ContractValidationError("progress_alert must be a boolean or callable")
        request = self._request(
            idempotency_key=idempotency_key,
            scope_id=scope_id,
            token_budget=token_budget,
            time_budget_seconds=time_budget_seconds,
            operation=operation,
            operation_fingerprint=operation_fingerprint,
            risk=risk,
            complexity=complexity,
            tokens_used=tokens_used,
            elapsed_seconds=elapsed_seconds,
        )
        digest = fingerprint(request)
        try:
            with self._transaction() as db:
                head = db.execute(
                    "SELECT " + self._BUDGET_HEAD_COLUMNS
                    + " FROM efficiency_budget_heads WHERE scope_id=?",
                    (request["scope_id"],),
                ).fetchone()
                policy = self._validate_budget_head(db, request["scope_id"], head)
                row = db.execute(
                    "SELECT " + self._ADMISSION_COLUMNS + " FROM efficiency_admissions WHERE idempotency_key=?",
                    (request["idempotency_key"],),
                ).fetchone()
                if row is not None:
                    stored_digest = row[2]
                    if stored_digest != digest:
                        raise EfficiencyAdmissionError(
                            "efficiency admission idempotency key has different input", "CONFLICT"
                        )
                    try:
                        return self._load(row)
                    except ContractValidationError as exc:
                        raise EfficiencyAdmissionError(
                            "persisted efficiency admission is malformed",
                            "UNTRUSTED",
                        ) from exc
                requested_policy = (
                    request["token_budget"],
                    request["time_budget_seconds"],
                )
                if policy is not None and policy != requested_policy:
                    raise EfficiencyAdmissionError(
                        "efficiency budget policy changed within one scope", "UNTRUSTED"
                    )

                progress = progress_alert() if callable(progress_alert) else progress_alert
                if type(progress) is not bool:
                    raise ContractValidationError("progress_alert must be a boolean")
                previous_tokens = 0 if head is None else head[1]
                previous_elapsed = 0.0 if head is None else head[2]
                current_generation = 0 if head is None else head[0]
                cumulative_tokens = previous_tokens
                cumulative_elapsed = previous_elapsed
                generation = 1 if head is None else current_generation

                token_status = "MEASURED" if tokens_used is not None else "UNKNOWN"
                if progress:
                    allowed = False
                    reason = "REPEATED_OPERATION"
                elif tokens_used is None:
                    allowed = False
                    reason = "UNKNOWN_USAGE"
                else:
                    candidate_tokens = cumulative_tokens + tokens_used
                    candidate_elapsed = cumulative_elapsed + elapsed_seconds
                    if candidate_tokens > token_budget:
                        allowed = False
                        reason = "TOKEN_BUDGET_EXCEEDED"
                    elif candidate_elapsed > time_budget_seconds:
                        allowed = False
                        reason = "TIME_BUDGET_EXCEEDED"
                    else:
                        allowed = True
                        reason = "WITHIN_BUDGET"
                        generation = current_generation + 1
                        cumulative_tokens = candidate_tokens
                        cumulative_elapsed = candidate_elapsed

                recorded_at = self._clock()
                parse_time(recorded_at)
                admission_id = hashlib.sha256(
                    (digest + "\0" + recorded_at).encode("utf-8")
                ).hexdigest()
                record = {
                    "admission_id": admission_id,
                    **request,
                    "request_digest": digest,
                    "allowed": allowed,
                    "reason": reason,
                    "token_status": token_status,
                    "progress_alert": progress,
                    "recorded_at": recorded_at,
                    "generation": generation,
                    "cumulative_tokens": cumulative_tokens,
                    "cumulative_elapsed_seconds": cumulative_elapsed,
                }
                payload = canonical(record)
                db.execute(
                    "INSERT INTO efficiency_admissions("
                    "admission_id,idempotency_key,request_digest,scope_id,operation,"
                    "operation_fingerprint,risk,allowed,reason,complexity,tokens_used,"
                    "elapsed_seconds,token_status,progress_alert,recorded_at,generation,"
                    "cumulative_tokens,cumulative_elapsed_seconds,payload) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        admission_id,
                        record["idempotency_key"],
                        digest,
                        record["scope_id"],
                        record["operation"],
                        record["operation_fingerprint"],
                        record["risk"],
                        int(allowed),
                        reason,
                        canonical(record["complexity"]),
                        record["tokens_used"],
                        record["elapsed_seconds"],
                        token_status,
                        int(progress),
                        recorded_at,
                        generation,
                        cumulative_tokens,
                        cumulative_elapsed,
                        payload,
                    ),
                )
                if allowed:
                    if head is None:
                        db.execute(
                            "INSERT INTO efficiency_budget_heads("
                            "scope_id,generation,tokens_consumed,elapsed_seconds,token_budget,"
                            "time_budget_seconds,last_admission_id,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (
                                request["scope_id"],
                                generation,
                                cumulative_tokens,
                                cumulative_elapsed,
                                request["token_budget"],
                                request["time_budget_seconds"],
                                admission_id,
                                recorded_at,
                            ),
                        )
                    else:
                        db.execute(
                            "UPDATE efficiency_budget_heads SET generation=?,tokens_consumed=?,"
                            "elapsed_seconds=?,token_budget=?,time_budget_seconds=?,"
                            "last_admission_id=?,updated_at=? WHERE scope_id=?",
                            (
                                generation,
                                cumulative_tokens,
                                cumulative_elapsed,
                                request["token_budget"],
                                request["time_budget_seconds"],
                                admission_id,
                                recorded_at,
                                request["scope_id"],
                            ),
                        )
                return self._load(
                    (
                        admission_id,
                        record["idempotency_key"],
                        digest,
                        record["scope_id"],
                        record["operation"],
                        record["operation_fingerprint"],
                        record["risk"],
                        int(allowed),
                        reason,
                        canonical(record["complexity"]),
                        record["tokens_used"],
                        record["elapsed_seconds"],
                        token_status,
                        int(progress),
                        recorded_at,
                        generation,
                        cumulative_tokens,
                        cumulative_elapsed,
                        payload,
                    )
                )
        except ContractValidationError:
            raise
        except sqlite3.IntegrityError as exc:
            raise EfficiencyAdmissionError(
                "efficiency admission constraint rejected the write",
                "UNTRUSTED",
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise ContractValidationError("Cannot durably record efficiency admission") from exc


__all__ = [
    "EfficiencyAdmissionStore",
    "EfficiencyBudgetState",
    "StoredEfficiencyAdmission",
]
