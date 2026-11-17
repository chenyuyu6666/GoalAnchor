"""MCP server contract tests: handshake, tool surface, gate results, boundaries."""

from __future__ import annotations

import io
import json

from goalanchor._version import __version__
from goalanchor.mcp_server import GATES, LATEST_PROTOCOL, PROTOCOL_VERSIONS, handle, serve

CLOSURE_RECORD = {
    "closure": {
        "requested_level": "Root Fix",
        "evidence_refs": ["audit/pi-desktop-runtime-20261114.json"],
        "containment_scope": "PI-Desktop 0.22.0 guard runtime",
        "root_cause_proven": True,
        "fix_change_ref": "PR #38-#42",
        "build_success": True,
        "test_object": "Guard runtime gate",
    }
}
OVERCLAIMED = {"closure": {"requested_level": "Product Closure", "evidence_refs": ["x"],
                           "root_cause_proven": True, "build_success": True}}


def _request(method: str, params: object = None, request_id: int = 1) -> dict:
    message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def test_initialize_negotiates_a_supported_version() -> None:
    for offered in (None, *PROTOCOL_VERSIONS, "1999-01-01"):
        response = handle(_request("initialize", {"protocolVersion": offered}))
        assert response is not None
        result = response["result"]
        assert result["protocolVersion"] in PROTOCOL_VERSIONS
        assert result["serverInfo"]["name"] == "goalanchor"
        assert result["serverInfo"]["version"] == __version__
        assert result["capabilities"]["tools"]["listChanged"] is False
    assert handle(_request("initialize", {}))["result"]["protocolVersion"] == LATEST_PROTOCOL


def test_notifications_get_no_response() -> None:
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_gate_and_capabilities_only() -> None:
    tools = handle(_request("tools/list"))["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == ["goalanchor_gate", "goalanchor_capabilities"]
    gate_schema = next(tool for tool in tools if tool["name"] == "goalanchor_gate")["inputSchema"]
    assert gate_schema["required"] == ["phase", "record"]
    assert set(gate_schema["properties"]["phase"]["enum"]) == set(GATES)


def test_gate_tool_passes_a_complete_root_fix_record() -> None:
    response = handle(_request("tools/call", {"name": "goalanchor_gate",
                                             "arguments": {"phase": "closure",
                                                           "record": CLOSURE_RECORD}}))
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["passed"] is True
    assert payload["gate_id"] == "CLOSURE"
    assert response["result"]["isError"] is False


def test_gate_tool_fails_closed_on_an_overclaimed_level() -> None:
    response = handle(_request("tools/call", {"name": "goalanchor_gate",
                                             "arguments": {"phase": "closure",
                                                           "record": OVERCLAIMED}}))
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["passed"] is False
    assert payload["findings"][0]["code"] == "ARTIFACT_MANIFEST_REQUIRED"
    assert payload["recommended_level"] == "Build Success"


def test_gate_tool_rejects_unknown_phase_and_bad_arguments_as_tool_errors() -> None:
    unknown = handle(_request("tools/call", {"name": "goalanchor_gate",
                                            "arguments": {"phase": "nope", "record": {}}}))
    assert unknown["result"]["isError"] is True
    assert "unsupported gate phase" in unknown["result"]["content"][0]["text"]
    missing = handle(_request("tools/call", {"name": "goalanchor_gate", "arguments": {}}))
    assert missing["result"]["isError"] is True
    not_object = handle(_request("tools/call", {"name": "goalanchor_gate",
                                               "arguments": {"phase": "closure", "record": []}}))
    assert not_object["result"]["isError"] is True


def test_capabilities_tool_states_the_honest_boundary() -> None:
    response = handle(_request("tools/call", {"name": "goalanchor_capabilities",
                                             "arguments": {}}))
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["server"] == "goalanchor"
    assert payload["version"] == __version__
    assert payload["boundaries"]["execution_grant"] is False
    assert payload["boundaries"]["tool_interception"] is False
    assert payload["boundaries"]["model_review"] is False


def test_unknown_method_and_malformed_messages_are_reported() -> None:
    assert handle(_request("tools/nope"))["error"]["code"] == -32601
    assert handle({"jsonrpc": "2.0", "id": 1})["error"]["code"] == -32601
    assert handle("not an object")["error"]["code"] == -32600


def test_serve_round_trips_ndjson_and_survives_bad_lines() -> None:
    lines = [
        json.dumps(_request("initialize", {"protocolVersion": "2025-06-18"}, 1)),
        "{not json",
        json.dumps(_request("tools/list", None, 2)),
        "",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps(_request("ping", None, 3)),
    ]
    out = io.StringIO()
    assert serve(io.StringIO("\n".join(lines) + "\n"), out) == 0
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [item["id"] for item in responses] == [1, None, 2, 3]
    assert responses[1]["error"]["code"] == -32700
    assert responses[3]["result"] == {}
