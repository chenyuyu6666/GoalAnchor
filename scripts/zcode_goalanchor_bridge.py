"""Experimental one-shot bridge for advisory legacy reviews, never Strict proof.

Set GOALANCHOR_ZCODE_CLI to the absolute path of the installed zcode.cjs bundle.
The default-model CLI does not establish model identity, independent sessions,
delivery receipts or host tool enforcement. An ``isolated`` report is only a
model claim; ``strict_binding`` requests are refused before launching ZCode.
The requested plan mode/Bash-Edit-Write denylist is not a universal tool fence.

Only a single direct role-result JSON object or an explicitly successful
status/resultType envelope is understood. Static inspection of local ZCode
CLI 0.17.0 found a ``type: result`` envelope with response/session/usage and
projection but no explicit terminal success marker. That shape remains
unsupported until a separate host-controlled transport verifies completion.
Unknown output never becomes review evidence. Timeout means UNKNOWN; killing
the CLI process does not prove cancellation of its tools or remote operations.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from goalanchor.agent_runtime import AgentRequest, AgentRole, AgentRuntimeError  # noqa: E402
from goalanchor.platforms.platform_prompt import render_agent_prompt  # noqa: E402
from goalanchor.review_service import (  # noqa: E402
    ArbiterResult,
    DisputePacket,
    MetaReviewResult,
    ReviewContext,
    ReviewerResult,
    ReviewRisk,
)

MAX_PROMPT_BYTES = 28_000
MAX_INPUT_CHARS = 1_048_576
_ENVELOPE_FIELDS = frozenset({"status", "resultType", "success", "result", "response"})
_REVIEWER_FIELDS = frozenset(
    {"reviewer_id", "round_number", "verdict", "findings", "isolated", "context_fingerprint"}
)
_ARBITER_FIELDS = frozenset(
    {
        "arbiter_id",
        "round_number",
        "verdict",
        "summary",
        "rationale",
        "reviewer_ids",
        "context_fingerprint",
        "disagrees_with_reviewers",
    }
)
_META_FIELDS = frozenset(
    {
        "reviewer_id",
        "round_number",
        "arbiter_id",
        "verdict",
        "summary",
        "findings",
        "context_fingerprint",
        "agrees_with_arbiter",
        "isolated",
    }
)


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentRuntimeError(f"{field_name} must be an object")
    return value


def _fields(payload: Mapping[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    # Do not erase failure flags, unverified model metadata or missing fields.
    if set(payload) != allowed:
        raise AgentRuntimeError("role result has missing or unsupported fields")
    return dict(payload)


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentRuntimeError("JSON object contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AgentRuntimeError("JSON contains a non-finite number")


def _json_object(raw: str, field_name: str) -> Mapping[str, Any]:
    return _mapping(
        json.loads(raw, object_pairs_hook=_unique_fields, parse_constant=_reject_constant),
        field_name,
    )


def _parse_output(raw: str) -> Mapping[str, Any]:
    payload = _json_object(raw, "CLI output")
    if "verdict" in payload:
        return payload  # The complete role schema is validated next.
    if set(payload) - _ENVELOPE_FIELDS:
        raise AgentRuntimeError("unsupported CLI envelope")
    markers = [payload[key] for key in ("status", "resultType") if key in payload]
    if not markers or any(marker != "success" for marker in markers):
        raise AgentRuntimeError("CLI envelope has no unambiguous success status")
    if "success" in payload and payload["success"] is not True:
        raise AgentRuntimeError("CLI envelope reports failure")
    contents = [payload[key] for key in ("result", "response") if key in payload]
    if len(contents) != 1:
        raise AgentRuntimeError("CLI envelope must contain exactly one result")
    content = contents[0]
    return _json_object(content, "CLI result") if isinstance(content, str) else _mapping(content, "CLI result")


def _request_from_dict(payload: Mapping[str, Any]) -> AgentRequest:
    if payload.get("protocol") != "goalanchor.agent.v1":
        raise AgentRuntimeError("unsupported GoalAnchor agent protocol")
    raw_context = _mapping(payload.get("context"), "context")
    raw_dispute = payload.get("dispute_packet")
    raw_arbiter = payload.get("arbiter_result")
    raw_reviewers = payload.get("reviewer_results", ())
    if not isinstance(raw_reviewers, list):
        raise AgentRuntimeError("reviewer_results must be an array")
    return AgentRequest(
        role=AgentRole(payload["role"]),
        agent_id=payload["agent_id"],
        round_number=payload["round_number"],
        risk=ReviewRisk(payload["risk"]),
        context=ReviewContext.from_dict(raw_context),
        dispute_packet=(
            None if raw_dispute is None else DisputePacket.from_dict(_mapping(raw_dispute, "dispute_packet"))
        ),
        reviewer_results=tuple(
            ReviewerResult.from_dict(_mapping(item, "reviewer_results[]")) for item in raw_reviewers
        ),
        arbiter_result=(
            None if raw_arbiter is None else ArbiterResult.from_dict(_mapping(raw_arbiter, "arbiter_result"))
        ),
    )


def _validate_result(request: AgentRequest, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Parse and bind the untrusted ZCode result before writing stdout."""

    if request.role is AgentRole.REVIEWER:
        reviewer_result = ReviewerResult.from_dict(_fields(payload, _REVIEWER_FIELDS))
        if (
            reviewer_result.reviewer_id != request.agent_id
            or reviewer_result.round_number != request.round_number
            or reviewer_result.context_fingerprint != request.context.fingerprint
            or not reviewer_result.isolated
        ):
            raise AgentRuntimeError("ZCode reviewer result is not bound to this request")
        return reviewer_result.to_dict()
    if request.role is AgentRole.ARBITER:
        arbiter_result = ArbiterResult.from_dict(_fields(payload, _ARBITER_FIELDS))
        expected_reviewer_ids = tuple(item.reviewer_id for item in request.reviewer_results)
        if (
            arbiter_result.arbiter_id != request.agent_id
            or arbiter_result.round_number != request.round_number
            or arbiter_result.context_fingerprint != request.context.fingerprint
            or tuple(arbiter_result.reviewer_ids) != expected_reviewer_ids
        ):
            raise AgentRuntimeError("ZCode arbiter result is not bound to this request")
        return arbiter_result.to_dict()
    if request.arbiter_result is None:
        raise AgentRuntimeError("meta reviewer request is missing arbiter_result")
    meta_result = MetaReviewResult.from_dict(_fields(payload, _META_FIELDS))
    if (
        meta_result.reviewer_id != request.agent_id
        or meta_result.round_number != request.round_number
        or meta_result.arbiter_id != request.arbiter_result.arbiter_id
        or meta_result.context_fingerprint != request.context.fingerprint
        or not meta_result.isolated
    ):
        raise AgentRuntimeError("ZCode meta-review result is not bound to this request")
    return meta_result.to_dict()


