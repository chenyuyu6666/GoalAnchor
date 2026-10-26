"""Offline transport regressions; none of these tests contact ZCode or a model."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.zcode_protocol_probe import (
    FIXED_PROMPT,
    MARKER,
    NdjsonTransport,
    ProbeClient,
    ProtocolError,
    TurnTracker,
    rpc_error_diagnostic,
    safe_frame,
    write_evidence,
)

# Fixture sentinel proving the probe never echoes provider runtime credentials.
NON_SECRET_RUNTIME_KEY_SENTINEL = "probe-fixture-sentinel-not-a-secret"


def event(seq, event_type, *, session="session", input_id="input", turn="turn", **payload):
    return {"eventId": f"e{seq}", "seq": seq, "sessionId": session,
            "turnId": turn, "timestamp": 1, "type": event_type,
            "payload": {"inputId": input_id, **payload}}


def started(**kwargs):
    return event(1, "turn.started", input=FIXED_PROMPT, turnNumber=1, **kwargs)


def completed(**kwargs):
    return event(2, "turn.completed", response=MARKER, tokenCount=3,
                 toolCallCount=0, duration=1, **kwargs)


class FakeTransport:
    def __init__(self, *, terminal="success", read_model=None, callbacks=False, replay=False,
                 wrong_ack=False, close_error=False, initial_mode="plan", ignores_mode_update=False,
                 plan_enabled=False, plan_wire_change=None):
        self.messages = queue.Queue()
        self.sent = []
        self.terminal = terminal
        self.read_model = read_model
        self.callbacks = callbacks
        self.replay = replay
        self.wrong_ack = wrong_ack
        self.close_error = close_error
        self.mode = initial_mode
        self.ignores_mode_update = ignores_mode_update
        self.plan_enabled = plan_enabled
        self.plan_wire_change = plan_wire_change
        self.pending_events = []

    def send(self, message, timeout):
        self.sent.append(message)
        method = message.get("method")
        params = message.get("params", {})
        if method is None:
            return
        result = {}
        if method == "runtime/capabilities":
            result = {"hostCapabilities": {}}
        elif method == "session/create":
            self.session_id = params["sessionId"]
            self.model = params["model"]
            self.workspace = params["workspace"]
            result = self.snapshot()
        elif method == "session/read":
            result = self.snapshot()
        elif method == "session/subscribe":
            result = {"sessionId": self.session_id, "eventSeq": 0, "events": []}
        elif method == "session/setMode":
            if not self.ignores_mode_update:
                self.mode = params["mode"]
            result = self.snapshot()
        elif method == "v4/conversation/subscribe":
            result = {"ack": {"subscriptionId": "sub-plan", "mode": "snapshot", "logEpoch": "epoch"}}
            wire = {"wireVersion": 3, "kind": "complete", "topic": params["topic"],
                    "subscriptionId": "sub-plan", "frame": {"topic": params["topic"],
                    "subscriptionId": "sub-plan", "payload": {"kind": "snapshot", "snapshot": {
                    "config": {"mode": self.mode, "planEnabled": self.plan_enabled,
                    "provider": self.model["providerId"], "model": self.model["modelId"],
                    "thought": self.model.get("options", {}).get("reasoningLevel", "")}}}}}
            if self.plan_wire_change:
                self.plan_wire_change(wire)
            self.messages.put({"method": "v4/conversation/frame", "params": wire})
        elif method == "session/send":
            result = {"accepted": True, "sessionId": self.session_id, "stateRevision": 1}
            if self.callbacks:
                for suffix in ("Permission", "UserInput", "ProviderRuntimeHeaders"):
                    self.messages.put({"id": suffix, "method": f"interaction/request{suffix}",
                                       "params": {"sessionId": self.session_id,
                                                  "apiKey": NON_SECRET_RUNTIME_KEY_SENTINEL}})
                self.messages.put({"id": "prefs", "method": "session/requestRuntimePreferences",
                                   "params": {"sessionId": self.session_id, "scope": "user-execution"}})
            binding = {"session": self.session_id, "input_id": params["inputId"]}
            self.pending_events = [started(**binding)]
            if self.terminal is not None:
                self.pending_events.append(completed(resultType=self.terminal, **binding))
            if not self.replay:
                for item in self.pending_events:
                    self.messages.put({"method": "session/event", "params": item})
        elif method == "session/events":
            result = {"events": self.pending_events}
        elif method == "session/close":
            if self.close_error:
                self.messages.put({"id": message["id"], "error": {"code": -32004, "message": "gone"}})
                return
            result = {"closed": True}
        response_id = "unrelated" if self.wrong_ack and method == "session/send" else message["id"]
        self.messages.put({"id": response_id, "result": result})

    def snapshot(self):
        return {"session": {"sessionId": self.session_id, "workspace": self.workspace},
                "settings": {"mode": {"current": self.mode},
                             "model": {"current": self.read_model or self.model}},
                "runtime": {"stateRevision": 0}}

    def receive(self, timeout):
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc


def client(transport):
    return ProbeClient(transport, workspace=Path.cwd(), provider="p", model="m",
                       timeout=0.06, cleanup_timeout=0.02)


def test_completed_event_itself_contains_type_and_payload():
    probe = client(FakeTransport())
    result = probe.run(send=True)
    assert result["outcome"] == "SUCCESS"
    assert result["strict_proof"] is False
    assert result["send_count"] == 1
    assert result["binding_verified"] is True
    assert result["terminal"]["result_type"] == "success"


def test_independent_plan_flag_allows_build_only_with_verified_v4_snapshot():
    transport = FakeTransport(initial_mode="build", plan_enabled=True)
    result = client(transport).run(send=True)
    assert result["outcome"] == "SUCCESS"
    assert result["permission_mode"] == "build"
    assert result["plan_enabled_observed"] is True
    assert not any(m.get("method") == "session/setMode" for m in transport.sent)
    assert sum(m.get("method") == "v4/conversation/subscribe" for m in transport.sent) == 2


@pytest.mark.parametrize("change", [
    lambda w: w.update(kind="fragment"),
    lambda w: w.update(wireVersion=99),
    lambda w: w["frame"].update(topic="conversation/foreign"),
    lambda w: w["frame"]["payload"]["snapshot"]["config"].update(planEnabled="true"),
    lambda w: w["frame"]["payload"]["snapshot"]["config"].update(provider="foreign"),
])
def test_unverified_v4_plan_never_sends(change):
    transport = FakeTransport(initial_mode="build", plan_enabled=True, plan_wire_change=change)
    result = client(transport).run(send=True)
    assert result["outcome"] != "SUCCESS"
    assert result["send_count"] == 0


@pytest.mark.parametrize("terminal", ["cancelled", "error_max_turns", "error_max_budget",
                                      "error_during_execution", "error_max_tool_calls"])
def test_completed_name_does_not_make_non_success_result_success(terminal):
    result = client(FakeTransport(terminal=terminal)).run(send=True)
    assert result["outcome"] == "FAILED"
    assert result["terminal"]["result_type"] == terminal


def test_callbacks_are_explicitly_denied_without_synthetic_auth_or_answers():
    transport = FakeTransport(callbacks=True)
    result = client(transport).run(send=True)
    replies = {m["id"]: m["result"] for m in transport.sent if "result" in m}
    assert replies["Permission"]["decision"] == "deny"
    assert replies["UserInput"]["action"] == "cancel"
    assert replies["ProviderRuntimeHeaders"]["headersApplied"] is False
    assert "requestAuth" not in replies["ProviderRuntimeHeaders"]
    assert replies["prefs"]["askUserQuestionAutoResolutionEnabled"] is False
    assert result["outcome"] == "FAILED"
    assert NON_SECRET_RUNTIME_KEY_SENTINEL not in json.dumps(probe_frames := result["events"])
    assert probe_frames


def test_wrong_ack_id_cannot_satisfy_send_even_if_terminal_arrives():
    result = client(FakeTransport(wrong_ack=True)).run(send=True)
    assert result["outcome"] == "UNKNOWN"
    assert result["send_count"] == 1
    assert result["stop_sent"] is True


def test_timeout_stop_is_once_and_replay_success_remains_unknown():
    transport = FakeTransport(replay=True)
    probe = client(transport)
    result = probe.run(send=True)
    assert result["outcome"] == "UNKNOWN"
    assert result["terminal"]["result_type"] == "success"
    assert result["stop_sent"] is True
    assert [m.get("method") for m in transport.sent].count("session/send") == 1
    assert [m.get("method") for m in transport.sent].count("session/stop") == 1
    with pytest.raises(ProtocolError, match="single-use"):
        probe.run(send=True)


def test_wrong_read_model_prevents_send_and_cleanup_only_closes_own_session():
    transport = FakeTransport(read_model={"providerId": "other", "modelId": "m"})
    probe = client(transport)
    result = probe.run(send=True)
    assert result["outcome"] == "UNTRUSTED"
    assert result["send_count"] == 0
    closes = [m for m in transport.sent if m.get("method") == "session/close"]
    assert len(closes) == 1
    assert closes[0]["params"]["sessionId"] == probe.session_id
    assert not any(m.get("method") == "session/resume" for m in transport.sent)


def test_preflight_is_default_and_disables_tools_titles_memory_and_workflows():
    transport = FakeTransport()
    result = client(transport).run()
    assert result["outcome"] == "PREFLIGHT_VERIFIED"
    create = next(m for m in transport.sent if m.get("method") == "session/create")["params"]
    assert create["toolAllowlist"] == []
    assert create["titleGenerationEnabled"] is False
    assert create["dynamicWorkflowMode"] == "disabled"
    assert create["mode"] == "plan"
    assert result["send_count"] == 0


def test_explicit_reasoning_level_is_sent_and_verified_in_snapshot():
    transport = FakeTransport()
    probe = ProbeClient(transport, workspace=Path.cwd(), provider="p", model="m", reasoning_level="medium",
                        timeout=0.06, cleanup_timeout=0.02)
    result = probe.run()
    assert result["outcome"] == "PREFLIGHT_VERIFIED"
    create = next(m for m in transport.sent if m.get("method") == "session/create")["params"]
    assert create["model"] == {"providerId": "p", "modelId": "m", "options": {"reasoningLevel": "medium"}}
    assert create["thoughtLevel"] == "medium"


def test_wrong_readback_reasoning_level_prevents_send():
    transport = FakeTransport(read_model={"providerId": "p", "modelId": "m", "options": {"reasoningLevel": "low"}})
    probe = ProbeClient(transport, workspace=Path.cwd(), provider="p", model="m", reasoning_level="medium",
                        timeout=0.06, cleanup_timeout=0.02)
    result = probe.run(send=True)
    assert result["send_count"] == 0
    assert result["failure"]["category"] == "MODEL_BINDING_MISMATCH"


def test_snapshot_thought_level_current_is_a_verified_reasoning_source():
    class SplitThoughtLevel(FakeTransport):
        def snapshot(self):
            snapshot = super().snapshot()
            snapshot["settings"]["model"]["current"] = {"providerId": "p", "modelId": "m"}
            snapshot["settings"]["thoughtLevel"] = {"current": "low", "enabled": True}
            return snapshot

    transport = SplitThoughtLevel()
    probe = ProbeClient(transport, workspace=Path.cwd(), provider="p", model="m", reasoning_level="low",
                        timeout=0.06, cleanup_timeout=0.02)
    result = probe.run(send=True)
    assert result["outcome"] == "SUCCESS"
    assert result["binding_verified"] is True


@pytest.mark.parametrize(("model_level", "thought_level"), [("low", "high"), ("high", "low"),
                                                             (None, None), (None, "high")])
def test_reasoning_sources_must_match_each_other_and_requested_level(model_level, thought_level):
    class DivergentThoughtLevel(FakeTransport):
        def snapshot(self):
            snapshot = super().snapshot()
            current = {"providerId": "p", "modelId": "m"}
            if model_level is not None:
                current["options"] = {"reasoningLevel": model_level}
            snapshot["settings"]["model"]["current"] = current
            if thought_level is not None:
                snapshot["settings"]["thoughtLevel"] = {"current": thought_level}
            return snapshot

    probe = ProbeClient(DivergentThoughtLevel(), workspace=Path.cwd(), provider="p", model="m",
                        reasoning_level="low", timeout=0.06, cleanup_timeout=0.02)
    result = probe.run(send=True)
    assert result["send_count"] == 0
    assert result["binding_verified"] is False
    assert result["failure"]["category"] == "MODEL_BINDING_MISMATCH"


def test_explicit_sandbox_mode_update_then_readback_enables_plan_probe():
    transport = FakeTransport(initial_mode="build")
    probe = ProbeClient(transport, workspace=Path.cwd(), provider="p", model="m",
                        timeout=0.06, cleanup_timeout=0.02, allow_workspace_mode_write=True)
    result = probe.run(send=True)
    assert result["outcome"] == "SUCCESS"
    updates = [m for m in transport.sent if m.get("method") == "session/setMode"]
    assert len(updates) == 1
    assert updates[0]["params"]["sessionId"] == probe.session_id
    assert updates[0]["params"]["mode"] == "plan"
    assert result["mode_update"] == {"attempted": True, "confirmed": True,
                                    "may_persist_workspace_mode": True, "scope": "isolated_probe_workspace"}


def test_mode_setter_ack_without_plan_readback_does_not_allow_send():
    transport = FakeTransport(initial_mode="build", ignores_mode_update=True)
    probe = ProbeClient(transport, workspace=Path.cwd(), provider="p", model="m",
                        timeout=0.06, cleanup_timeout=0.02, allow_workspace_mode_write=True)
    result = probe.run(send=True)
    assert result["send_count"] == 0
    assert result["failure"]["category"] == "MODE_MISMATCH"
    assert result["mode_update"]["attempted"] is True
    assert result["mode_update"]["confirmed"] is False
    assert result["cleanup"]["closed"] is True


def test_mode_is_not_persisted_without_explicit_workspace_write_option():
    transport = FakeTransport(initial_mode="build")
    result = client(transport).run(send=True)
    assert result["send_count"] == 0
    assert not any(m.get("method") == "session/setMode" for m in transport.sent)


def test_foreign_workspace_in_readback_blocks_even_explicit_mode_update():
    class WrongWorkspace(FakeTransport):
        def snapshot(self):
            snapshot = super().snapshot()
            snapshot["session"]["workspace"] = {"workspacePath": "C:/another-project", "workspaceKey": "foreign"}
            return snapshot

    transport = WrongWorkspace(initial_mode="build")
    probe = ProbeClient(transport, workspace=Path.cwd(), provider="p", model="m",
                        timeout=0.06, cleanup_timeout=0.02, allow_workspace_mode_write=True)
    result = probe.run(send=True)
    assert result["send_count"] == 0
    assert result["failure"]["category"] == "OWNERSHIP_MISMATCH"
    assert not any(m.get("method") == "session/setMode" for m in transport.sent)


def test_cleanup_failure_is_reported_without_claiming_successful_close():
    result = client(FakeTransport(close_error=True)).run()
    assert result["cleanup"]["closed"] is False


def test_wrong_mode_prevents_send_and_no_existing_session_is_closed():
    class WrongMode(FakeTransport):
        def snapshot(self):
            snapshot = super().snapshot()
            snapshot["settings"]["mode"]["current"] = "yolo"
            return snapshot

    transport = WrongMode()
    result = client(transport).run(send=True)
    assert result["outcome"] == "UNTRUSTED"
    assert result["send_count"] == 0


def test_foreign_creation_ack_never_establishes_cleanup_ownership():
    class ForeignSession(FakeTransport):
        def snapshot(self):
            snapshot = super().snapshot()
            snapshot["session"]["sessionId"] = "existing-user-session"
            return snapshot

    transport = ForeignSession()
    result = client(transport).run(send=True)
    assert result["cleanup"] == {"attempted": False, "closed": False}
    assert not any(m.get("method") in {"session/send", "session/stop", "session/close"} for m in transport.sent)


def test_current_model_change_after_send_invalidates_binding():
    class ModelChanges(FakeTransport):
        def snapshot(self):
            snapshot = super().snapshot()
            if any(m.get("method") == "session/send" for m in self.sent):
                snapshot["settings"]["model"]["current"] = {"providerId": "other", "modelId": "other"}
            return snapshot

    result = client(ModelChanges()).run(send=True)
    assert result["outcome"] == "UNKNOWN"
    assert result["binding_verified"] is False


def test_tool_event_prevents_success_even_with_zero_self_reported_tool_count():
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest(started())
    tracker.ingest(event(2, "tool.updated", kind="started"))
    tracker.ingest({**completed(resultType="success"), "seq": 3, "eventId": "e3"})
    assert tracker.terminal["outcome"] == "FAILED"


@pytest.mark.parametrize("injection_method", ["session/read", "session/close"])
def test_tool_event_after_terminal_before_post_read_ack_cannot_pass(injection_method):
    class ToolAfterTerminal(FakeTransport):
        def send(self, message, timeout):
            if message.get("method") == injection_method and self.pending_events:
                tool = event(3, "tool.updated", session=self.session_id,
                             input_id=self.pending_events[0]["payload"]["inputId"], kind="started")
                self.messages.put({"method": "session/event", "params": tool})
            super().send(message, timeout)

    result = client(ToolAfterTerminal()).run(send=True)
    assert result["outcome"] == "FAILED"
    assert result["terminal"]["outcome"] == "FAILED"


@pytest.mark.parametrize("kind", ["turn.completed", "tool.updated"])
def test_event_before_bound_turn_start_sequence_is_rejected(kind):
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest({**started(), "seq": 10, "eventId": "start10"})
    earlier = event(9, kind, resultType="success", response=MARKER, toolCallCount=0, kind="started")
    with pytest.raises(ProtocolError, match="precedes"):
        tracker.ingest(earlier)


def test_distinct_start_event_cannot_move_an_existing_turn_sequence_backward():
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest({**started(), "seq": 10, "eventId": "start10"})
    with pytest.raises(ProtocolError):
        tracker.ingest({**started(), "seq": 5, "eventId": "start5"})
    assert tracker.start_seq == 10


@pytest.mark.parametrize(("error", "category"), [
    ({"type": "provider_error", "code": "402", "message": "Insufficient Balance SECRET"},
     "INSUFFICIENT_BALANCE"),
    ({"type": "model_error", "code": "PROVIDER_BUSINESS_ERROR", "message": "status 401 SECRET"},
     "AUTHENTICATION_OR_PERMISSION"),
    ({"type": "provider_error", "code": "403", "message": "SECRET"}, "AUTHENTICATION_OR_PERMISSION"),
    ({"type": "model_error", "code": "model_not_found", "message": "SECRET"}, "UNKNOWN_MODEL"),
    ({"type": "provider_error", "code": "unrecognized_model", "message": "SECRET"}, "UNKNOWN_MODEL"),
    ({"type": "provider_error", "code": "anything-SECRET", "message": "free text SECRET"}, "UNCLASSIFIED"),
])
def test_failed_turn_keeps_fixed_diagnostics_without_error_body(error, category):
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest(started())
    tracker.ingest(event(2, "turn.failed", error=error, turnPhase="model"))
    failure = tracker.terminal["failure"]
    assert failure["category"] == category
    assert failure["type"] == error["type"]
    assert failure["phase"] == "model"
    assert failure["code"] == (None if "SECRET" in error["code"] else error["code"])
    assert "SECRET" not in json.dumps(tracker.terminal)


def test_unrecognized_failure_fields_are_hashed_not_exported_as_free_text():
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest(started())
    tracker.ingest(event(2, "turn.failed", error={"type": "SECRET", "code": "SECRET"}, turnPhase="SECRET"))
    failure = tracker.terminal["failure"]
    assert failure["type"] is None and failure["phase"] is None and failure["code"] is None
    assert "SECRET" not in json.dumps(tracker.terminal)


@pytest.mark.parametrize(("message", "category"), [
    ("Reasoning level is required for SECRET", "REASONING_LEVEL_REQUIRED"),
    ("reasoning-level-missing SECRET", "REASONING_LEVEL_REQUIRED"),
    ("Model not found: SECRET", "UNKNOWN_MODEL"),
    ("Reasoning effort SECRET is not supported by SECRET", "REASONING_LEVEL_UNSUPPORTED"),
    ("creation did not confirm ownership", "OWNERSHIP_MISMATCH"),
    ("plan-mode mismatch", "MODE_MISMATCH"),
])
def test_rpc_errors_keep_a_fixed_category_without_free_text(message, category):
    diagnostic = rpc_error_diagnostic({"code": -32603, "message": message,
                                      "data": {"details": message, "headers": {"Authorization": "SECRET"}}})
    assert diagnostic["category"] == category
    assert diagnostic["rpc_code"] == -32603
    assert "SECRET" not in json.dumps(diagnostic)


def test_rpc_schema_paths_are_projected_and_unknown_segments_redacted():
    error = {"code": -32602, "message": "Invalid params SECRET", "data": {"details": {"issues": [
        {"code": "invalid_type", "path": ["model", "options", "reasoningLevel"], "message": "SECRET"},
        {"code": "custom", "path": ["workspace", "SECRET", 0], "message": "SECRET"},
    ]}}}
    diagnostic = rpc_error_diagnostic(error)
    assert diagnostic["category"] == "SCHEMA_VALIDATION"
    assert diagnostic["schema_issues"] == [
        {"code": "invalid_type", "path": ["model", "options", "reasoningLevel"]},
        {"code": "custom", "path": ["workspace", "[redacted]", 0]},
    ]
    assert "SECRET" not in json.dumps(safe_frame({"id": "request", "error": error}, direction="in"))


def test_rpc_error_embedded_json_message_yields_safe_schema_paths():
    message = json.dumps([{"code": "unrecognized_keys", "path": ["model"], "keys": ["SECRET"]}])
    diagnostic = rpc_error_diagnostic({"code": -32602, "message": message})
    assert diagnostic["category"] == "SCHEMA_VALIDATION"
    assert diagnostic["schema_issues"] == [{"code": "unrecognized_keys", "path": ["model"]}]
    assert "SECRET" not in json.dumps(diagnostic)


def test_create_rpc_failure_has_diagnostic_and_never_sends_or_closes_foreign_session():
    class CreateFails(FakeTransport):
        def send(self, message, timeout):
            if message.get("method") == "session/create":
                self.sent.append(message)
                self.messages.put({"id": message["id"], "error": {
                    "code": -32603, "message": "Reasoning level is required for SECRET"}})
                return
            super().send(message, timeout)

    result = client(CreateFails()).run(send=True)
    assert result["send_count"] == 0
    assert result["failure"]["category"] == "REASONING_LEVEL_REQUIRED"
    assert result["cleanup"]["attempted"] is False
    assert "SECRET" not in json.dumps(result)


def test_sequence_reuse_with_a_different_event_id_is_conflict():
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest(started())
    with pytest.raises(ProtocolError, match="conflicting"):
        tracker.ingest({**started(), "eventId": "different"})


def test_tracker_accepts_replay_idempotently_and_rejects_conflicting_event_ids():
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest(started())
    tracker.ingest(started())
    tracker.ingest(completed(resultType="success"))
    assert tracker.terminal["result_type"] == "success"
    changed = completed(resultType="cancelled")
    with pytest.raises(ProtocolError, match="conflicting"):
        tracker.ingest(changed)


@pytest.mark.parametrize("change", [{"sessionId": "other"}, {"turnId": "other"},
                                    {"payload": {"inputId": "other", "resultType": "success"}}])
def test_tracker_cannot_reuse_cross_session_input_or_turn_results(change):
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest(started())
    tracker.ingest({**completed(resultType="success"), **change})
    assert tracker.terminal is None


def test_tracker_does_not_accept_unbound_or_old_terminal():
    tracker = TurnTracker("session", "input")
    tracker.arm(2)
    tracker.ingest(completed(resultType="success"))
    assert tracker.terminal is None
    with pytest.raises(ProtocolError, match="started"):
        tracker.ingest({**completed(resultType="success"), "seq": 3, "eventId": "e3"})


def test_tracker_turn_failed_is_failure_and_tool_execution_blocks_success():
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest(started())
    tracker.ingest(event(2, "turn.failed", error={"type": "provider", "message": "secret"}, turnPhase="model"))
    assert tracker.terminal["outcome"] == "FAILED"
    assert "secret" not in json.dumps(tracker.terminal)


def test_non_sensitive_journal_never_persists_headers_prompts_or_error_text():
    frame = {"id": "server-1", "method": "interaction/requestProviderRuntimeHeaders",
             "params": {"headers": {"Authorization": "SECRET"}, "apiKey": "SECRET"}}
    encoded = json.dumps(safe_frame(frame, direction="in"))
    assert "SECRET" not in encoded
    assert "sha256" in encoded
    bad = event(3, "turn.failed", error={"message": "SECRET", "code": "SECRET"})
    assert "SECRET" not in json.dumps(safe_frame({"method": "session/event", "params": bad}, direction="in"))


def test_safe_ack_projection_preserves_acceptance_and_close_fields_only():
    frame = {"id": "client-1", "result": {"accepted": True, "closed": False, "sessionId": "session-1",
             "stateRevision": 4, "headers": {"Authorization": "SECRET"}, "account": "SECRET"}}
    projected = safe_frame(frame, direction="in")
    assert projected["result"] == {"accepted": True, "closed": False, "sessionId": "session-1", "stateRevision": 4}
    assert "SECRET" not in json.dumps(projected)


def test_safe_snapshot_projection_keeps_model_mode_revision_without_extra_options():
    frame = {"id": "client-1", "result": {
        "session": {"sessionId": "session-1", "account": "SECRET", "title": "SECRET"},
        "settings": {"model": {"current": {"providerId": "provider-1", "modelId": "model-1",
                     "options": {"reasoningLevel": "low", "apiKey": "SECRET"}, "account": "SECRET"},
                               "available": [{"apiKey": "SECRET"}]},
                     "mode": {"current": "plan", "unknown": "SECRET"},
                     "thoughtLevel": {"current": "low", "available": ["SECRET"]}, "headers": "SECRET"},
        "runtime": {"stateRevision": 5, "secret": "SECRET"}, "messages": ["SECRET"],
    }}
    projected = safe_frame(frame, direction="in")
    assert projected["result"] == {
        "session": {"sessionId": "session-1"},
        "settings": {"model": {"current": {"providerId": "provider-1", "modelId": "model-1",
                                           "options": {"reasoningLevel": "low"}}}, "mode": {"current": "plan"},
                     "thoughtLevel": {"current": "low"}},
        "runtime": {"stateRevision": 5},
    }
    assert "SECRET" not in json.dumps(projected)


def test_safe_result_drops_wrong_primitive_types_and_unknown_modes():
    frame = {"id": "client-1", "result": {"accepted": "SECRET", "closed": 1, "sessionId": {"key": "SECRET"},
             "stateRevision": True, "settings": {"mode": {"current": "SECRET"}}}}
    projected = safe_frame(frame, direction="in")
    assert projected["result"] == {}
    assert "SECRET" not in json.dumps(projected)


@pytest.mark.parametrize("code", [{"detail": "AUDIT_CANARY"}, "AUDIT_CANARY", ["AUDIT_CANARY"]])
def test_rpc_error_code_cannot_bypass_safe_projection(code):
    projected = safe_frame({"id": "request", "error": {"code": code}}, direction="in")
    assert "AUDIT_CANARY" not in json.dumps(projected)
    assert projected["error_code"] is None


def test_projection_retains_send_start_and_subscription_identity_without_payload_text():
    send = {"id": "send-1", "method": "session/send", "params": {
        "sessionId": "session", "inputId": "input", "queryId": "input", "content": "SECRET"}}
    projected = safe_frame(send, direction="out")
    assert projected["params"] == {"sessionId": "session", "inputId": "input", "queryId": "input"}
    assert "SECRET" not in json.dumps(projected)
    start = safe_frame({"method": "session/event", "params": started()}, direction="in")
    assert start["params"]["payload"]["inputId"] == "input"
    ack = safe_frame({"id": "v4-1", "result": {"ack": {
        "subscriptionId": "sub-1", "mode": "snapshot", "logEpoch": "epoch", "extra": "SECRET"}}}, direction="in")
    assert ack["result"]["ack"] == {"subscriptionId": "sub-1", "mode": "snapshot", "logEpoch": "epoch"}
    assert "SECRET" not in json.dumps(ack)


def test_malformed_completion_is_protocol_error_not_attribute_error():
    tracker = TurnTracker("session", "input")
    tracker.arm(0)
    tracker.ingest(started())
    malformed = completed(resultType="success")
    malformed["payload"]["response"] = {"apiKey": "SECRET"}
    with pytest.raises(ProtocolError):
        tracker.ingest(malformed)


def test_evidence_write_is_exclusive_and_has_no_strict_claim(tmp_path):
    result = client(FakeTransport()).run()
    output = tmp_path / "probe.json"
    write_evidence(output, result)
    assert json.loads(output.read_text())["strict_proof"] is False
    assert output.with_suffix(".events.jsonl").is_file()
    with pytest.raises(FileExistsError):
        write_evidence(output, result)


def test_ndjson_transport_handles_real_pipe_reply_and_stderr_without_deadlock():
    source = "import sys,json; sys.stderr.write('x'*100000); sys.stderr.flush(); " \
             "m=json.loads(sys.stdin.readline()); print(json.dumps({'id':m['id'],'result':{}}),flush=True)"
    transport = NdjsonTransport([sys.executable, "-c", source], cwd=Path.cwd())
    try:
        transport.send({"id": "request", "method": "test"}, 2)
        assert transport.receive(2) == {"id": "request", "result": {}}
    finally:
        transport.close(1)
    assert transport.process.poll() is not None


@pytest.mark.parametrize("source", ["print('not-json',flush=True)", "print('x'*2048,flush=True)"])
def test_ndjson_transport_rejects_malformed_and_oversized_lines(source):
    transport = NdjsonTransport([sys.executable, "-c", source], cwd=Path.cwd(), max_line_bytes=1024)
    try:
        with pytest.raises(ProtocolError):
            transport.receive(2)
    finally:
        transport.close(1)


def test_ndjson_rejects_duplicate_keys_in_an_ack():
    source = 'print(\'{"id":"wrong","id":"right","result":{}}\',flush=True)'
    transport = NdjsonTransport([sys.executable, "-c", source], cwd=Path.cwd())
    try:
        with pytest.raises(ProtocolError):
            transport.receive(2)
    finally:
        transport.close(1)


def test_ndjson_eof_and_timeout_are_bounded():
    transport = NdjsonTransport([sys.executable, "-c", "import time; time.sleep(30)"], cwd=Path.cwd())
    before = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            transport.receive(0.02)
    finally:
        transport.close(0.1)
    assert time.monotonic() - before < 3


def test_cli_help_does_not_launch_zcode():
    script = Path(__file__).parents[1] / "scripts" / "zcode_protocol_probe.py"
    proc = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0
    assert "--provider-config" in proc.stdout
    assert "--send" in proc.stdout
