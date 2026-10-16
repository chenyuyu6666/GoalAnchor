"""Trusted controller for one Codex turn, with no model-accessible signer.

Only registered exact callbacks can reach Workflow.execute. This controls the
observed command approval path, not every tool or shell side effect in a host.
"""
from __future__ import annotations

import threading
import time
from typing import Any, cast

from .codex_host import CodexClient, command_approval, user_question_payload
from .core import ContractValidationError
from .host_contract import validate_host_binding
from .questions import Question, QuestionOption, QuestionRecord
from .workflow import Workflow
from .workflow_protocol import Operation, SignedEvent, canonical, fingerprint, text


class CodexController:
    def __init__(self, client: CodexClient, thread_id: str, turn_id: str, *, host_binding: str | None = None):
        text(thread_id, "thread_id")
        text(turn_id, "turn_id")
        self.client = client
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.host_binding = validate_host_binding(host_binding) if host_binding is not None else None
        self._pending: dict[int | str, dict[str, Any]] = {}
        self.notifications: list[dict[str, Any]] = []
        self.approval_outcomes: dict[int | str, str] = {}
        self._cancelled = False
        self._lock = threading.RLock()
        self._questions: dict[int | str, dict[str, str]] = {}

    def questions(self, request: dict[str, Any], runtime: Workflow, goal_id: str) -> tuple[QuestionRecord, ...]:
        """Persist native direction questions; answers require the configured user authority."""
        if self._cancelled or request.get("method") != "item/tool/requestUserInput":
            raise ContractValidationError("Unsupported or cancelled question request")
        ident = request.get("id")
        if type(ident) not in (int, str) or ident in self._questions or ident in self._pending:
            raise ContractValidationError("Invalid question request identity")
        params = user_question_payload(request.get("params", {}))
        if params["threadId"] != self.thread_id or params["turnId"] != self.turn_id:
            raise ContractValidationError("Question belongs to another turn")
        goal = runtime.store.get("goal", goal_id)
        if goal is None:
            raise ContractValidationError("Question has no current goal")
        mapping = {}
        records = []
        for item in params["questions"]:
            options = item.get("options") or []
            if not isinstance(options, list):
                raise ContractValidationError("Invalid native options")
            question = Question(
                "codex-question-" + fingerprint({"thread": self.thread_id, "turn": self.turn_id,
                                                "item": params["itemId"], "id": item["id"], "goal": goal_id}),
                goal_id, goal["revision"], item["header"], item["question"],
                tuple(QuestionOption(f"option-{index}", option["label"]) for index, option in enumerate(options)),
                allow_custom=True, blocking_items=(params["itemId"],) if params.get("isBlocking", True) else (),
                baseline_digest=goal["baseline_digest"],
            )
            record = runtime.request_question(question)
            mapping[item["id"]] = record.question.question_id
            records.append(record)
        self._questions[cast(int | str, ident)] = mapping
        return tuple(records)

    def answer_questions(self, request_id: int | str, runtime: Workflow, events: dict[str, SignedEvent]) -> None:
        if self._cancelled or request_id not in self._questions:
            raise ContractValidationError("Unknown or cancelled question request")
        mapping = self._questions[request_id]
        if set(events) != set(mapping.values()):
            raise ContractValidationError("All correlated question answers are required")
        answers = {}
        for native_id, question_id in mapping.items():
            record = runtime.answer(question_id, events[question_id])
            if record.state != "ANSWERED" or record.answer is None:
                raise ContractValidationError("Question remains unanswered")
            labels = {option.option_id: option.label for option in record.question.options}
            chosen = [labels[value] for value in record.answer.selected]
            if record.answer.custom:
                chosen.append(record.answer.custom)
            answers[native_id] = {"answers": chosen}
        with self._lock:
            if self._cancelled:
                raise ContractValidationError("Turn was cancelled before answer")
            self.client.respond(request_id, {"answers": answers})
            del self._questions[request_id]

    def command(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._cancelled:
            raise ContractValidationError("Turn was cancelled")
        if request.get("method") != "item/commandExecution/requestApproval":
            raise ContractValidationError("Unsupported approval method")
        ident = request.get("id")
        if type(ident) not in (int, str) or ident in self._pending:
            raise ContractValidationError("Invalid or duplicate approval request")
        params = command_approval(request.get("params", {}))
        if params["threadId"] != self.thread_id or params["turnId"] != self.turn_id:
            raise ContractValidationError("Approval belongs to another turn")
        self._pending[cast(int | str, ident)] = params
        return dict(params)

    def reject(self, request_id: int | str, *, cancel: bool = False) -> None:
        if request_id not in self._pending:
            raise ContractValidationError("Unknown approval request")
        self.client.respond(request_id, {"decision": "cancel" if cancel else "decline"})
        del self._pending[request_id]

    def dispatch(self, request_id: int | str, runtime: Workflow, operation_id: str,
                 attempt_id: str, *, timeout: float = 30) -> dict[str, Any]:
        """Consume exact pre-existing authority before replying accept.

        The trusted embedding must restrict which host tools bypass callbacks.
        A missing completion becomes UNKNOWN and never an automatic retry.
        """
        if self._cancelled:
            raise ContractValidationError("Turn was cancelled")
        if request_id not in self._pending:
            raise ContractValidationError("Unknown approval request")
        if self.host_binding is None or self.host_binding != runtime.policy.host_binding:
            raise ContractValidationError("Trusted host binding is missing or changed")
        params = self._pending[request_id]
        operation_data = runtime.store.get("operation", operation_id)
        if operation_data is None:
            raise ContractValidationError("No registered operation")
        operation = Operation.from_dict(operation_data)
        if (operation.tool != "codex.command" or canonical(operation.arguments) != canonical(params)
                or operation.cwd != params["cwd"]):
            raise ContractValidationError("Callback differs from registered exact operation")
        if not 0 < timeout <= 300:
            raise ContractValidationError("Completion timeout must be bounded")

        def execute(_: Operation) -> dict[str, Any]:
            with self._lock:
                if self._cancelled:
                    raise ContractValidationError("Turn was cancelled before approval")
                self.approval_outcomes[request_id] = "UNKNOWN"
                self.client.respond(request_id, {"decision": "accept"})
                self.approval_outcomes[request_id] = "SENT"
                del self._pending[request_id]
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                event = self.client.next_event(timeout=max(.001, deadline - time.monotonic()))
                data = event.get("params", {})
                if (event.get("method") == "item/completed" and data.get("threadId") == self.thread_id
                        and data.get("turnId") == self.turn_id):
                    item = data.get("item", {})
                    if item.get("id") == params["itemId"] and item.get("type") == "commandExecution":
                        code = item.get("exitCode")
                        if type(code) is not int:
                            raise ContractValidationError("Host completion lacks exit code")
                        return {"exit_code": code, "object_ref": operation_id,
                                "source_request_id": str(request_id), "item_id": params["itemId"]}
                if len(self.notifications) >= 1024:
                    raise ContractValidationError("Unconsumed notification limit exceeded")
                self.notifications.append(event)
            raise TimeoutError("No correlated completion")

        return runtime.execute(operation_id, attempt_id, execute)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
        self.client.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})