def _run(request: AgentRequest) -> dict[str, Any]:
    if request.context.strict_binding is not None:
        raise AgentRuntimeError("Strict review proof is unavailable through the one-shot ZCode bridge")
    prompt = render_agent_prompt(request, language="en")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise AgentRuntimeError(
            f"ZCode prompt exceeds the safe Windows argv limit ({MAX_PROMPT_BYTES} bytes)"
        )
    node = shutil.which(os.environ.get("GOALANCHOR_ZCODE_NODE", "node"))
    if node is None:
        raise AgentRuntimeError("GOALANCHOR_ZCODE_NODE executable is unavailable")
    cli = os.environ.get("GOALANCHOR_ZCODE_CLI", "")
    if not cli or not Path(cli).is_absolute() or not Path(cli).is_file():
        raise AgentRuntimeError("GOALANCHOR_ZCODE_CLI must name an existing absolute bundle path")
    cwd = os.environ.get("GOALANCHOR_ZCODE_CWD", os.getcwd())
    if not Path(cwd).is_dir():
        raise AgentRuntimeError("GOALANCHOR_ZCODE_CWD directory is unavailable")
    try:
        timeout = float(os.environ.get("GOALANCHOR_ZCODE_TIMEOUT_SECONDS", "300"))
    except ValueError as exc:
        raise AgentRuntimeError("ZCode timeout must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise AgentRuntimeError("ZCode timeout must be a positive finite number")
    completed = subprocess.run(
        [
            node,
            cli,
            "-p",
            prompt,
            "--json",
            "--no-color",
            "--mode",
            "plan",
            "--disallowed-tools",
            "Bash Edit Write",
            "--cwd",
            cwd,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        cwd=cwd,
        encoding="utf-8",
        errors="replace",
        env=os.environ.copy(),
        shell=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AgentRuntimeError(f"ZCode CLI exited with code {completed.returncode}; diagnostics withheld")
    return _validate_result(request, _parse_output(completed.stdout))


def main() -> int:
    try:
        line = sys.stdin.readline(MAX_INPUT_CHARS + 1)
        if len(line) > MAX_INPUT_CHARS:
            raise AgentRuntimeError("bridge input exceeds its size limit")
        if not line.strip():
            raise AgentRuntimeError("bridge input is empty")
        request = _request_from_dict(_json_object(line, "request"))
        result = _run(request)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    except AgentRuntimeError as exc:
        print(f"zcode_goalanchor_bridge: {exc}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        # TimeoutExpired.__str__ includes the argv, hence the frozen prompt.
        print("zcode_goalanchor_bridge: CLI timed out; completion is UNKNOWN", file=sys.stderr)
        return 1
    except (OSError, TimeoutError):
        print("zcode_goalanchor_bridge: CLI transport unavailable", file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError, RecursionError):
        print("zcode_goalanchor_bridge: invalid request or CLI result", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
