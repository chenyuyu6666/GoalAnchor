"""Bounded, opt-in ZCode NDJSON probe. This is never a Strict review proof.

Uses the installed ZCode Protocol v1 shapes (desktop 3.16.3 / CLI 0.17.0).
Only self-created sessions are touched. Provider configuration is passed by path;
the client never opens it, reads credentials, approves tools, or answers questions.
Default execution verifies a fresh session without sending a model prompt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeGuard

MARKER = "ZCODE_GOALANCHOR_PROTOCOL_OK"
FIXED_PROMPT = f"Do not use any tools. Reply with exactly {MARKER} and nothing else."
DELIVERY = "desktop-continuous"
RESULT_TYPES = {"success", "cancelled", "error_max_turns", "error_max_budget",
                "error_during_execution", "error_max_tool_calls"}
MAX_FRAMES = 4096
SAFE_ERROR_CODES = {"401", "402", "403", "408", "429", "500", "502", "503", "504",
                    "PROVIDER_BUSINESS_ERROR", "invalid_request_error", "insufficient_balance_error",
                    "authentication_error", "permission_denied", "unauthorized", "forbidden",
                    "unrecognized_model", "model_not_found", "unknown_model", "MODEL_ERROR",
                    "model_error", "rate_limit_error", "context_length_exceeded", "ETIMEDOUT", "ECONNRESET"}
SAFE_ERROR_TYPES = {"provider", "provider_error", "model_error", "model_timeout", "model_rate_limited",
                    "model_context_exceeded", "permission_denied", "tool_error", "turn_cancelled",
                    "internal_error", "unknown", "Error", "ProviderBusinessError", "invalid_request_error",
                    "insufficient_balance_error", "authentication_error"}
SAFE_TURN_PHASES = {"model", "tool", "tools", "execute", "model_request", "model_stream", "tool_execution",
                    "initialization", "preflight", "unknown", "planning", "thinking", "responding",
                    "completed", "failed", "idle", "running"}
SAFE_SCHEMA_SEGMENTS = {"sessionId", "model", "providerId", "modelId", "options", "reasoningLevel", "mode",
                        "thoughtLevel", "workspace", "workspaceKey", "workspacePath", "runtime", "settings",
                        "stateRevision", "current", "toolAllowlist", "toolDenylist", "mcpServers", "deliveryKind",
                        "includeSnapshot", "afterSeq", "content", "inputId", "queryId", "titleGenerationEnabled",
                        "persistence", "expectedRevision", "nativeSearchEnhancementsEnabled", "toolSearchMode",
                        "memoryEnabled", "askUserQuestionAutoResolutionEnabled", "modelContextBudgetStrategy"}
SAFE_SCHEMA_CODES = {"invalid_type", "unrecognized_keys", "invalid_value", "too_small", "too_big", "custom",
                     "invalid_union", "invalid_format", "invalid_literal", "invalid_enum_value"}


class ProtocolError(RuntimeError):
    """The peer or transport did not satisfy the bounded probe contract."""

    def __init__(self, message: str, *, diagnostic: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


def digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _integer(value: Any) -> TypeGuard[int]:
    return type(value) is int and value >= 0


def _text(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= 256


def _seconds(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeouts must be positive finite seconds")
    return value


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("probe deadline")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def failure_diagnostic(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Classify this turn only; unknown code/type/phase text is hashed, not logged."""
    error = payload.get("error")
    if not isinstance(error, dict):
        error = {}
    fragments = [error.get(k) for k in ("code", "type", "message", "detail", "underlyingErrorMessage")]
    text = " ".join(value[:4096].lower() for value in fragments if isinstance(value, str))
    status = error.get("code")
    explicit_http = re.search(r"\b(?:http(?: status)?|status(?:_code| code)?|responsestatus)\s*[:=]?\s*(\d{3})\b",
                              text)
    if explicit_http:
        status = explicit_http.group(1)
    if status == "402" or any(s in text for s in ("insufficient balance", "insufficient_balance")):
        category = "INSUFFICIENT_BALANCE"
    elif status in ("401", "403") or any(s in text for s in ("authentication_error", "permission_denied")):
        category = "AUTHENTICATION_OR_PERMISSION"
    elif any(s in text for s in ("unrecognized_model", "model_not_found", "unknown_model",
                                  "unknown model", "model not found")):
        category = "UNKNOWN_MODEL"
    elif status == "429" or "rate_limit" in text or "rate limit" in text:
        category = "RATE_LIMITED"
    elif status in ("408", "504") or "timed out" in text or "timeout" in text:
        category = "TIMEOUT"
    else:
        category = "UNCLASSIFIED"

    def known(value: Any, allowed: set[str]) -> str | None:
        return value if isinstance(value, str) and value in allowed else None

    return {"category": category, "scope": "this_turn_only",
            "code": known(error.get("code"), SAFE_ERROR_CODES),
            "type": known(error.get("type"), SAFE_ERROR_TYPES),
            "phase": known(payload.get("turnPhase"), SAFE_TURN_PHASES),
            "detail_sha256": digest({"error": error, "turnPhase": payload.get("turnPhase")})}


