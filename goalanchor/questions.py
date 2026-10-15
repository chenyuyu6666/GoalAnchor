"""Immutable question records; host provenance must be verified before receive.

An answer (including source_event_id) is data, never proof of trusted origin.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Self

from .core import ContractValidationError


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{name} must be non-empty text")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ContractValidationError(f"{name} must be an array")
    result = tuple(_text(item, name) for item in value)
    if len(set(result)) != len(result):
        raise ContractValidationError(f"{name} contains duplicates")
    return result


class _Serializable:
    def to_dict(self) -> dict[str, Any]:
        # JSON arrays also make serialized data independent of input container types.
        return json.loads(json.dumps(asdict(self)))  # type: ignore[call-overload,no-any-return]

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
            raise ContractValidationError(f"{cls.__name__} requires an object")
        allowed = {field.name for field in fields(cls)}  # type: ignore[arg-type]
        if set(payload) - allowed:
            raise ContractValidationError(f"Unknown {cls.__name__} fields")
        data = dict(payload)
        if cls is Question and "options" in data:
            if not isinstance(data["options"], (list, tuple)):
                raise ContractValidationError("options must be an array")
            data["options"] = tuple(QuestionOption.from_dict(option) for option in data["options"])
        if cls is QuestionRecord:
            if "question" in data:
                data["question"] = Question.from_dict(data["question"])
            if data.get("answer") is not None:
                data["answer"] = QuestionAnswer.from_dict(data["answer"])
        try:
            return cls(**data)
        except TypeError as exc:
            raise ContractValidationError(f"Invalid {cls.__name__} fields") from exc


@dataclass(frozen=True)
class QuestionOption(_Serializable):
    option_id: str
    label: str

    def __post_init__(self) -> None:
        _text(self.option_id, "option_id")
        _text(self.label, "label")


@dataclass(frozen=True)
class Question(_Serializable):
    question_id: str
    goal_id: str
    plan_revision: int
    title: str
    prompt: str
    options: tuple[QuestionOption, ...] = ()
    multi_select: bool = False
    allow_custom: bool = True
    blocking_items: tuple[str, ...] = ()
    kind: str = "DIRECTION"
    operation_digest: str | None = None
    approval_option_id: str | None = None
    question_revision: int = 1
    baseline_digest: str | None = None
    decision_key: str | None = None

    def __post_init__(self) -> None:
        for name in ("question_id", "goal_id", "title", "prompt"):
            _text(getattr(self, name), name)
        for name in ("operation_digest", "approval_option_id", "baseline_digest", "decision_key"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        for name in ("plan_revision", "question_revision"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ContractValidationError(f"{name} must be a positive integer")
        if type(self.multi_select) is not bool or type(self.allow_custom) is not bool:
            raise ContractValidationError("question flags must be booleans")
        if not isinstance(self.options, (tuple, list)) or any(
            not isinstance(option, QuestionOption) for option in self.options
        ):
            raise ContractValidationError("options must contain QuestionOption values")
        object.__setattr__(self, "options", tuple(self.options))
        object.__setattr__(self, "blocking_items", _strings(self.blocking_items, "blocking_items"))
        for name in ("option_id", "label"):
            values = [getattr(option, name).strip() for option in self.options]
            if len(set(values)) != len(values):
                raise ContractValidationError(f"Duplicate option {name}")
        if self.kind not in ("DIRECTION", "AUTHORIZATION"):
            raise ContractValidationError("Invalid question kind")
        if self.kind == "AUTHORIZATION" and (
            self.operation_digest is None or self.multi_select
            or self.approval_option_id not in {option.option_id for option in self.options}
        ):
            raise ContractValidationError("Authorization requires exact operation and single approval option")

    def _digest(self, *, dedupe: bool) -> str:
        data = self.to_dict()
        if dedupe:
            del data["question_id"]
            del data["question_revision"]
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._digest(dedupe=False)

    @property
    def dedupe_key(self) -> str:
        return self._digest(dedupe=True)


@dataclass(frozen=True)
class QuestionAnswer(_Serializable):
    answer_id: str
    question_id: str
    question_fingerprint: str
    selected: tuple[str, ...] = ()
    custom: str | None = None
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("answer_id", "question_id", "question_fingerprint"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "selected", _strings(self.selected, "selected"))
        if self.custom is not None:
            if not isinstance(self.custom, str):
                raise ContractValidationError("custom must be text or null")
            if not self.custom.strip():
                object.__setattr__(self, "custom", None)
        if self.source_event_id is not None:
            _text(self.source_event_id, "source_event_id")


@dataclass(frozen=True)
class QuestionRecord(_Serializable):
    question: Question
    state: str = "CREATED"
    answer: QuestionAnswer | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.question, Question):
            raise ContractValidationError("question must be Question")
        if self.state not in ("CREATED", "WAITING", "ANSWERED", "CANCELLED", "EXPIRED", "SUPERSEDED"):
            raise ContractValidationError("Invalid question state")
        if self.state == "ANSWERED":
            self._validate_answer(self.answer)
            if self.answer is None or (not self.answer.selected and self.answer.custom is None):
                raise ContractValidationError("ANSWERED requires a non-empty answer")
        elif self.answer is not None:
            raise ContractValidationError("Only ANSWERED records can contain an answer")

    def _validate_answer(self, answer: QuestionAnswer | None) -> None:
        q = self.question
        if not isinstance(answer, QuestionAnswer):
            raise ContractValidationError("answer must be QuestionAnswer")
        if answer.question_id != q.question_id or answer.question_fingerprint != q.fingerprint:
            raise ContractValidationError("Answer refers to another question version")
        if set(answer.selected) - {option.option_id for option in q.options}:
            raise ContractValidationError("Unknown selected option")
        if not q.multi_select and len(answer.selected) > 1:
            raise ContractValidationError("Question permits only one selection")
        if answer.custom is not None and not q.allow_custom:
            raise ContractValidationError("Question forbids custom text")

    def wait(self) -> QuestionRecord:
        if self.state != "CREATED":
            raise ContractValidationError("Only CREATED can become WAITING")
        return replace(self, state="WAITING")

    def receive(self, answer: QuestionAnswer) -> QuestionRecord:
        """Receive data after the caller verifies the trusted control-side event."""
        if self.state == "ANSWERED" and answer == self.answer:
            return self
        if self.state != "WAITING":
            raise ContractValidationError("Only WAITING can receive a new answer")
        self._validate_answer(answer)
        if not answer.selected and answer.custom is None:
            return self
        return replace(self, state="ANSWERED", answer=answer)

    def _terminate(self, state: str) -> QuestionRecord:
        if self.state not in ("CREATED", "WAITING"):
            raise ContractValidationError("Terminal question cannot transition")
        return replace(self, state=state)

    def cancel(self) -> QuestionRecord:
        return self._terminate("CANCELLED")

    def expire(self) -> QuestionRecord:
        return self._terminate("EXPIRED")

    def supersede(self) -> QuestionRecord:
        return self._terminate("SUPERSEDED")

    @property
    def decision(self) -> str:
        if self.state != "ANSWERED" or self.answer is None:
            return "WAIT"
        if self.question.kind == "DIRECTION":
            return "ANSWERED"
        if self.answer.custom is not None or not self.answer.selected:
            return "ANSWERED"
        return "APPROVE" if self.answer.selected == (self.question.approval_option_id,) else "DENY"
