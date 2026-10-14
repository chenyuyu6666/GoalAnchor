"""Shared constants and field validators for platform dispatch.

The validators normalize values entering :class:`~goalanchor.platforms.PlatformSpec`
and :class:`~goalanchor.platforms.ProfileBinding` and raise the runtime's own
error type so config problems surface consistently.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import TypeVar, cast

from ..agent_runtime import AgentRole, AgentRuntimeError

_E = TypeVar("_E")

BUILTIN_PLATFORMS: tuple[str, ...] = ("codex", "claude", "gemini", "opencode")

# All built-in templates pipe the prompt through stdin (no ``{prompt}`` argv
# slot). On Windows these CLIs are .cmd shims, and multi-line prompts passed
# as argv get mangled by cmd.exe quoting; stdin also removes the ~32K argv
# length limit for large review contexts.
_BUILTIN_ARGV: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "exec", "-"),
    "claude": ("claude", "-p"),
    "gemini": ("gemini",),
    "opencode": ("opencode", "run"),
}

# Model flag used to pin each built-in platform's sub-agent to the main
# agent's model. Custom platforms either place a ``{model}`` placeholder in
# their argv or rely on the default ``-m`` insertion (see ``_apply_model``).
_BUILTIN_MODEL_FLAGS: dict[str, str] = {
    "codex": "-m",
    "claude": "--model",
    "gemini": "-m",
    "opencode": "-m",
}

_ENVELOPE_KEYS: tuple[str, ...] = (
    "result",
    "response",
    "content",
    "output",
    "message",
    "text",
)
_RESULT_KEYS: frozenset[str] = frozenset(
    {
        "verdict",
        "reviewer_id",
        "arbiter_id",
        "summary",
        "findings",
        "disagrees_with_reviewers",
        "agrees_with_arbiter",
    }
)
_MAX_ENVELOPE_DEPTH = 8


def _enum(value: object, enum_type: type[_E], field_name: str) -> _E:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except (TypeError, ValueError) as exc:
        raise AgentRuntimeError(f"{field_name} is invalid") from exc


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentRuntimeError(f"{field_name} must be a non-empty string")
    return value.strip()


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AgentRuntimeError(f"{field_name} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _string_mapping(value: object, field_name: str) -> Mapping[str, str]:
    mapping = _mapping(value, field_name)
    if any(not isinstance(item, str) for item in mapping.values()):
        raise AgentRuntimeError(f"{field_name} must map strings to strings")
    return cast(Mapping[str, str], mapping)


def _strings(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, bytes) or not isinstance(value, Iterable):
        raise AgentRuntimeError(f"{field_name} must be an array of strings")
    items = tuple(cast(Iterable[object], value))
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise AgentRuntimeError(f"{field_name} must contain non-empty strings")
    return cast(tuple[str, ...], items)


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise AgentRuntimeError(f"{field_name} must be a boolean")
    return value


def _timeout(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise AgentRuntimeError(f"{field_name} must be a positive finite number")
    return value


def _optional_timeout(value: object, field_name: str) -> float | None:
    return None if value is None else _timeout(value, field_name)


def _roles(value: object, field_name: str) -> tuple[AgentRole, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AgentRuntimeError(f"{field_name} must be an array of role strings")
    roles = tuple(_enum(item, AgentRole, f"{field_name}[]") for item in value)
    if not roles or len(roles) != len(set(roles)):
        raise AgentRuntimeError(f"{field_name} must contain unique AgentRole values")
    return roles


def _retries(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentRuntimeError(f"{field_name} must be a positive integer (1 = no retry)")
    return value


def _model(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AgentRuntimeError(f"{field_name} must be a non-empty string when present")
    return value.strip()
