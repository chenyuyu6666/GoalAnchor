"""Additional exact local acceptance cases; no native UI or external account claim."""
import json
import multiprocessing
import os
from copy import deepcopy
from dataclasses import replace

import pytest
from test_workflow import NOW, approve, operation, review, setup_workflow, synthetic_delivery
from test_workflow_contract_cases import second_round

from goalanchor.core import ContractValidationError
from goalanchor.integration import ReviewFinding
from goalanchor.questions import Question, QuestionOption
from goalanchor.review_service import ReviewContext
from goalanchor.workflow import Workflow, WorkflowPolicy
from goalanchor.workflow_protocol import EventAuthority, EvidenceSnapshot, canonical
from goalanchor.workflow_store import WorkflowStore


def test_t06_agent_baseline_change_request_is_retained_without_mutating_baseline(tmp_path):
    runtime, base, _, _ = setup_workflow(tmp_path)
    proposal = replace(base, revision=2, criteria=(replace(base.criteria[0], expected_json=canonical("other")),))
    request = Question("baseline-change-request", "G1", 1, "Proposed acceptance change",
                       canonical(proposal.to_dict()), baseline_digest=base.fingerprint,
                       decision_key="baseline-change")
    runtime.request_question(request)
    model = EventAuthority(b"m" * 32, "model")
    with pytest.raises(ContractValidationError):
        runtime.start_baseline(proposal, model.sign("user.baseline", proposal.fingerprint, proposal.to_dict(), at=NOW))
    assert runtime.store.get("goal", "G1")["baseline_digest"] == base.fingerprint
    assert runtime.store.get("question", request.question_id)["record"]["question"]["prompt"] == request.prompt


def test_t08_same_commit_changed_workspace_diff_changes_frozen_context():
    old = ReviewContext("goal", "plan", (), ("commit",), canonical({"commit": "abc123", "diff": "+old"}))
    new = replace(old, candidate=canonical({"commit": "abc123", "diff": "+new"}))
    assert json.loads(old.candidate)["commit"] == json.loads(new.candidate)["commit"]
    assert old.fingerprint != new.fingerprint
    stale = old.to_dict()
    stale["candidate"] = new.candidate
    with pytest.raises(ContractValidationError):
        ReviewContext.from_dict(stale)


def test_t11_majority_refutation_without_counterevidence_stays_blocked(tmp_path):
    runtime, _, _, observer = setup_workflow(tmp_path)
    finding = ReviewFinding("F1", "important objection", ("E1",), "blocks", "claim")
    first = review(runtime, observer, finding=finding,
                   dispositions={"F1": {"status": "REFUTED", "evidence_refs": []}})
    assert first["status"] == "REVIEW_REQUIRED"
    bundle = second_round(first)
    ctx = runtime.review_context("G1")
    synthetic_delivery(runtime, bundle)
    stopped = runtime.submit_review("G1", bundle, observer.sign("observer.review", ctx.fingerprint, bundle, at=NOW))
    assert stopped["status"] == "STOPPED" and stopped["unresolved"] == ["F1"]
    assert runtime.store.list("receipt", "G1") == ()


def test_t18_no_answer_or_empty_selection_never_authorizes(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    runtime.register_operation(operation(tmp_path, base))
    question = runtime.request_authorization("O1").question
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "no-answer", lambda _: pytest.fail("no explicit answer"))
    empty = user.sign("user.answer", question.fingerprint,
                      {"answer_id": "empty", "selected": [], "custom": None}, at=NOW)
    assert runtime.answer(question.question_id, empty).decision != "APPROVE"
    assert runtime.store.list("grant", "G1") == ()
    assert runtime.store.list("receipt", "G1") == ()


