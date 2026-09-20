"""Decision audit records and lightweight action authorization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from .core import ContractValidationError, LifecycleState
from .gates import GateFinding, GateId, GateResult


class DecisionActor(str, Enum):
    USER = "USER"
    AGENT = "AGENT"
    POLICY = "POLICY"
    REVIEW = "REVIEW"


class AuthorizationKind(str, Enum):
    ONE_TIME = "ONE_TIME"
    AUTONOMY_GRANT = "AUTONOMY_GRANT"


class AuthorizationScope(str, Enum):
    WORK_ITEM = "WORK_ITEM"
    SESSION = "SESSION"
    PROJECT = "PROJECT"


class ActionType(str, Enum):
    READ = "READ"
    SEARCH = "SEARCH"
    ANALYZE = "ANALYZE"
    BUILD = "BUILD"
    TEST = "TEST"
    LINT = "LINT"
    SOURCE_EDIT = "SOURCE_EDIT"
    CONFIG_EDIT = "CONFIG_EDIT"
    PROMPT_EDIT = "PROMPT_EDIT"
    DELETE = "DELETE"
    DEPLOY = "DEPLOY"
    GOAL_CHANGE = "GOAL_CHANGE"
    ACCEPTANCE_CHANGE = "ACCEPTANCE_CHANGE"
    LONG_TERM_MEMORY_WRITE = "LONG_TERM_MEMORY_WRITE"


_AUTO_ACTIONS = frozenset({
    ActionType.READ,
    ActionType.SEARCH,
    ActionType.ANALYZE,
    ActionType.BUILD,
    ActionType.TEST,
    ActionType.LINT,
})

_EXPLICIT_ACTIONS = frozenset({
    ActionType.DELETE,
    ActionType.DEPLOY,
    ActionType.GOAL_CHANGE,
    ActionType.ACCEPTANCE_CHANGE,
    ActionType.LONG_TERM_MEMORY_WRITE,
})


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(f"{field_name} is invalid") from exc


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{field_name} must be an array of strings")
    return tuple(_text(item, f"{field_name}[]") for item in value)


def _instant(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractValidationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractValidationError(f"{field_name} must include a timezone")
    return text


@dataclass(frozen=True)
class Authorization:
    authorization_id: str
    kind: AuthorizationKind
    scope: AuthorizationScope
    scope_ref: str
    actions: tuple[ActionType, ...]
    granted_by: DecisionActor
    granted_at: str
    expires_at: str | None = None
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "authorization_id", _text(self.authorization_id, "authorization_id"))
        object.__setattr__(self, "kind", _enum(self.kind, AuthorizationKind, "kind"))
        object.__setattr__(self, "scope", _enum(self.scope, AuthorizationScope, "scope"))
        object.__setattr__(self, "scope_ref", _text(self.scope_ref, "scope_ref"))
        actions = tuple(_enum(item, ActionType, "actions[]") for item in self.actions)
        if not actions:
            raise ContractValidationError("actions must contain at least one item")
        if len(actions) != len(set(actions)):
            raise ContractValidationError("actions must not contain duplicates")
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "granted_by", _enum(self.granted_by, DecisionActor, "granted_by"))
        object.__setattr__(self, "granted_at", _instant(self.granted_at, "granted_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _instant(self.expires_at, "expires_at"))
        object.__setattr__(self, "constraints", _strings(self.constraints, "constraints"))

    def is_valid_for(
        self,
        action: ActionType,
        scope: AuthorizationScope,
        scope_ref: str,
        *,
        at: str,
    ) -> bool:
        requested_action = _enum(action, ActionType, "action")
        requested_scope = _enum(scope, AuthorizationScope, "scope")
        requested_ref = _text(scope_ref, "scope_ref")
        now = datetime.fromisoformat(_instant(at, "at").replace("Z", "+00:00"))
        if requested_action not in self.actions:
            return False
        if self.scope is not requested_scope or self.scope_ref != requested_ref:
            return False
        granted = datetime.fromisoformat(self.granted_at.replace("Z", "+00:00"))
        if now < granted:
            return False
        if self.expires_at is None:
            return True
        expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return now < expires

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorization_id": self.authorization_id,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "scope_ref": self.scope_ref,
            "actions": [item.value for item in self.actions],
            "granted_by": self.granted_by.value,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "constraints": list(self.constraints),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Authorization":
        return cls(**dict(payload))


@dataclass(frozen=True)
class DecisionRecord:
    record_id: str
    selected_option: str
    reason: str
    decided_by: DecisionActor
    created_at: str
    alternatives: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    constraint_refs: tuple[str, ...] = ()
    authorized_by: DecisionActor | None = None
    authorization_id: str | None = None
    goal_id: str | None = None
    work_item_id: str | None = None
    design_decision_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", _text(self.record_id, "record_id"))
        object.__setattr__(self, "selected_option", _text(self.selected_option, "selected_option"))
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(self, "decided_by", _enum(self.decided_by, DecisionActor, "decided_by"))
        object.__setattr__(self, "created_at", _instant(self.created_at, "created_at"))
        for field_name in ("alternatives", "evidence_refs", "constraint_refs"):
            object.__setattr__(self, field_name, _strings(getattr(self, field_name), field_name))
        if self.authorized_by is not None:
            object.__setattr__(
                self, "authorized_by", _enum(self.authorized_by, DecisionActor, "authorized_by")
            )
        for field_name in ("authorization_id", "goal_id", "work_item_id", "design_decision_id"):
            object.__setattr__(self, field_name, _optional_text(getattr(self, field_name), field_name))
        if (self.authorized_by is None) != (self.authorization_id is None):
            raise ContractValidationError(
                "authorized_by and authorization_id must be provided together"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "selected_option": self.selected_option,
            "reason": self.reason,
            "decided_by": self.decided_by.value,
            "created_at": self.created_at,
            "alternatives": list(self.alternatives),
            "evidence_refs": list(self.evidence_refs),
            "constraint_refs": list(self.constraint_refs),
            "authorized_by": None if self.authorized_by is None else self.authorized_by.value,
            "authorization_id": self.authorization_id,
            "goal_id": self.goal_id,
            "work_item_id": self.work_item_id,
            "design_decision_id": self.design_decision_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DecisionRecord":
        return cls(**dict(payload))


def check_action_authorization(
    action: ActionType,
    *,
    scope: AuthorizationScope,
    scope_ref: str,
    at: str,
    authorizations: Sequence[Authorization] = (),
) -> GateResult:
    """Apply the default action policy and any explicit scoped grants."""

    requested_action = _enum(action, ActionType, "action")
    requested_scope = _enum(scope, AuthorizationScope, "scope")
    requested_ref = _text(scope_ref, "scope_ref")
    _instant(at, "at")
    if requested_action in _AUTO_ACTIONS:
        return GateResult(gate_id=GateId.EXECUTE, passed=True)
    for authorization in tuple(authorizations):
        if not isinstance(authorization, Authorization):
            raise ContractValidationError("authorizations must contain Authorization values")
        if authorization.is_valid_for(
            requested_action,
            requested_scope,
            requested_ref,
            at=at,
        ):
            return GateResult(gate_id=GateId.EXECUTE, passed=True)
    code = (
        "EXPLICIT_CONFIRMATION_REQUIRED"
        if requested_action in _EXPLICIT_ACTIONS
        else "AUTHORIZATION_REQUIRED"
    )
    return GateResult(
        gate_id=GateId.EXECUTE,
        passed=False,
        findings=(
            GateFinding(
                code=code,
                message=f"{requested_action.value} requires a matching user authorization",
                field="authorization",
            ),
        ),
        next_state=LifecycleState.HUMAN_DECISION_REQUIRED,
    )


__all__ = [
    "ActionType",
    "Authorization",
    "AuthorizationKind",
    "AuthorizationScope",
    "DecisionActor",
    "DecisionRecord",
    "check_action_authorization",
]
