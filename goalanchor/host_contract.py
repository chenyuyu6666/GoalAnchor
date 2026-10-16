"""Versioned host-neutral wire format and advisory capability descriptions.

The pure decision helper never issues a grant or dispatches an operation. Durable
execution requires Workflow's signed events and transactional checks.
"""
from __future__ import annotations

from typing import Any

from .core import ContractValidationError
from .workflow_protocol import canonical, shape, strict_loads, text

PROTOCOL = "goalanchor.workflow.v1"
_HOSTS = {"codex", "claude", "dsh", "zcode", "pi-desktop"}


def validate_host_binding(raw: str) -> str:
    facts = strict_loads(raw)
    shape(facts, {"host", "user_agent", "model", "mode", "hook_enabled", "hook_trust"})
    capabilities(facts["host"])
    for name in ("user_agent", "model", "mode", "hook_trust"):
        text(facts[name], name)
    if type(facts["hook_enabled"]) is not bool:
        raise ContractValidationError("Host binding hook_enabled must be boolean")
    return canonical(facts)


def envelope(kind: str, data: dict[str, Any]) -> str:
    text(kind, "kind")
    if not isinstance(data, dict):
        raise ContractValidationError("Envelope data must be an object")
    return canonical({"protocol": PROTOCOL, "kind": kind, "data": data})


def decode_envelope(raw: str) -> tuple[str, dict[str, Any]]:
    value = strict_loads(raw)
    shape(value, {"protocol", "kind", "data"})
    if value["protocol"] != PROTOCOL:
        raise ContractValidationError("Unsupported workflow protocol")
    text(value["kind"], "kind")
    if not isinstance(value["data"], dict):
        raise ContractValidationError("Envelope data must be an object")
    return value["kind"], value["data"]


def capabilities(host: str, *, mode: str = "skill-only") -> dict[str, Any]:
    if not isinstance(host, str) or host not in _HOSTS:
        raise ContractValidationError("Unsupported host")
    if mode not in ("skill-only", "mcp-only", "app-server"):
        raise ContractValidationError("Unsupported host mode")
    enforcement = "ADVISORY" if mode != "app-server" else "LIMITED" if host == "codex" else "UNAVAILABLE"
    return {"host": host, "mode": mode, "protocol": PROTOCOL, "core_contract": "SUPPORTED",
            "native_enforcement": enforcement,
            "question_fallback": "trusted-controller-text",
            "automatic_approval": False,
            "file_change_approval": "UNAVAILABLE",
            "note": "Installation or transport support does not establish native execution enforcement."}


def decision(host: str, *, has_baseline: bool, has_evidence: bool, review_passed: bool,
             authorized: bool, execution_supported: bool) -> str:
    capabilities(host)
    values = (has_baseline, has_evidence, review_passed, authorized, execution_supported)
    if any(type(value) is not bool for value in values):
        raise ContractValidationError("Decision inputs must be booleans")
    if not all((has_baseline, has_evidence, review_passed)):
        return "BLOCKED"
    if not authorized:
        return "WAIT_AUTHORIZATION"
    return "ELIGIBLE_FOR_RUNTIME_CHECK" if execution_supported else "UNAVAILABLE"