def test_t17_same_text_different_goal_or_authorization_scope_never_deduplicates(tmp_path):
    runtime, base, _, _ = setup_workflow(tmp_path)
    question = Question("q", "G1", 1, "Approve?", "Same text", (QuestionOption("yes", "Yes"),),
                        kind="AUTHORIZATION", approval_option_id="yes", operation_digest="a" * 64,
                        baseline_digest=base.fingerprint)
    different_goal = replace(question, question_id="other-goal", goal_id="G2")
    different_scope = replace(question, question_id="other-scope", operation_digest="b" * 64)
    assert question.prompt == different_goal.prompt == different_scope.prompt
    assert len({q.dedupe_key for q in (question, different_goal, different_scope)}) == 3
    assert runtime.request_question(question).question == question
    assert runtime.request_question(different_scope).question == different_scope
    assert len(runtime.store.list("question", "G1")) == 2


def test_t20_reworded_authorization_reuses_existing_refusal(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    runtime.register_operation(operation(tmp_path, base))
    question = runtime.request_authorization("O1").question
    runtime.answer(question.question_id, user.sign("user.answer", question.fingerprint,
                   {"answer_id": "deny", "selected": ["deny"], "custom": None}, at=NOW))
    reworded = replace(question, question_id="reworded", prompt="Please reconsider this same operation")
    assert runtime.request_question(reworded).decision == "DENY"
    assert len(runtime.store.list("question", "G1")) == 1
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "refused", lambda _: pytest.fail("refusal still applies"))


def test_t21_pending_question_blocks_dependent_dispatch_but_independent_authorized_work_runs(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    independent = replace(operation(tmp_path, base), operation_id="O2", work_item_id="independent")
    approve(runtime, independent, user)
    runtime.request_question(Question("pending", "G1", 1, "direction", "Which?", (QuestionOption("a", "A"),),
                             blocking_items=("W1",), baseline_digest=base.fingerprint))
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "dependent", lambda _: pytest.fail("pending dependency"))
    calls = []
    assert runtime.execute("O2", "independent", lambda _: calls.append(1) or {"exit_code": 0})["state"] == "SUCCEEDED"
    assert calls == [1]


def workflow_contender(directory, attempt, ready, start, results):
    runtime = Workflow(WorkflowStore(os.path.join(directory, "state.db")),
                       EventAuthority(b"u" * 32, "trusted-ui"), EventAuthority(b"o" * 32, "trusted-runner"),
                       WorkflowPolicy(directory, ("write_text",)), clock=lambda: NOW)
    ready.put(True)
    start.wait(10)
    def send(_):
        descriptor = os.open(os.path.join(directory, "dispatches"),
                             os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
        try:
            os.write(descriptor, b"send\n")
        finally:
            os.close(descriptor)
        return {"exit_code": 0}
    try:
        runtime.execute("O1", attempt, send)
        results.put("won")
    except ContractValidationError:
        results.put("rejected")


def test_t24_t25_two_workflow_processes_one_authorization_one_dispatch(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    context = multiprocessing.get_context("spawn")
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=workflow_contender, args=(str(tmp_path), str(n), ready, start, results))
               for n in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=15)
        start.set()
        assert sorted(results.get(timeout=15) for _ in workers) == ["rejected", "won"]
        assert (tmp_path / "dispatches").read_bytes() == b"send\n"
    finally:
        start.set()
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        assert all(worker.exitcode == 0 for worker in workers)


def accepted_risk(tmp_path):
    runtime, old, user, observer = setup_workflow(tmp_path)
    runtime.policy = replace(runtime.policy, acceptable_risks=("F1",))
    base = replace(old, revision=2)
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict(), at=NOW))
    finding = ReviewFinding("F1", "bounded risk", ("E1",), "needs acceptance", "known")
    first = review(runtime, observer, finding=finding)
    payload = {"goal_id": "G1", "baseline_digest": base.fingerprint, "finding_id": "F1",
               "reason": "accept bounded risk", "expires_at": "2026-10-16T02:00:00+08:00"}
    runtime.accept_risk("G1", "F1", user.sign("user.risk", base.fingerprint, payload, at=NOW))
    bundle = second_round(first)
    bundle["dispositions"] = {"F1": {"status": "RISK_ACCEPTED", "evidence_refs": ["E1"]}}
    ctx = runtime.review_context("G1")
    synthetic_delivery(runtime, bundle)
    assert runtime.submit_review("G1", bundle, observer.sign("observer.review", ctx.fingerprint, bundle, at=NOW))[
        "status"] == "PASSED"
    return runtime, base, user, observer