def rpc_error_diagnostic(error: Any) -> dict[str, Any]:
    """Summarize bounded RPC message/data.details without exporting their contents."""
    root = error if isinstance(error, dict) else {}
    fragments: list[str] = []
    issues: list[dict[str, Any]] = []
    visited = 0

    def visit(value: Any, depth: int = 0) -> None:
        nonlocal visited
        if visited >= 64 or depth > 6:
            return
        visited += 1
        if isinstance(value, str):
            fragments.append(value[:4096].lower())
            if len(value) <= 16384 and value.lstrip().startswith(("{", "[")):
                try:
                    visit(json.loads(value, object_pairs_hook=_unique_object), depth + 1)
                except (ValueError, ProtocolError, RecursionError):
                    pass
        elif isinstance(value, list):
            for item in value[:32]:
                visit(item, depth + 1)
        elif isinstance(value, dict):
            path, code = value.get("path"), value.get("code")
            if isinstance(path, list) and isinstance(code, str) and code in SAFE_SCHEMA_CODES:
                cleaned = [item if ((isinstance(item, str) and item in SAFE_SCHEMA_SEGMENTS)
                                   or (_integer(item) and item <= 100)) else "[redacted]" for item in path[:16]]
                issues.append({"code": code, "path": cleaned})
            for key in ("message", "detail", "details", "issues", "errors", "cause", "error", "data", "name", "code"):
                if key in value:
                    visit(value[key], depth + 1)

    visit(root)
    text = " ".join(fragments)
    category = failure_diagnostic({"error": root})["category"]
    if issues:
        category = "SCHEMA_VALIDATION"
    elif ("reasoning-level-missing" in text or "no reasoning level selected" in text
          or (("reasoninglevel" in text or "reasoning level" in text)
              and any(word in text for word in ("required", "requires", "missing")))):
        category = "REASONING_LEVEL_REQUIRED"
    elif "reasoning-level-not-supported" in text or ("reasoning effort" in text and "not supported" in text):
        category = "REASONING_LEVEL_UNSUPPORTED"
    elif any(phrase in text for phrase in ("model-not-found", "model not found", "unknown model",
                                           "unrecognized_model", "model_not_found", "中不存在 model")):
        category = "UNKNOWN_MODEL"
    elif "ownership" in text or "session binding mismatch" in text or "workspace binding mismatch" in text:
        category = "OWNERSHIP_MISMATCH"
    elif "mode mismatch" in text:
        category = "MODE_MISMATCH"
    elif "model binding mismatch" in text:
        category = "MODEL_BINDING_MISMATCH"
    elif "invalid session snapshot" in text:
        category = "INVALID_SNAPSHOT"
    return {"category": category, "scope": "this_request_only",
            "rpc_code": root.get("code") if type(root.get("code")) is int else None,
            "schema_issues": issues, "detail_sha256": digest(root)}


def safe_result(result: Any) -> dict[str, Any]:
    """Project only ACK and snapshot identity fields needed to audit the probe."""
    paths = (
        ("accepted",), ("closed",), ("sessionId",), ("stateRevision",),
        ("session", "sessionId"), ("runtime", "stateRevision"), ("settings", "mode", "current"),
        ("settings", "model", "current", "providerId"), ("settings", "model", "current", "modelId"),
        ("settings", "model", "current", "options", "reasoningLevel"),
        ("settings", "thoughtLevel", "current"),
        ("ack", "subscriptionId"), ("ack", "mode"), ("ack", "logEpoch"),
    )
    out: dict[str, Any] = {}
    for path in paths:
        value = result
        for segment in path:
            value = value.get(segment) if isinstance(value, dict) else None
        if path[-1] in {"accepted", "closed"}:
            valid = type(value) is bool
        elif path[-1] == "stateRevision":
            valid = _integer(value)
        elif path == ("settings", "mode", "current"):
            valid = isinstance(value, str) and value in {"plan", "build", "edit", "yolo", "guarded", "auto"}
        elif path == ("ack", "mode"):
            valid = value in {"snapshot", "resume"} if isinstance(value, str) else False
        else:
            valid = _text(value)
        if valid:
            target = out
            for segment in path[:-1]:
                target = target.setdefault(segment, {})
            target[path[-1]] = value
    return out


