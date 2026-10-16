import pytest
from test_workflow import operation, review, setup_workflow

from goalanchor.questions import Question
from goalanchor.text_host import TextHostAdapter


@pytest.mark.parametrize("host", ["codex", "claude", "dsh", "zcode", "pi-desktop"])
def test_t46_t48_same_trusted_text_answer_drives_same_core_governance(tmp_path, host):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    op = operation(tmp_path, base)
    runtime.register_operation(op)
    question = runtime.request_authorization("O1").question
    shown = []
    adapter = TextHostAdapter(host, runtime, provider=lambda _: "approve", output=shown.append)
    assert adapter.answer(question.question_id).decision == "APPROVE"
    assert question.prompt in shown[0]
    calls = []
    receipt = runtime.execute("O1", "text", lambda _: calls.append(1) or {"exit_code": 0})
    assert receipt["state"] == "SUCCEEDED"
    assert calls == [1]
    assert adapter.answer(question.question_id).decision == "APPROVE"
    assert len(shown) == 1


@pytest.mark.parametrize("response", [None, "", "wrong-option"])
def test_no_provider_or_invalid_text_never_approves(tmp_path, response):
    runtime, base, _, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    runtime.register_operation(operation(tmp_path, base))
    question = runtime.request_authorization("O1").question
    adapter = TextHostAdapter("codex", runtime, provider=lambda _: response, output=lambda _: None)
    assert adapter.answer(question.question_id).decision == "WAIT"
    assert runtime.store.list("grant", "G1") == ()


@pytest.mark.parametrize("failure", [EOFError, TimeoutError])
def test_text_input_unavailable_keeps_question_waiting(tmp_path, failure):
    runtime, base, _, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    runtime.register_operation(operation(tmp_path, base))
    question = runtime.request_authorization("O1").question

    def unavailable(_):
        raise failure()

    adapter = TextHostAdapter("codex", runtime, provider=unavailable, output=lambda _: None)
    assert adapter.answer(question.question_id).decision == "WAIT"
    assert runtime.store.list("grant", "G1") == ()


def test_direction_custom_text_never_becomes_authorization(tmp_path):
    runtime, base, _, _ = setup_workflow(tmp_path)
    question = Question("direction", "G1", 1, "Direction", "Describe target", baseline_digest=base.fingerprint)
    runtime.request_question(question)
    adapter = TextHostAdapter("claude", runtime, provider=lambda _: "text:ship locally", output=lambda _: None)
    assert adapter.answer(question.question_id).decision == "ANSWERED"
    assert runtime.store.list("grant", "G1") == ()
