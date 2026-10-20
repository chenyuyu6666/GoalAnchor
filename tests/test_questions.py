"""Pure question contract, lifecycle and untrusted-answer regressions."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.questions import Question, QuestionAnswer, QuestionOption, QuestionRecord


def question(**changes):
    return replace(Question("q", "g", 1, "Choose", "What next?"), **changes)


def answer(q, **changes):
    return replace(QuestionAnswer("a", q.question_id, q.fingerprint, custom="Continue"), **changes)


_V08_AUTHORIZATION_QUESTION = {
    "question_id": "q-legacy-001",
    "goal_id": "goal-001",
    "plan_revision": 7,
    "title": "Choose rollout",
    "prompt": "Approve the rollout?",
    "options": [
        {"option_id": "approve", "label": "Approve"},
        {"option_id": "reject", "label": "Reject"},
    ],
    "multi_select": False,
    "allow_custom": False,
    "blocking_items": ["deploy-prod"],
    "kind": "AUTHORIZATION",
    "operation_digest": "op-digest-v1",
    "approval_option_id": "approve",
    "question_revision": 3,
    "baseline_digest": "baseline-v7",
    "decision_key": "prod-rollout",
}
_V08_AUTHORIZATION_FINGERPRINT = "27e7231b759aa2c664297b475a8aeb4ccb5e7d5d2b3bb49e9864749331f976c3"
_V08_AUTHORIZATION_DEDUPE_KEY = "fb55c0134c01afc175e62c7174c5dc9a8668719ec7a27d32d18bf125ed7e8b1c"


def test_v08_question_wire_hashes_and_nested_answer_fingerprint_are_stable():
    legacy = Question.from_dict(_V08_AUTHORIZATION_QUESTION)

    assert legacy.to_dict() == _V08_AUTHORIZATION_QUESTION
    assert legacy.fingerprint == _V08_AUTHORIZATION_FINGERPRINT
    assert legacy.dedupe_key == _V08_AUTHORIZATION_DEDUPE_KEY

    legacy_answer = QuestionAnswer(
        "answer-v08",
        legacy.question_id,
        _V08_AUTHORIZATION_FINGERPRINT,
        selected=("approve",),
        custom=None,
        source_event_id="event-v08",
    )
    records = (
        QuestionRecord(legacy),
        QuestionRecord(legacy).wait(),
        QuestionRecord(legacy).wait().receive(legacy_answer),
    )
    for record in records:
        serialized = record.to_dict()
        assert serialized["question"] == _V08_AUTHORIZATION_QUESTION
        assert "trigger_reason" not in serialized["question"]
        assert QuestionRecord.from_dict(serialized) == record

    answered = records[-1].to_dict()
    assert answered["answer"]["question_fingerprint"] == _V08_AUTHORIZATION_FINGERPRINT


def test_legacy_question_with_explicit_null_reason_keeps_v08_wire_shape():
    legacy = Question.from_dict({**_V08_AUTHORIZATION_QUESTION, "trigger_reason": None})

    assert legacy.to_dict() == _V08_AUTHORIZATION_QUESTION
    assert legacy.fingerprint == _V08_AUTHORIZATION_FINGERPRINT
    assert legacy.dedupe_key == _V08_AUTHORIZATION_DEDUPE_KEY


def test_absent_expiry_and_authorization_context_keep_v08_question_identity():
    legacy = Question.from_dict(_V08_AUTHORIZATION_QUESTION)

    assert legacy.to_dict() == _V08_AUTHORIZATION_QUESTION
    assert legacy.fingerprint == _V08_AUTHORIZATION_FINGERPRINT
    assert legacy.dedupe_key == _V08_AUTHORIZATION_DEDUPE_KEY


def test_question_expiry_is_fingerprinted_but_not_part_of_dedupe_identity():
    first = Question.from_dict({
        **_V08_AUTHORIZATION_QUESTION,
        "expires_at": "2026-10-17T00:00:00+08:00",
    })
    later = Question.from_dict({
        **_V08_AUTHORIZATION_QUESTION,
        "expires_at": "2026-10-17T00:30:00+08:00",
    })

    assert first.to_dict()["expires_at"] == "2026-10-17T00:00:00+08:00"
    assert first.fingerprint != later.fingerprint
    assert first.dedupe_key == later.dedupe_key
    assert Question.from_dict(first.to_dict()) == first


@pytest.mark.parametrize(
    "expires_at",
    ["", 1, True, "not-a-time", "2026-10-17T00:00:00", "2026-02-30T00:00:00+00:00"],
)
def test_question_expiry_requires_a_valid_timezone_aware_timestamp(expires_at):
    with pytest.raises(ContractValidationError):
        Question.from_dict({**_V08_AUTHORIZATION_QUESTION, "expires_at": expires_at})


def test_authorization_context_digest_is_part_of_question_identity():
    first = Question.from_dict({
        **_V08_AUTHORIZATION_QUESTION,
        "authorization_context_digest": "a" * 64,
    })
    second = Question.from_dict({
        **_V08_AUTHORIZATION_QUESTION,
        "authorization_context_digest": "b" * 64,
    })

    assert first.to_dict()["authorization_context_digest"] == "a" * 64
    assert first.fingerprint != second.fingerprint
    assert first.dedupe_key != second.dedupe_key
    assert Question.from_dict(first.to_dict()) == first


@pytest.mark.parametrize("digest", ["", "0" * 63, "G" * 64, "a" * 63 + "A"])
def test_authorization_context_digest_requires_canonical_sha256(digest):
    with pytest.raises(ContractValidationError):
        Question.from_dict({
            **_V08_AUTHORIZATION_QUESTION,
            "authorization_context_digest": digest,
        })


def test_direction_questions_cannot_carry_authorization_context_digest():
    with pytest.raises(ContractValidationError):
        Question.from_dict({
            "question_id": "direction-1",
            "goal_id": "goal-1",
            "plan_revision": 1,
            "title": "Choose",
            "prompt": "Which path?",
            "authorization_context_digest": "a" * 64,
        })


def test_trigger_reason_is_serialized_and_changes_question_identity():
    first = Question.from_dict({
        **_V08_AUTHORIZATION_QUESTION,
        "trigger_reason": "The operation changes the production rollout.",
    })
    second = Question.from_dict({
        **_V08_AUTHORIZATION_QUESTION,
        "trigger_reason": "The production rollout needs explicit approval.",
    })

    assert first.to_dict()["trigger_reason"] == "The operation changes the production rollout."
    assert first.fingerprint != _V08_AUTHORIZATION_FINGERPRINT
    assert first.dedupe_key != _V08_AUTHORIZATION_DEDUPE_KEY
    assert first.fingerprint != second.fingerprint
    assert first.dedupe_key != second.dedupe_key
    assert Question.from_dict(first.to_dict()) == first


@pytest.mark.parametrize("reason", ["", "   ", 1, True])
def test_trigger_reason_rejects_empty_or_non_text_values(reason):
    with pytest.raises(ContractValidationError):
        Question.from_dict({**_V08_AUTHORIZATION_QUESTION, "trigger_reason": reason})


def test_t16_roundtrip_and_immutable_inputs():
    options = [QuestionOption("yes", "Yes")]
    q = question(options=options, blocking_items=["work"])
    options.clear()
    assert len(q.options) == 1 and q.blocking_items == ("work",)
    assert Question.from_dict(json.loads(json.dumps(q.to_dict()))) == q
    with pytest.raises(FrozenInstanceError):
        q.title = "changed"


@pytest.mark.parametrize("changes", [
    {"question_id": " "}, {"goal_id": ""}, {"plan_revision": True},
    {"plan_revision": -1}, {"question_revision": 0}, {"multi_select": 1},
    {"allow_custom": "yes"}, {"kind": "OTHER"}, {"options": "bad"},
    {"blocking_items": [""]}, {"baseline_digest": ""},
    {"options": [QuestionOption("a", "Same"), QuestionOption("b", "Same")]},
    {"options": [QuestionOption("a", "One"), QuestionOption("a", "Two")]},
    {"kind": "AUTHORIZATION"},
])
def test_t17_invalid_question_shapes(changes):
    with pytest.raises(ContractValidationError):
        question(**changes)


def test_t18_identity_and_dedupe_bindings():
    q = question()
    renamed = replace(q, question_id="other", question_revision=2)
    assert renamed.fingerprint != q.fingerprint
    assert renamed.dedupe_key == q.dedupe_key
    for changes in ({"prompt": "Changed"}, {"baseline_digest": "base"},
                    {"blocking_items": ("w",)}, {"plan_revision": 2}, {"decision_key": "purpose"}):
        assert replace(q, **changes).dedupe_key != q.dedupe_key


def test_t19_lifecycle_idempotency_and_roundtrip():
    q = question()
    created = QuestionRecord(q)
    waiting = created.wait()
    assert created.state == "CREATED" and waiting.decision == "WAIT"
    assert waiting.receive(answer(q, custom=None)) == waiting
    received = waiting.receive(answer(q))
    assert received.decision == "ANSWERED"
    assert received.receive(answer(q)) == received
    assert QuestionRecord.from_dict(json.loads(json.dumps(received.to_dict()))) == received
    with pytest.raises(ContractValidationError):
        received.receive(answer(q, source_event_id="different"))


@pytest.mark.parametrize("method", ["cancel", "expire", "supersede"])
def test_t20_terminal_states_reject_late_answers_and_revival(method):
    q = question()
    terminal = getattr(QuestionRecord(q).wait(), method)()
    for transition in (terminal.wait, terminal.cancel, terminal.expire, terminal.supersede):
        with pytest.raises(ContractValidationError):
            transition()
    with pytest.raises(ContractValidationError):
        terminal.receive(answer(q))


@pytest.mark.parametrize("changes", [
    {"question_id": "old"}, {"question_fingerprint": "old"},
    {"selected": ("missing",)}, {"selected": ("yes", "no")},
    {"selected": ("yes", "yes")},
])
def test_t21_reject_invalid_answers(changes):
    q = question(options=(QuestionOption("yes", "Yes"), QuestionOption("no", "No")))
    with pytest.raises(ContractValidationError):
        QuestionRecord(q).wait().receive(answer(q, **changes))


def test_t22_authorization_requires_explicit_approval():
    q = question(kind="AUTHORIZATION", operation_digest="operation", approval_option_id="yes",
                 options=(QuestionOption("yes", "Yes"), QuestionOption("no", "No")))
    waiting = QuestionRecord(q).wait()
    assert waiting.receive(answer(q, selected=("yes",), custom=None)).decision == "APPROVE"
    assert waiting.receive(answer(q, selected=("no",), custom=None)).decision == "DENY"
    assert waiting.receive(answer(q, selected=("yes",))).decision != "APPROVE"
    assert waiting.receive(answer(q, source_event_id="trusted-user")).decision != "APPROVE"
    with pytest.raises(ContractValidationError):
        replace(q, multi_select=True)
    with pytest.raises(ContractValidationError):
        replace(q, approval_option_id="missing")


def test_t52_strict_deserialization_and_state_consistency():
    q = question()
    for cls, payload in ((Question, q.to_dict()), (QuestionAnswer, answer(q).to_dict()),
                         (QuestionRecord, QuestionRecord(q).to_dict()),
                         (QuestionOption, QuestionOption("x", "X").to_dict())):
        with pytest.raises(ContractValidationError):
            cls.from_dict({**payload, "trusted": True})
    for state, supplied in (("ANSWERED", None), ("WAITING", answer(q)), ("FAKE", None)):
        with pytest.raises(ContractValidationError):
            QuestionRecord(q, state, supplied)
    with pytest.raises(ContractValidationError):
        QuestionRecord(q, "ANSWERED", answer(q, question_fingerprint="old"))


def test_multiselect_custom_rules_and_untrusted_data_roundtrip():
    q = question(multi_select=True, allow_custom=False,
                 options=(QuestionOption("a", "A"), QuestionOption("b", "B")))
    a = answer(q, selected=["a", "b"], custom=None)
    assert QuestionAnswer.from_dict(a.to_dict()) == a
    assert QuestionRecord(q).wait().receive(a).decision == "ANSWERED"
    with pytest.raises(ContractValidationError):
        QuestionRecord(q).wait().receive(answer(q))
    with pytest.raises(ContractValidationError):
        QuestionRecord(q).receive(a)


@pytest.mark.parametrize("changes", [
    {"answer_id": ""}, {"question_id": ""}, {"question_fingerprint": ""},
    {"selected": "yes"}, {"selected": [1]}, {"custom": True}, {"source_event_id": ""},
])
def test_answer_shapes_are_validated_before_use(changes):
    with pytest.raises(ContractValidationError):
        answer(question(), **changes)


@pytest.mark.parametrize("cls", [Question, QuestionOption, QuestionAnswer, QuestionRecord])
def test_missing_fields_and_nonobjects_are_contract_errors(cls):
    for payload in ({}, None, [], {1: "bad-key"}):
        with pytest.raises(ContractValidationError):
            cls.from_dict(payload)


def test_option_labels_cannot_hide_whitespace_ambiguity():
    with pytest.raises(ContractValidationError):
        question(options=(QuestionOption("a", "Yes"), QuestionOption("b", " Yes ")))
    with pytest.raises(ContractValidationError):
        QuestionOption("", "Yes")
    with pytest.raises(ContractValidationError):
        QuestionOption("a", "")