def safe_frame(frame: Mapping[str, Any], *, direction: str) -> dict[str, Any]:
    """Keep exact non-sensitive protocol fields; hash omitted content, never log auth.

    This is an explicit field projection, not a claim to retain an unredacted log.
    Free text, callbacks, model reasoning, errors, credentials and tool inputs are
    omitted. Only the fixed expected response may be stored as response text.
    """
    out: dict[str, Any] = {"direction": direction, "sha256": digest(frame), "projection": "non-sensitive"}
    if "id" in frame:
        out["id"] = frame["id"]
    method = frame.get("method")
    if isinstance(method, str):
        out["method"] = method
    if method == "v4/conversation/frame":
        wire = frame.get("params", {})
        logical = wire.get("frame", {}) if isinstance(wire, dict) else {}
        body = logical.get("payload", {}) if isinstance(logical, dict) else {}
        snapshot = body.get("snapshot", {}) if isinstance(body, dict) else {}
        config = snapshot.get("config", {}) if isinstance(snapshot, dict) else {}
        if isinstance(config, dict):
            out["plan_config"] = {k: config[k] for k in ("mode", "provider", "model", "thought")
                                  if _text(config.get(k))}
            if type(config.get("planEnabled")) is bool:
                out["plan_config"]["planEnabled"] = config["planEnabled"]
        for key in ("topic", "subscriptionId", "logicalFrameId"):
            if isinstance(wire, dict) and _text(wire.get(key)):
                out[key] = wire[key]
        return out
    if "error" in frame:
        error = frame["error"]
        code = error.get("code") if isinstance(error, dict) else None
        out["error_code"] = code if type(code) is int else None
        out["failure"] = rpc_error_diagnostic(error)
    if "result" in frame:
        out["result"] = safe_result(frame["result"])
    if direction == "out" and method == "session/send" and isinstance(frame.get("params"), dict):
        out["params"] = {k: frame["params"][k] for k in ("sessionId", "inputId", "queryId")
                         if _text(frame["params"].get(k))}
    if method != "session/event" or not isinstance(frame.get("params"), dict):
        return out
    event = frame["params"]
    clean = {k: event[k] for k in ("eventId", "seq", "sessionId", "turnId", "timestamp", "type", "deliveryKind")
             if k in event and isinstance(event[k], (str, int))}
    payload = event.get("payload")
    if isinstance(payload, dict):
        values = {k: payload[k] for k in ("tokenCount", "toolCallCount", "duration")
                  if type(payload.get(k)) in (int, float) and math.isfinite(payload[k])}
        if _text(payload.get("inputId")):
            values["inputId"] = payload["inputId"]
        if isinstance(payload.get("resultType"), str) and payload["resultType"] in RESULT_TYPES:
            values["resultType"] = payload["resultType"]
        if payload.get("response") == MARKER:
            values["response"] = MARKER
        if event.get("type") == "turn.failed":
            values["failure"] = failure_diagnostic(payload)
        clean["payload"] = values
    out["params"] = clean
    return out


class Transport(Protocol):
    def send(self, message: dict[str, Any], timeout: float) -> None: ...
    def receive(self, timeout: float) -> dict[str, Any]: ...


