"""Content-bound workflow values and authenticated control-side events.

Authority objects belong to the trusted host, never to model-facing tools.
Signatures authenticate that configured boundary, not a natural person's identity.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from .core import ContractValidationError


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{name} must be nonempty text")
    return value.strip()


def positive_int(value: Any, name: str = "revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError(f"{name} must be a positive integer")
    return value


def strings(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{name} must be an array")
    return tuple(text(item, name) for item in value)


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text(value, "timestamp").replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ContractValidationError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractValidationError("timestamp must have a timezone")
    return parsed


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _valid_json(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _valid_json(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _valid_json(item)
        return
    raise ContractValidationError("only finite JSON values and string object keys are supported")


def canonical(value: Any) -> str:
    _valid_json(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def strict_loads(raw: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ContractValidationError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(value: str) -> Any:
        raise ContractValidationError(f"invalid JSON constant: {value}")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
        _valid_json(value)
        return value
    except (ValueError, TypeError) as exc:
        raise ContractValidationError("invalid JSON") from exc


def shape(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError("object required")
    data = dict(value)
    if not required <= data.keys() or data.keys() - required - (optional or set()):
        raise ContractValidationError("missing or unknown workflow fields")
    return data


def sha256_text(value: Any, name: str = "sha256") -> str:
    result = text(value, name).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractValidationError(f"{name} must be a SHA-256 digest")
    return result


@dataclass(frozen=True)
class SignedEvent:
    event_id: str
    kind: str
    source: str
    subject: str
    payload_json: str
    created_at: str
    signature: str

    def __post_init__(self) -> None:
        for name in ("event_id", "kind", "source", "subject"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        parse_time(self.created_at)
        payload = strict_loads(self.payload_json)
        if not isinstance(payload, dict):
            raise ContractValidationError("event payload must be an object")
        object.__setattr__(self, "payload_json", canonical(payload))
        object.__setattr__(self, "signature", sha256_text(self.signature, "signature"))

    @property
    def payload(self) -> dict[str, Any]:
        return dict(strict_loads(self.payload_json))

    def unsigned(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "kind": self.kind, "source": self.source, "subject": self.subject,
                "payload": self.payload, "created_at": self.created_at}

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned(), "signature": self.signature}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SignedEvent:
        data = shape(value, {"event_id", "kind", "source", "subject", "payload", "created_at", "signature"})
        payload = data.pop("payload")
        return cls(**data, payload_json=canonical(payload))


class EventAuthority:
    source: str
    _key: bytes

    def __init__(self, key: bytes, source: str) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ContractValidationError("authority requires at least 32 secret bytes")
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "source", text(source, "source"))

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_key", "source"} and hasattr(self, name):
            raise AttributeError(f"EventAuthority {name} is immutable; replace the authority to rotate it")
        object.__setattr__(self, name, value)

    @property
    def verification_fingerprint(self) -> str:
        """Non-secret identity for safely keying ephemeral verification caches."""
        return hashlib.sha256(self.source.encode("utf-8") + b"\0" + self._key).hexdigest()

    def sign(self, kind: str, subject: str, payload: Mapping[str, Any], *,
             event_id: str | None = None, at: str | None = None) -> SignedEvent:
        event = SignedEvent(event_id or uuid.uuid4().hex, kind, self.source, subject,
                            canonical(dict(payload)), at or utc_now(), "0" * 64)
        signature = hmac.new(self._key, canonical(event.unsigned()).encode("utf-8"), hashlib.sha256).hexdigest()
        return SignedEvent(**{**event.__dict__, "signature": signature})

    def verify(self, event: SignedEvent, *, kind: str | None = None, subject: str | None = None) -> dict[str, Any]:
        if not isinstance(event, SignedEvent):
            raise ContractValidationError("a signed control-side event is required")
        expected = hmac.new(self._key, canonical(event.unsigned()).encode("utf-8"), hashlib.sha256).hexdigest()
        if event.source != self.source or not hmac.compare_digest(expected, event.signature):
            raise ContractValidationError("invalid control-side event signature")
        if (kind is not None and event.kind != kind) or (subject is not None and event.subject != subject):
            raise ContractValidationError("event kind or subject mismatch")
        return event.payload


@dataclass(frozen=True)
class Criterion:
    criterion_id: str
    description: str
    expected_json: str
    object_ref: str

    def __post_init__(self) -> None:
        for name in ("criterion_id", "description", "object_ref"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        object.__setattr__(self, "expected_json", canonical(strict_loads(self.expected_json)))

    @property
    def expected(self) -> Any:
        return strict_loads(self.expected_json)

    def to_dict(self) -> dict[str, Any]:
        return {"criterion_id": self.criterion_id, "description": self.description,
                "expected": self.expected, "object_ref": self.object_ref}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Criterion:
        data = shape(value, {"criterion_id", "description", "expected", "object_ref"})
        expected = data.pop("expected")
        return cls(**data, expected_json=canonical(expected))


DecisionDependencyItems = tuple[str, ...] | list[str]
DecisionDependencyInput = (
    Mapping[str, DecisionDependencyItems]
    | tuple[tuple[str, DecisionDependencyItems], ...]
)
DecisionDependencyPairs = tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class Baseline:
    """Content-bound plan revision.

    ``decision_dependencies`` accepts a mapping or canonical tuple-pairs at
    construction and is stored as sorted immutable pairs. The tuple form is
    intended for immutable reconstruction (for example, ``dataclasses.replace``).
    Consumers that need key lookup should use ``decision_dependency_map``;
    ``to_dict`` retains the JSON object form.
    """

    goal_id: str
    revision: int
    objective: str
    plan: str
    criteria: tuple[Criterion, ...]
    constraints: tuple[str, ...] = ()
    required_operations: tuple[str, ...] = ()
    decision_dependencies: DecisionDependencyInput = ()

    def __post_init__(self) -> None:
        for name in ("goal_id", "objective", "plan"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        positive_int(self.revision)
        if not self.criteria or any(not isinstance(item, Criterion) for item in self.criteria):
            raise ContractValidationError("baseline requires criteria")
        items = tuple(self.criteria)
        if len({item.criterion_id for item in items}) != len(items):
            raise ContractValidationError("criterion IDs must be unique")
        object.__setattr__(self, "criteria", items)
        object.__setattr__(self, "constraints", strings(self.constraints, "constraints"))
        object.__setattr__(self, "required_operations", strings(self.required_operations, "required_operations"))
        raw_dependencies = self.decision_dependencies
        if isinstance(raw_dependencies, Mapping):
            raw_items = list(raw_dependencies.items())
        elif isinstance(raw_dependencies, tuple):
            raw_items = list(raw_dependencies)
        else:
            raise ContractValidationError("decision_dependencies must be an object")
        dependencies: dict[str, tuple[str, ...]] = {}
        for pair in raw_items:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ContractValidationError("decision_dependencies entries must be key/item pairs")
            raw_key, raw_decision_items = pair
            key = text(raw_key, "decision_key")
            if key in dependencies:
                raise ContractValidationError("decision_dependencies keys collide after normalization")
            if not isinstance(raw_decision_items, (tuple, list)):
                raise ContractValidationError("decision work items must be an array")
            items_for_decision = strings(raw_decision_items, "decision work items")
            if len(set(items_for_decision)) != len(items_for_decision):
                raise ContractValidationError("decision work items contain duplicates")
            dependencies[key] = tuple(sorted(items_for_decision))
        object.__setattr__(self, "decision_dependencies", tuple(sorted(dependencies.items())))

    def to_dict(self) -> dict[str, Any]:
        data = {
            "goal_id": self.goal_id,
            "revision": self.revision,
            "objective": self.objective,
            "plan": self.plan,
            "criteria": [item.to_dict() for item in self.criteria],
            "constraints": list(self.constraints),
            "required_operations": list(self.required_operations),
        }
        dependencies = self._normalized_decision_dependencies()
        if dependencies:
            data["decision_dependencies"] = {
                key: list(items) for key, items in dependencies
            }
        return data

    def _normalized_decision_dependencies(self) -> DecisionDependencyPairs:
        dependencies = self.decision_dependencies
        if not isinstance(dependencies, tuple):
            raise ContractValidationError("decision_dependencies are not normalized")
        return tuple((key, tuple(items)) for key, items in dependencies)

    @property
    def decision_dependency_map(self) -> Mapping[str, tuple[str, ...]]:
        """Read-only mapping view of the canonical dependency pairs."""
        return MappingProxyType(dict(self._normalized_decision_dependencies()))

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Baseline:
        data = shape(
            value,
            {"goal_id", "revision", "objective", "plan", "criteria"},
            {"constraints", "required_operations", "decision_dependencies"},
        )
        if "decision_dependencies" in data and not isinstance(data["decision_dependencies"], Mapping):
            raise ContractValidationError("decision_dependencies must be an object")
        data["criteria"] = tuple(Criterion.from_dict(item) for item in data["criteria"])
        return cls(**data)


@dataclass(frozen=True)
class Operation:
    operation_id: str
    goal_id: str
    baseline_digest: str
    tool: str
    arguments_json: str
    cwd: str
    work_item_id: str
    environment: str = "local"
    account: str = "default"
    resource_preconditions: tuple[tuple[str, str | None], ...] = ()

    def __post_init__(self) -> None:
        for name in ("operation_id", "goal_id", "tool", "cwd", "work_item_id", "environment", "account"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        object.__setattr__(self, "baseline_digest", sha256_text(self.baseline_digest, "baseline_digest"))
        args = strict_loads(self.arguments_json)
        if not isinstance(args, dict):
            raise ContractValidationError("operation arguments must be an object")
        object.__setattr__(self, "arguments_json", canonical(args))
        pairs: dict[str, str | None] = {}
        for pair in self.resource_preconditions:
            if isinstance(pair, (str, bytes)) or len(pair) != 2:
                raise ContractValidationError("invalid resource precondition pair")
            path = text(pair[0], "resource path")
            if path in pairs:
                raise ContractValidationError("duplicate resource precondition")
            pairs[path] = None if pair[1] is None else sha256_text(pair[1])
        object.__setattr__(self, "resource_preconditions", tuple(sorted(pairs.items())))

    @property
    def arguments(self) -> dict[str, Any]:
        return dict(strict_loads(self.arguments_json))

    def to_dict(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id, "goal_id": self.goal_id, "baseline_digest": self.baseline_digest,
                "tool": self.tool, "arguments": self.arguments, "cwd": self.cwd, "work_item_id": self.work_item_id,
                "environment": self.environment, "account": self.account,
                "resource_preconditions": [list(pair) for pair in self.resource_preconditions]}

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Operation:
        data = shape(value, {"operation_id", "goal_id", "baseline_digest", "tool", "arguments", "cwd", "work_item_id"},
                     {"environment", "account", "resource_preconditions"})
        arguments = data.pop("arguments")
        return cls(**data, arguments_json=canonical(arguments))


@dataclass(frozen=True)
class EvidenceSnapshot:
    evidence_id: str
    object_ref: str
    content_base64: str
    sha256: str
    source: str
    collected_at: str
    expires_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("evidence_id", "object_ref", "source"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        if not isinstance(self.content_base64, str):
            raise ContractValidationError("invalid snapshot content: expected base64 text")
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ContractValidationError("invalid snapshot content") from exc
        digest = sha256_text(self.sha256)
        if hashlib.sha256(content).hexdigest() != digest:
            raise ContractValidationError("snapshot content digest mismatch")
        object.__setattr__(self, "sha256", digest)
        collected = parse_time(self.collected_at)
        if self.expires_at is not None and parse_time(self.expires_at) <= collected:
            raise ContractValidationError("snapshot expiry must follow collection")

    @property
    def content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.to_dict())

    def valid_at(self, at: str) -> bool:
        now = parse_time(at)
        return now >= parse_time(self.collected_at) and (self.expires_at is None or now < parse_time(self.expires_at))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceSnapshot:
        return cls(**shape(value, {"evidence_id", "object_ref", "content_base64", "sha256", "source", "collected_at"},
                           {"expires_at"}))

    @classmethod
    def capture(cls, evidence_id: str, object_ref: str, content: bytes, *, source: str,
                collected_at: str | None = None, expires_at: str | None = None) -> EvidenceSnapshot:
        if not isinstance(content, bytes):
            raise ContractValidationError("snapshot content must be bytes")
        return cls(evidence_id, object_ref, base64.b64encode(content).decode("ascii"),
                   hashlib.sha256(content).hexdigest(), source, collected_at or utc_now(), expires_at)
