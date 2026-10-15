from __future__ import annotations

from dataclasses import replace

import pytest
from test_workflow import NOW, approve, operation, review, setup_workflow

from goalanchor.codex_controller import CodexController
from goalanchor.core import ContractValidationError
from goalanchor.workflow import Workflow
from goalanchor.workflow_protocol import canonical

BINDING = canonical({"host": "codex", "user_agent": "test-host/1", "model": "test-model",
                     "mode": "approval-controlled", "hook_enabled": True, "hook_trust": "controller-owned"})


class Client:
    def __init__(self):
        self.responses = []

    def respond(self, ident, result):
        self.responses.append((ident, result))

    def next_event(self, timeout=None):
        return {"method": "item/completed", "params": {"threadId": "T", "turnId": "R",
                "item": {"id": "I", "type": "commandExecution", "exitCode": 0}}}

    def request(self, method, params):
        return {}


def test_no_implicit_approval_and_cross_turn_rejected():
    client = Client()
    controller = CodexController(client, "T", "R")
    request = {"id": 1, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "T", "turnId": "other", "itemId": "I", "command": "echo hello", "cwd": "/tmp"}}
    with pytest.raises(ContractValidationError):
        controller.command(request)
    assert client.responses == []


def test_command_denial_is_correlated_and_once():
    client = Client()
    controller = CodexController(client, "T", "R")
    request = {"id": 1, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "T", "turnId": "R", "itemId": "I", "command": "echo hello", "cwd": "/tmp"}}
    controller.command(request)
    assert not client.responses
    controller.reject(1)
    assert client.responses == [(1, {"decision": "decline"})]
    with pytest.raises(ContractValidationError):
        controller.reject(1)


def test_unknown_file_approval_is_never_supported():
    controller = CodexController(Client(), "T", "R")
    with pytest.raises(ContractValidationError):
        controller.command({"id": 1, "method": "item/fileChange/requestApproval", "params": {}})


@pytest.mark.parametrize("lost_result", [False, True])
def test_dispatch_requires_exact_durable_authority_and_records_host_result(tmp_path, lost_result):
    original, base, user, observer = setup_workflow(tmp_path)
    runtime = Workflow(original.store, user, observer,
                       replace(original.policy, allowed_tools=("codex.command",), host_binding=BINDING),
                       clock=lambda: NOW)
    base = replace(base, revision=2)
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict(), at=NOW))
    review(runtime, observer)
    params = {"threadId": "T", "turnId": "R", "itemId": "I", "command": "echo hello", "cwd": str(tmp_path)}
    op = replace(operation(tmp_path, base), tool="codex.command", arguments_json=canonical(params))
    runtime.register_operation(op)
    client = Client()
    if lost_result:
        def no_result(timeout=None):
            raise TimeoutError("connection lost after approval")
        client.next_event = no_result
    controller = CodexController(client, "T", "R", host_binding=BINDING)
    controller.command({"id": 1, "method": "item/commandExecution/requestApproval", "params": params})
    with pytest.raises(ContractValidationError):
        controller.dispatch(1, runtime, "O1", "attempt")
    assert client.responses == []
    approve(runtime, op, user)
    receipt = controller.dispatch(1, runtime, "O1", "attempt")
    assert receipt["state"] == ("UNKNOWN" if lost_result else "SUCCEEDED")
    assert client.responses == [(1, {"decision": "accept"})]
    assert controller.approval_outcomes[1] == "SENT"


def test_cancelled_controller_cannot_send_pending_approval(tmp_path):
    client = Client()
    controller = CodexController(client, "T", "R")
    params = {"threadId": "T", "turnId": "R", "itemId": "I", "command": "echo hello", "cwd": str(tmp_path)}
    controller.command({"id": 1, "method": "item/commandExecution/requestApproval", "params": params})
    controller.cancel()
    with pytest.raises(ContractValidationError, match="cancel"):
        controller.dispatch(1, None, "O1", "attempt")
    assert client.responses == []


def test_native_question_waits_for_verified_answer_and_cannot_grant_authority(tmp_path):
    runtime, base, user, _ = setup_workflow(tmp_path)
    client = Client()
    controller = CodexController(client, "T", "R")
    request = {"id": 7, "method": "item/tool/requestUserInput", "params": {
        "threadId": "T", "turnId": "R", "itemId": "I", "isBlocking": True,
        "questions": [{"id": "choice", "header": "Direction", "question": "Which artifact?",
                       "options": [{"label": "Alpha", "description": "First"},
                                   {"label": "Beta", "description": "Second"}]}]}}
    records = controller.questions(request, runtime, "G1")
    assert client.responses == []
    question = records[0].question
    assert question.kind == "DIRECTION"
    assert runtime.store.list("grant", "G1") == ()
    event = user.sign("user.answer", question.fingerprint,
                      {"answer_id": "native-choice", "selected": ["option-0"], "custom": None}, at=NOW)
    controller.answer_questions(7, runtime, {question.question_id: event})
    assert client.responses == [(7, {"answers": {"choice": {"answers": ["Alpha"]}}})]
    assert runtime.store.list("grant", "G1") == ()
