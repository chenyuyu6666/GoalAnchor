"""App-Server transport bridge for GoalAnchor role dispatch on ZCode.

Unlike the CLI bridge (zcode_goalanchor_bridge.py), this bridge drives the
ZCode ``app-server`` NDJSON protocol through ProbeClient, so turn completion
is verified against a host-controlled terminal event (turn.completed with
resultType=success, zero tool calls, correlated session/input binding) instead
of trusting a CLI exit code. Advisory only: role identity/round/digest checks
match the CLI bridge; this still never fabricates Strict proof —
``strict_binding`` requests are refused before any launch.

Environment: GOALANCHOR_ZCODE_NODE, GOALANCHOR_ZCODE_CLI (absolute bundle
path), GOALANCHOR_ZCODE_CWD (isolated workspace), GOALANCHOR_ZCODE_PROVIDER,
GOALANCHOR_ZCODE_MODEL, optional GOALANCHOR_ZCODE_REASONING_LEVEL and
GOALANCHOR_ZCODE_TIMEOUT_SECONDS.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.zcode_goalanchor_bridge import (  # noqa: E402
    MAX_INPUT_CHARS,
    AgentRuntimeError,
    _json_object,
    _request_from_dict,
    _validate_result,
)
from scripts.zcode_protocol_probe import NdjsonTransport, ProbeClient  # noqa: E402


def _build_client_factory() -> tuple[Callable[[str], ProbeClient], Path]:
    node = os.environ.get("GOALANCHOR_ZCODE_NODE", "node")
    node_exe = shutil.which(node)
    if node_exe is None:
        raise AgentRuntimeError("GOALANCHOR_ZCODE_NODE executable is unavailable")
    cli = os.environ.get("GOALANCHOR_ZCODE_CLI", "")
    if not cli or not Path(cli).is_absolute() or not Path(cli).is_file():
        raise AgentRuntimeError("GOALANCHOR_ZCODE_CLI must name an existing absolute bundle path")
    workspace = Path(os.environ.get("GOALANCHOR_ZCODE_CWD", os.getcwd()))
    if not workspace.is_dir():
        raise AgentRuntimeError("GOALANCHOR_ZCODE_CWD workspace directory is unavailable")
    provider = os.environ.get("GOALANCHOR_ZCODE_PROVIDER", "")
    model = os.environ.get("GOALANCHOR_ZCODE_MODEL", "")
    if not provider.strip() or not model.strip():
        raise AgentRuntimeError("GOALANCHOR_ZCODE_PROVIDER and GOALANCHOR_ZCODE_MODEL are required")
    reasoning = os.environ.get("GOALANCHOR_ZCODE_REASONING_LEVEL", "").strip() or None
    try:
        timeout = float(os.environ.get("GOALANCHOR_ZCODE_TIMEOUT_SECONDS", "300"))
    except ValueError as exc:
        raise AgentRuntimeError("ZCode timeout must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise AgentRuntimeError("ZCode timeout must be a positive finite number")

    def factory(prompt: str) -> ProbeClient:
        transport = NdjsonTransport([node_exe, cli, "app-server"], cwd=workspace.resolve())
        return ProbeClient(transport, workspace=workspace, provider=provider, model=model,
                           timeout=timeout, reasoning_level=reasoning, prompt=prompt,
                           capture_response=True)

    return factory, workspace


def run_request(request: Any, client_factory: Callable[[str], ProbeClient]) -> dict[str, Any]:
    from goalanchor.platforms.platform_prompt import render_agent_prompt

    prompt = render_agent_prompt(request, language="en")
    if len(prompt.encode("utf-8")) > 28_000:
        raise AgentRuntimeError("ZCode prompt exceeds the safe transport limit")
    probe = client_factory(prompt)
    transport = probe.transport
    try:
        result = probe.run(send=True)
    finally:
        transport.close(probe.cleanup_timeout)
    terminal = result.get("terminal") or {}
    cleanup = result.get("cleanup") or {}
    if result.get("outcome") != "SUCCESS" or not cleanup.get("closed"):
        raise AgentRuntimeError("ZCode app-server turn did not verify as completed; diagnostics withheld")
    if terminal.get("tool_call_count") != 0:
        raise AgentRuntimeError("ZCode app-server turn used tools; result refused")
    response = terminal.get("response_text")
    if not isinstance(response, str) or not response.strip():
        raise AgentRuntimeError("ZCode app-server completion carried no response text")
    return _validate_result(request, _json_object(response, "response"))


def main() -> int:
    try:
        line = sys.stdin.readline(MAX_INPUT_CHARS + 1)
        if len(line) > MAX_INPUT_CHARS:
            raise AgentRuntimeError("bridge input exceeds its size limit")
        if not line.strip():
            raise AgentRuntimeError("bridge input is empty")
        request = _request_from_dict(_json_object(line, "request"))
        factory, _workspace = _build_client_factory()
        result = run_request(request, factory)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    except AgentRuntimeError as exc:
        print(f"zcode_appserver_bridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
