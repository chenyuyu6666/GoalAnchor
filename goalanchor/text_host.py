"""Trusted control-side text fallback shared by supported host adapters.

This is not a model-facing signing endpoint or a host-wide tool interceptor.
The provider must be owned by the application, never supplied by model JSON.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from uuid import uuid4

from .host_contract import capabilities
from .questions import QuestionRecord
from .workflow import Workflow


class TextHostAdapter:
    def __init__(self, host: str, runtime: Workflow, *,
                 provider: Callable[[str], str | None] | None = None,
                 output: Callable[[str], None] = print):
        self.capabilities = capabilities(host)
        self.runtime = runtime
        self.provider = provider
        self.output = output

    def answer(self, question_id: str) -> QuestionRecord:
        raw = self.runtime.store.get("question", question_id)
        if raw is None:
            raise ValueError("Unknown question")
        record = QuestionRecord.from_dict(raw["record"])
        if record.state != "WAITING":
            return record
        question = record.question
        rendered = "\n".join((question.title, question.prompt,
                              *(f"{o.option_id}: {o.label}" for o in question.options)))
        self.output(rendered)
        response: str | None
        try:
            if self.provider is None:
                if not sys.stdin.isatty():
                    return record
                response = input("Enter an option ID or text:answer; leave empty to keep waiting: ")
            else:
                response = self.provider(rendered)
        except (EOFError, TimeoutError):
            return record
        if not response or not isinstance(response, str):
            return record
        selected = response.strip()
        custom = selected[5:].strip() if selected.startswith("text:") and question.allow_custom else None
        chosen = [] if custom else [selected]
        if not custom and selected not in {option.option_id for option in question.options}:
            return record
        event = self.runtime.user_authority.sign("user.answer", question.fingerprint, {
            "answer_id": "text-" + uuid4().hex, "selected": chosen, "custom": custom}, at=self.runtime.clock())
        return self.runtime.answer(question_id, event)