class NdjsonTransport:
    """One child process; bounded lines, queues, writes, reads and shutdown.

    Dedicated pipe readers avoid waiting for process exit before draining stdout
    or stderr. Stderr is drained but intentionally never retained (may contain auth).
    """

    def __init__(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None,
                 max_line_bytes: int = 1024 * 1024) -> None:
        self.max_line_bytes = max_line_bytes
        self._in: queue.Queue[dict[str, Any] | Exception] = queue.Queue(maxsize=256)
        self._out: queue.Queue[tuple[bytes, threading.Event, list[Exception]]] = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._overflow = threading.Event()
        self.process = subprocess.Popen(
            list(argv), cwd=cwd, env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        self._threads = [threading.Thread(target=target, daemon=True)
                         for target in (self._read_stdout, self._drain_stderr, self._write_stdin)]
        for thread in self._threads:
            thread.start()

    def _put(self, value: dict[str, Any] | Exception) -> None:
        try:
            self._in.put_nowait(value)
        except queue.Full:
            self._overflow.set()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            while not self._stop.is_set():
                raw = self.process.stdout.readline(self.max_line_bytes + 1)
                if not raw:
                    self._put(ProtocolError("stdout EOF"))
                    return
                if len(raw) > self.max_line_bytes:
                    raise ProtocolError("oversized NDJSON frame")
                if not raw.strip():
                    continue
                value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
                if not isinstance(value, dict):
                    raise ProtocolError("NDJSON frame must be an object")
                digest(value)  # Reject non-JSON numbers such as NaN/Infinity.
                self._put(value)
        except (ValueError, OSError, ProtocolError, RecursionError):
            self._put(ProtocolError("invalid NDJSON stream"))

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        try:
            while not self._stop.is_set() and self.process.stderr.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def _write_stdin(self) -> None:
        assert self.process.stdin is not None
        while not self._stop.is_set():
            try:
                raw, done, errors = self._out.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                # FileIO can perform partial writes; send the whole frame.
                offset = 0
                while offset < len(raw):
                    size = self.process.stdin.write(raw[offset:])
                    if not size:
                        raise OSError("stdin closed")
                    offset += size
            except (OSError, ValueError) as exc:
                errors.append(exc)
            finally:
                done.set()

    def send(self, message: dict[str, Any], timeout: float) -> None:
        deadline = time.monotonic() + _seconds(timeout)
        raw = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(raw) > self.max_line_bytes:
            raise ProtocolError("outgoing frame exceeds bound")
        done = threading.Event()
        errors: list[Exception] = []
        try:
            self._out.put((raw, done, errors), timeout=remaining(deadline))
        except queue.Full as exc:
            raise TimeoutError("stdin queue deadline") from exc
        if not done.wait(remaining(deadline)):
            raise TimeoutError("stdin write deadline")
        if errors:
            raise ProtocolError("stdin unavailable")

    def receive(self, timeout: float) -> dict[str, Any]:
        if self._overflow.is_set():
            raise ProtocolError("NDJSON queue overflow")
        try:
            item = self._in.get(timeout=_seconds(timeout))
        except queue.Empty as exc:
            raise TimeoutError("stdout deadline") from exc
        if isinstance(item, Exception):
            raise item
        return item

    def close(self, timeout: float = 2.0) -> None:
        _seconds(timeout)
        self._stop.set()
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        for thread in self._threads:
            thread.join(timeout=0.1)


class TurnTracker:
    """Bind one send to one turn; stale/replayed/cross-session events cannot pass."""

    def __init__(self, session_id: str, input_id: str) -> None:
        self.session_id = session_id
        self.input_id = input_id
        self.turn_id: str | None = None
        self.start_seq: int | None = None
        self.terminal: dict[str, Any] | None = None
        self.last_seq = 0
        self.baseline: int | None = None
        self.sequences: dict[int, str] = {}
        self.ids: dict[str, str] = {}
        self.tool_observed = False

    def arm(self, baseline: int) -> None:
        if self.baseline is not None or not _integer(baseline):
            raise ProtocolError("turn tracker is single-use")
        self.baseline = baseline

    def ingest(self, event: Any) -> None:
        if not isinstance(event, dict) or event.get("sessionId") != self.session_id:
            return
        seq, event_id = event.get("seq"), event.get("eventId")
        if not _integer(seq) or not _text(event_id) or not _text(event.get("type")):
            raise ProtocolError("invalid event envelope")
        hashed = digest(event)
        for previous in (self.sequences.get(seq), self.ids.get(event_id)):
            if previous is not None and previous != hashed:
                raise ProtocolError("conflicting event replay")
        if seq in self.sequences:
            return
        if len(self.sequences) >= MAX_FRAMES:
            raise ProtocolError("event bound exceeded")
        self.sequences[seq] = hashed
        self.ids[event_id] = hashed
        self.last_seq = max(seq, self.last_seq)
        if self.baseline is None or seq <= self.baseline:
            return
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            raise ProtocolError("invalid event payload")
        kind, turn = event["type"], event.get("turnId")
        input_id = payload.get("inputId")
        if input_id is not None and input_id != self.input_id:
            return
        if kind == "turn.started":
            if input_id != self.input_id or not _text(turn):
                return
            if self.turn_id is not None:
                raise ProtocolError("input was bound to multiple distinct turn.started events")
            self.turn_id = turn
            self.start_seq = seq
            return
        if kind not in {"turn.completed", "turn.failed", "tool.updated", "model.streaming"}:
            return
        if self.turn_id is not None and turn != self.turn_id:
            return
        if self.start_seq is not None and seq <= self.start_seq:
            raise ProtocolError("event precedes bound turn.started sequence")
        if kind in {"tool.updated", "model.streaming"}:
            if self.turn_id is not None and (kind == "tool.updated" or payload.get("kind") == "tool_call"):
                self.tool_observed = True
                if self.terminal is not None:
                    self.terminal["outcome"] = "FAILED"
            return
        if self.turn_id is None:
            raise ProtocolError("terminal has no matching turn.started")
        if self.terminal is not None:
            raise ProtocolError("conflicting terminal events")
        if kind == "turn.failed":
            self.terminal = {"outcome": "FAILED", "result_type": "turn.failed", "event_id": event_id,
                             "failure": failure_diagnostic(payload)}
            return
        result_type = payload.get("resultType")
        if not isinstance(result_type, str) or result_type not in RESULT_TYPES:
            raise ProtocolError("unknown completion resultType")
        calls = payload.get("toolCallCount")
        if not _integer(calls):
            raise ProtocolError("missing completion tool count")
        if not isinstance(payload.get("response"), str):
            raise ProtocolError("completion response must be text")
        success = (result_type == "success" and payload["response"].strip() == MARKER
                   and calls == 0 and not self.tool_observed)
        self.terminal = {"outcome": "SUCCESS" if success else "FAILED", "result_type": result_type,
                         "event_id": event_id, "tool_call_count": calls,
                         "token_count": payload.get("tokenCount") if _integer(payload.get("tokenCount")) else None}


class ProbeClient:
    def __init__(self, transport: Transport, *, workspace: Path, provider: str, model: str,
                 timeout: float = 45.0, cleanup_timeout: float = 3.0, reasoning_level: str | None = None,
                 allow_workspace_mode_write: bool = False) -> None:
        self.transport = transport
        self.workspace = str(workspace.resolve())
        self.model: dict[str, Any] = {"providerId": provider, "modelId": model}
        if reasoning_level is not None:
            if not _text(reasoning_level) or not reasoning_level.strip():
                raise ValueError("reasoning level must be explicit non-empty text")
            self.model["options"] = {"reasoningLevel": reasoning_level}
        self.timeout = _seconds(timeout)
        self.cleanup_timeout = _seconds(cleanup_timeout)
        self.session_id = "sess_" + str(uuid.uuid4())
        self.input_id = "goalanchor-probe-" + str(uuid.uuid4())
        self.tracker = TurnTracker(self.session_id, self.input_id)
        self.owned = False
        self.used = False
        self.send_count = 0
        self.stop_sent = False
        self.binding_verified = False
        self.plan_enabled_observed: bool | None = None
        self.permission_mode: str | None = None
        self._plan_wires: list[dict[str, Any]] = []
        self.allow_workspace_mode_write = allow_workspace_mode_write
        self.mode_update = {"attempted": False, "confirmed": False,
                            "may_persist_workspace_mode": False, "scope": "isolated_probe_workspace"}
        self.denied_interactions = 0
        self.frames: list[dict[str, Any]] = []
        self._request_number = 0
        self._callback_ids: dict[str, str] = {}

    def _record(self, frame: dict[str, Any], direction: str) -> None:
        if len(self.frames) >= MAX_FRAMES:
            raise ProtocolError("frame count bound exceeded")
        self.frames.append(safe_frame(frame, direction=direction))

    def _send(self, frame: dict[str, Any], deadline: float) -> None:
        self._record(frame, "out")
        self.transport.send(frame, remaining(deadline))

    def _callback(self, frame: dict[str, Any], deadline: float) -> None:
        method, request_id = frame["method"], frame["id"]
        key, hashed = str(request_id), digest(frame)
        if key in self._callback_ids and self._callback_ids[key] != hashed:
            raise ProtocolError("conflicting callback id")
        self._callback_ids[key] = hashed
        result: dict[str, Any]
        if method == "session/requestRuntimePreferences":
            params = frame.get("params") or {}
            if not isinstance(params, dict) or params.get("sessionId") != self.session_id:
                raise ProtocolError("foreign runtime preferences request")
            result = {"nativeSearchEnhancementsEnabled": False, "toolSearchMode": "off",
                      "memoryEnabled": False, "askUserQuestionAutoResolutionEnabled": False,
                      "subagentRuntimeConfigEnabled": False, "modelContextBudgetStrategy": "preflight-v1"}
        elif method == "interaction/requestPermission":
            self.denied_interactions += 1
            result = {"decision": "deny", "reason": "No-tool probe never authorizes tools"}
        elif method == "interaction/requestUserInput":
            self.denied_interactions += 1
            result = {"action": "cancel", "reason": "Probe cannot impersonate user answers"}
        elif method == "interaction/requestProviderRuntimeHeaders":
            self.denied_interactions += 1
            result = {"headersApplied": False, "errorMessage": "Trusted runtime headers unavailable in probe"}
        else:
            self._send({"id": request_id, "error": {"code": -32601, "message": "Unsupported probe callback"}},
                       deadline)
            return
        self._send({"id": request_id, "result": result}, deadline)

    def _pump(self, deadline: float, expected_id: str | None = None) -> tuple[bool, Any]:
        frame = self.transport.receive(remaining(deadline))
        if not isinstance(frame, dict):
            raise ProtocolError("frame must be an object")
        self._record(frame, "in")
        if "method" in frame:
            if "id" in frame:
                self._callback(frame, deadline)
            elif frame["method"] == "session/event":
                self.tracker.ingest(frame.get("params"))
            elif frame["method"] == "v4/conversation/frame" and isinstance(frame.get("params"), dict):
                self._plan_wires.append(frame["params"])
            return False, None
        if expected_id is None or frame.get("id") != expected_id:
            return False, None
        if "error" in frame:
            raise ProtocolError("peer rejected request", diagnostic=rpc_error_diagnostic(frame["error"]))
        if "result" not in frame:
            raise ProtocolError("response lacks result")
        return True, frame["result"]

    def rpc(self, method: str, params: dict[str, Any], deadline: float) -> Any:
        self._request_number += 1
        request_id = f"probe-{self.input_id}-{self._request_number}"
        self._send({"id": request_id, "method": method, "params": params}, deadline)
        while True:
            matched, result = self._pump(deadline, request_id)
            if matched:
                return result

    def _verify_snapshot(self, snapshot: Any, *, require_plan: bool = True) -> int:
        self.binding_verified = False
        try:
            if snapshot["session"]["sessionId"] != self.session_id:
                raise ProtocolError("session binding mismatch")
            workspace = snapshot["session"]["workspace"]
            expected = os.path.normcase(os.path.normpath(self.workspace))
            if (not isinstance(workspace.get("workspacePath"), str)
                    or os.path.normcase(os.path.normpath(workspace["workspacePath"])) != expected
                    or workspace.get("workspaceKey") != self.workspace):
                raise ProtocolError("workspace binding mismatch")
            settings = snapshot["settings"]
            current = settings["model"]["current"]
            self.permission_mode = settings["mode"]["current"]
            effective_plan = self.permission_mode == "plan" or (
                self.permission_mode == "build" and self.plan_enabled_observed is True
            )
            if require_plan and not effective_plan:
                raise ProtocolError("plan-mode mismatch")
            if any(current.get(k) != self.model[k] for k in ("providerId", "modelId")):
                raise ProtocolError("model binding mismatch")
            if "options" in self.model:
                expected_level = self.model["options"]["reasoningLevel"]
                # Installed 0.17.0 snapshots may expose reasoning separately
                # from model.current. Every supplied source must agree.
                sources = []
                options = current.get("options")
                thought = settings.get("thoughtLevel")
                if isinstance(options, dict) and "reasoningLevel" in options:
                    sources.append(options["reasoningLevel"])
                if isinstance(thought, dict) and "current" in thought:
                    sources.append(thought["current"])
                if not sources or any(value != expected_level for value in sources):
                    raise ProtocolError("model binding mismatch")
            revision = snapshot["runtime"]["stateRevision"]
            if not _integer(revision):
                raise ProtocolError("invalid snapshot revision")
            self.binding_verified = effective_plan
            return int(revision)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ProtocolError("invalid session snapshot") from exc

    def _read_plan_state(self, snapshot: Any, deadline: float) -> None:
        """0.17 normalizes plan to build + planEnabled; read the independent flag.

        Fresh empty test conversations fit a complete v3 wire frame. Fragmented
        snapshots are explicitly unsupported, never inferred from an ACK.
        """
        self.plan_enabled_observed = None
        if snapshot["settings"]["mode"]["current"] == "plan":
            return
        topic = "conversation/" + self.session_id
        self._plan_wires.clear()
        response = self.rpc("v4/conversation/subscribe", {
            "topic": topic, "connectionId": self.input_id, "clientMode": DELIVERY,
        }, deadline)
        ack = response.get("ack") if isinstance(response, dict) else None
        if not isinstance(ack, dict) or not _text(ack.get("subscriptionId")) or ack.get("mode") != "snapshot":
            raise ProtocolError("invalid plan subscription acknowledgement")
        try:
            while True:
                while self._plan_wires:
                    wire = self._plan_wires.pop(0)
                    if wire.get("topic") != topic or wire.get("subscriptionId") != ack["subscriptionId"]:
                        continue
                    if wire.get("wireVersion") != 3 or wire.get("kind") != "complete":
                        raise ProtocolError("unsupported plan snapshot wire")
                    logical = wire.get("frame", {})
                    if (logical.get("topic") != topic or logical.get("subscriptionId") != ack["subscriptionId"]):
                        raise ProtocolError("plan snapshot identity mismatch")
                    body = logical.get("payload", {})
                    if body.get("kind") != "snapshot":
                        raise ProtocolError("plan snapshot required")
                    config = body.get("snapshot", {}).get("config", {})
                    if (config.get("provider") != self.model["providerId"]
                            or config.get("model") != self.model["modelId"]
                            or config.get("mode") != snapshot["settings"]["mode"]["current"]
                            or type(config.get("planEnabled")) is not bool):
                        raise ProtocolError("plan snapshot model or mode mismatch")
                    if "options" in self.model and config.get("thought") != self.model["options"]["reasoningLevel"]:
                        raise ProtocolError("model binding mismatch")
                    self.plan_enabled_observed = config["planEnabled"]
                    return
                self._pump(deadline)
        finally:
            self.rpc("v4/conversation/unsubscribe", {"topic": topic, "connectionId": self.input_id,
                     "subscriptionId": ack["subscriptionId"]}, deadline)

    def _events(self, value: Any) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("events"), list):
            raise ProtocolError("invalid replay envelope")
        for item in value["events"]:
            self._record({"method": "session/event", "params": item}, "replay")
            self.tracker.ingest(item)

    def _recover_unknown(self) -> None:
        if not self.owned or not self.send_count or self.stop_sent:
            return
        deadline = time.monotonic() + self.cleanup_timeout
        self.stop_sent = True
        try:
            self.rpc("session/stop", {"sessionId": self.session_id}, deadline)
            replay = self.rpc("session/events", {"sessionId": self.session_id,
                              "afterSeq": self.tracker.baseline or 0, "limit": MAX_FRAMES}, deadline)
            self._events(replay)
            while self.tracker.terminal is None:
                self._pump(deadline)
        except (TimeoutError, ProtocolError, OSError):
            pass  # Receipt remains UNKNOWN, including when late success was observed.

    def _cleanup(self) -> dict[str, Any]:
        if not self.owned:
            return {"attempted": False, "closed": False}
        try:
            result = self.rpc("session/close", {"sessionId": self.session_id},
                              time.monotonic() + self.cleanup_timeout)
            return {"attempted": True, "closed": isinstance(result, dict) and result.get("closed") is True}
        except (TimeoutError, ProtocolError, OSError):
            return {"attempted": True, "closed": False}

    def run(self, *, send: bool = False) -> dict[str, Any]:
        if self.used:
            raise ProtocolError("probe client is single-use; automatic resend is forbidden")
        self.used = True
        start = time.monotonic()
        deadline = start + self.timeout
        outcome = "UNAVAILABLE"
        reason: str | None = None
        failure: dict[str, Any] | None = None
        try:
            self.rpc("runtime/capabilities", {}, deadline)
            created = self.rpc("session/create", {
                "sessionId": self.session_id,
                "workspace": {"workspacePath": self.workspace, "workspaceKey": self.workspace},
                "model": self.model, "mode": "plan", "persistence": "deferred",
                # 0.17.0 create re-selects the model by string and applies the
                # top-level thoughtLevel afterward; options alone are lost.
                **({"thoughtLevel": self.model["options"]["reasoningLevel"]} if "options" in self.model else {}),
                "titleGenerationEnabled": False, "mcpServers": [], "toolAllowlist": [],
                "offPeakToolEnabled": False, "dynamicWorkflowEnabled": False, "dynamicWorkflowMode": "disabled",
            }, deadline)
            if not isinstance(created, dict) or created.get("session", {}).get("sessionId") != self.session_id:
                raise ProtocolError("creation did not confirm ownership")
            self.owned = True
            subscription = self.rpc("session/subscribe", {"sessionId": self.session_id,
                                    "deliveryKind": DELIVERY, "afterSeq": 0, "includeSnapshot": True}, deadline)
            self._events(subscription)
            if subscription.get("sessionId") != self.session_id or not _integer(subscription.get("eventSeq")):
                raise ProtocolError("invalid subscription binding")
            snapshot = self.rpc("session/read", {"sessionId": self.session_id, "deliveryKind": DELIVERY,
                               "contentProfile": "index"}, deadline)
            revision = self._verify_snapshot(snapshot, require_plan=False)
            self._read_plan_state(snapshot, deadline)
            if (snapshot["settings"]["mode"]["current"] != "plan"
                    and self.plan_enabled_observed is not True and self.allow_workspace_mode_write):
                # Desktop 3.16.3 setMode persists project preference unless the
                # session has permission.scope=session. Legacy create cannot set
                # that scope. Permit this only for an explicitly approved sandbox.
                self.mode_update["attempted"] = True
                self.mode_update["may_persist_workspace_mode"] = True
                self.rpc("session/setMode", {"sessionId": self.session_id, "mode": "plan",
                         "expectedRevision": revision}, deadline)
                snapshot = self.rpc("session/read", {"sessionId": self.session_id,
                                    "deliveryKind": DELIVERY, "contentProfile": "index"}, deadline)
                self._read_plan_state(snapshot, deadline)
                revision = self._verify_snapshot(snapshot)
                self.mode_update["confirmed"] = True
            else:
                revision = self._verify_snapshot(snapshot)
            self.binding_verified = True
            outcome = "PREFLIGHT_VERIFIED"
            if send:
                self.tracker.arm(max(subscription["eventSeq"], self.tracker.last_seq))
                self.send_count = 1  # Reserve before write: a lost ACK must never cause retransmission.
                ack = self.rpc("session/send", {"sessionId": self.session_id, "content": FIXED_PROMPT,
                               "inputId": self.input_id, "queryId": self.input_id,
                               "expectedRevision": revision, "toolDenylist": ["Bash", "Write", "Edit"]}, deadline)
                if (not isinstance(ack, dict) or ack.get("accepted") is not True
                        or ack.get("sessionId") != self.session_id or not _integer(ack.get("stateRevision"))):
                    raise ProtocolError("invalid send acknowledgement")
                while self.tracker.terminal is None:
                    self._pump(deadline)
                after = self.rpc("session/read", {"sessionId": self.session_id,
                                 "contentProfile": "index"}, deadline)
                self._read_plan_state(after, deadline)
                self._verify_snapshot(after)
                outcome = self.tracker.terminal["outcome"]
                if self.denied_interactions:
                    outcome = "FAILED"
                    reason = "interaction_required_by_no_tool_probe"
        except TimeoutError:
            outcome = "UNKNOWN" if self.send_count else "UNAVAILABLE"
            reason = "deadline_exceeded"
            self._recover_unknown()
        except ProtocolError as exc:
            outcome = "UNKNOWN" if self.send_count else "UNTRUSTED"
            reason = "protocol_or_transport_failure"
            failure = exc.diagnostic or rpc_error_diagnostic({"message": str(exc)})
            self._recover_unknown()
        except (OSError, ValueError, TypeError, AttributeError):
            outcome = "UNKNOWN" if self.send_count else "UNTRUSTED"
            reason = "protocol_or_transport_failure"
            self._recover_unknown()
        cleanup = self._cleanup()
        # Read/close ACKs can be interleaved with late same-turn tool events.
        # Re-evaluate the no-tool invariant after the last bounded drain, while
        # never upgrading an UNKNOWN timeout based on a late success receipt.
        if outcome == "SUCCESS" and (self.tracker.tool_observed or self.denied_interactions):
            outcome = "FAILED"
            reason = "no_tool_contract_violated"
        return {"schema": "goalanchor.zcode-protocol-probe/v1", "status": "PARTIAL/BOUNDED",
                "outcome": outcome, "reason": reason, "failure": failure,
                "strict_proof": False, "native_strict_loop_verified": False,
                "session_id": self.session_id, "input_id": self.input_id, "turn_id": self.tracker.turn_id,
                "requested_model": self.model, "binding_verified": self.binding_verified,
                "requested_mode": "plan", "mode": "plan" if self.binding_verified else None,
                "permission_mode": self.permission_mode, "plan_enabled_observed": self.plan_enabled_observed,
                "mode_update": self.mode_update, "send_count": self.send_count, "automatic_retry": False,
                "stop_sent": self.stop_sent, "denied_interactions": self.denied_interactions,
                "terminal": self.tracker.terminal, "cleanup": cleanup,
                "elapsed_seconds": round(time.monotonic() - start, 3), "events": self.frames,
                "evidence_boundary": "Native transport observation only; no authorization or review proof issued."}