def test_t30_actual_accepted_risk_never_overrides_hard_tool_policy(tmp_path):
    runtime, base, _, _ = accepted_risk(tmp_path)
    with pytest.raises(ContractValidationError):
        runtime.register_operation(replace(operation(tmp_path, base), tool="forbidden"))
    assert runtime.store.list("receipt", "G1") == ()


def test_t38_cancel_then_new_run_does_not_inherit_late_old_success(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    new = replace(base, revision=2, plan="new run")
    def old_executor(_):
        runtime.cancel("G1", user.sign("user.stop", base.fingerprint,
                       {"goal_id": "G1", "reason": "cancel old run"}, at=NOW))
        runtime.start_baseline(new, user.sign("user.baseline", new.fingerprint, new.to_dict(), at=NOW))
        return {"exit_code": 0, "object_ref": "artifact"}
    assert runtime.execute("O1", "old", old_executor)["state"] == "SUCCEEDED"
    current = runtime.store.get("goal", "G1")
    assert current["baseline_digest"] == new.fingerprint and current["state"] == "PLAN"
    assert runtime.store.list("observation", "G1") == ()
    with pytest.raises(ContractValidationError):
        runtime.close("G1")


def test_t42_accepted_risk_does_not_replace_missing_acceptance_observation(tmp_path):
    runtime, base, user, _ = accepted_risk(tmp_path)
    approve(runtime, operation(tmp_path, base), user)
    runtime.execute("O1", "done", lambda _: {"exit_code": 0, "object_ref": "artifact"})
    with pytest.raises(ContractValidationError):
        runtime.close("G1")
    assert runtime.store.list("observation", "G1") == ()


def test_t43_closed_chain_has_exportable_original_signed_events(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    runtime.execute("O1", "done", lambda _: {"exit_code": 0, "object_ref": "artifact"})
    observation = {"goal_id": "G1", "baseline_digest": base.fingerprint, "criterion_id": "C1",
                   "object_ref": "artifact", "actual": "done", "attempt_id": "done"}
    runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, observation, at=NOW))
    assert runtime.close("G1")["state"] == "CLOSED"
    exported = json.loads(json.dumps(runtime.store.audit()))
    kinds = {event["payload"].get("kind") for event in exported}
    assert {"user.baseline", "observer.evidence", "observer.review", "user.answer",
            "observer.observation", "controller.close"} <= kinds
    for record in exported:
        event = runtime.store.get_event(record["event_id"])
        assert event == record["payload"]


def test_t53_dynamic_permission_expiry_does_not_corrupt_static_snapshot(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    static = deepcopy(runtime.store.get("evidence", "G1:E1"))
    dynamic = EvidenceSnapshot.capture("PERMISSION", "account-permission", b"allowed", source="permission-query",
                                       collected_at=NOW, expires_at="2026-10-16T01:01:00+08:00")
    runtime.add_evidence("G1", dynamic, observer.sign("observer.evidence", dynamic.fingerprint,
                         {"goal_id": "G1", "snapshot": dynamic.to_dict()}, at=NOW))
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    runtime.clock = lambda: "2026-10-16T01:02:00+08:00"
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "expired-permission", lambda _: pytest.fail("permission needs current proof"))
    assert runtime.store.get("evidence", "G1:E1") == static
    assert EvidenceSnapshot.from_dict(static["snapshot"]).valid_at(runtime.clock())
