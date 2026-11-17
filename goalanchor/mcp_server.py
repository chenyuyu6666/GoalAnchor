"""Zero-dependency MCP server exposing GoalAnchor's deterministic gates.

Why MCP instead of a host plugin: a plugin binds to a host's private extension
API, and PI-Desktop 0.22.0 already broke that surface three times (per-turn
runtime rebuild, composer slash routing, palette RUNTIME_NOT_FOUND). MCP is a
published protocol, so a host upgrade does not invalidate this server.

Transport: newline-delimited JSON-RPC 2.0 over stdio, per the MCP spec. Only
standard library modules are used, because the tool must run inside whatever
Python a host already ships.

Tools exposed (all read-only, none grants execution permission):
  goalanchor_gate          validate a PLAN/VERIFY/EXECUTE/closure/... record
  goalanchor_capabilities  report protocol version, gates and boundaries

Run:
    python -m goalanchor.mcp_server
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._version import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-11-25", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = PROTOCOL_VERSIONS[0]
SERVER_NAME = "goalanchor"

GATES = (
    "plan", "verify", "execute", "plan-change", "evidence", "conclusion",
    "experiment", "design", "artifact", "artifact-file", "provenance",
    "closure", "review", "review-cycle", "graphify",
)


def _gate_result(phase: str, record: Any) -> dict[str, Any]:
    """Run one pure gate over an inline record (no filesystem writes)."""

    import tempfile

    from .compat import _run_gate

    if phase not in GATES:
        raise ValueError(f"unsupported gate phase: {phase}")
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    with tempfile.TemporaryDirectory(prefix="goalanchor-mcp-") as directory:
        path = Path(directory) / "record.json"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return _run_gate(phase, path)


def _tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "goalanchor_gate",
            "description": (
                "Validate a GoalAnchor record with one deterministic gate. "
                "Phases: " + ", ".join(GATES) + ". "
                "Returns {passed, findings, ...}. Never grants execution permission; "
                "a PASS here only means the record satisfies that gate's rules."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "phase": {"type": "string", "enum": list(GATES)},
                    "record": {"type": "object"},
                },
                "required": ["phase", "record"],
            },
        },
        {
            "name": "goalanchor_capabilities",
            "description": (
                "Report the GoalAnchor MCP surface: version, protocol versions, gate list, "
                "and the honest boundary (deterministic gates only; no model review, "
                "no tool interception, no execution grant)."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _call_tool(name: str, arguments: Any) -> dict[str, Any]:
    args = arguments if isinstance(arguments, dict) else {}
    if name == "goalanchor_capabilities":
        payload = {
            "server": SERVER_NAME,
            "version": __version__,
            "protocol_versions": list(PROTOCOL_VERSIONS),
            "gates": list(GATES),
            "boundaries": {
                "execution_grant": False,
                "tool_interception": False,
                "model_review": False,
                "note": (
                    "This server only evaluates records. Blocking an unapproved tool call "
                    "requires a host-side gate; MCP cannot intercept the host's own tools."
                ),
            },
        }
        return _text_result(json.dumps(payload, ensure_ascii=False, indent=1))
    if name == "goalanchor_gate":
        phase = args.get("phase")
        if not isinstance(phase, str):
            raise ValueError("phase must be a string")
        result = _gate_result(phase, args.get("record"))
        return _text_result(json.dumps(result, ensure_ascii=False, indent=1))
    raise ValueError(f"unknown tool: {name}")


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _negotiate(requested: Any) -> str:
    if isinstance(requested, str) and requested in PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL


def handle(message: Any) -> dict[str, Any] | None:
    """Handle one JSON-RPC message; return a response or None for notifications."""

    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request"}}
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None  # notification: nothing to answer
    params = message.get("params")
    try:
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": _negotiate(requested),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _tool_specs()}}
        if method == "tools/call":
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            name = params.get("name")
            if not isinstance(name, str):
                raise ValueError("tool name must be a string")
            try:
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": _call_tool(name, params.get("arguments"))}
            except (ValueError, KeyError, TypeError) as exc:
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": _text_result(f"{type(exc).__name__}: {exc}", is_error=True)}
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception as exc:  # noqa: BLE001 - one bad request must not kill the server
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """Serve NDJSON JSON-RPC until stdin closes."""

    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    for line in source:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            response: dict[str, Any] | None = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc.msg}"},
            }
        else:
            response = handle(message)
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=False) + "\n")
            sink.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure: Callable[..., Any] | None = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