def write_evidence(path: Path, result: Mapping[str, Any]) -> None:
    """Exclusive files prevent accidental replacement of another run's evidence."""
    events_path = path.with_suffix(".events.jsonl")
    if path.exists() or events_path.exists():
        raise FileExistsError("evidence path already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = {k: v for k, v in result.items() if k != "events"}
    summary["event_projection_path"] = str(events_path.resolve())
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    with events_path.open("x", encoding="utf-8") as handle:
        for event in result.get("events", []):
            handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, type=Path, help="Installed resources/glm/zcode.cjs path")
    parser.add_argument("--node", default="node", help="Node executable (never uses a shell)")
    parser.add_argument("--provider-config", type=Path, help="Pass config path to ZCode; never read its contents")
    parser.add_argument("--workspace", required=True, type=Path, help="Explicit isolated probe workspace")
    parser.add_argument("--provider", required=True, help="Explicit providerId, not a credential")
    parser.add_argument("--model", required=True, help="Explicit modelId")
    parser.add_argument("--reasoning-level", help="Explicit supported reasoning level; no inferred default")
    parser.add_argument("--allow-workspace-mode-write", action="store_true",
                        help="Allow setMode(plan) in this isolated workspace; may persist its project preference")
    parser.add_argument("--output", required=True, type=Path, help="New audit JSON path; existing files are refused")
    parser.add_argument("--timeout", default=45.0, type=float, help="Total operation deadline in seconds")
    parser.add_argument("--cleanup-timeout", default=3.0, type=float, help="Bound for stop/replay and close")
    parser.add_argument("--send", action="store_true", help="Opt in to one fixed no-tool model prompt, no retry")
    args = parser.parse_args(argv)
    if not args.cli.is_file() or not args.workspace.is_dir():
        parser.error("CLI file and existing isolated workspace are required")
    if args.provider_config is not None and not args.provider_config.is_file():
        parser.error("provider config path does not exist")
    if args.output.exists() or args.output.with_suffix(".events.jsonl").exists():
        parser.error("output already exists; choose a new evidence path")
    try:
        _seconds(args.timeout)
        _seconds(args.cleanup_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    env = dict(os.environ)
    if args.provider_config is not None:
        env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = str(args.provider_config.resolve())
    transport = NdjsonTransport([args.node, str(args.cli.resolve()), "app-server"],
                               cwd=args.workspace.resolve(), env=env)
    try:
        probe = ProbeClient(transport, workspace=args.workspace, provider=args.provider, model=args.model,
                            timeout=args.timeout, cleanup_timeout=args.cleanup_timeout,
                            reasoning_level=args.reasoning_level,
                            allow_workspace_mode_write=args.allow_workspace_mode_write)
        result = probe.run(send=args.send)
    finally:
        transport.close(args.cleanup_timeout)
    result["cli_path"] = str(args.cli.resolve())
    result["cli_sha256"] = hashlib.sha256(args.cli.read_bytes()).hexdigest()
    result["provider_config_passed"] = args.provider_config is not None
    write_evidence(args.output, result)
    print(json.dumps({k: result[k] for k in ("outcome", "status", "send_count", "strict_proof", "cleanup", "failure")}))
    return 0 if result["outcome"] in {"SUCCESS", "PREFLIGHT_VERIFIED"} and result["cleanup"]["closed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
