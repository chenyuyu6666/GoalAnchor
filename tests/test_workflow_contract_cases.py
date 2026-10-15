"""Exact core contract probes; local controlled executors are not native-host evidence."""
import hashlib
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest
from test_workflow import NOW, approve, operation, review, setup_workflow, synthetic_delivery

from goalanchor.core import ContractValidationError
from goalanchor.integration import ReviewFinding
from goalanchor.workflow_protocol import EvidenceSnapshot, canonical
from goalanchor.workflow_store import WorkflowStore


def second_round(record):
    bundle = deepcopy(record["history"][0])
    bundle["round_number"] = 2
    for report in bundle["reviewer_results"] + [bundle["arbiter"], bundle["meta_review"]]:
        report["round_number"] = 2
    for binding in bundle["bindings"]:
        binding["session_id"] += "-independent-second"
    return bundle


def test_t12_t55_t56_refutation_requires_independent_second_round(tmp_path):
    runtime, _, _, observer = setup_workflow(tmp_path)
    finding = ReviewFinding("F1", "important objection", ("E1",), "blocks", "claim")
    first = review(runtime, observer, finding=finding,
                   dispositions={"F1": {"status": "REFUTED", "evidence_refs": ["E1"]}})
    assert first["status"] == "REVIEW_REQUIRED"
    assert first["unresolved"] == ["F1"]
    bundle = second_round(first)
    ctx = runtime.review_context("G1")
    synthetic_delivery(runtime, bundle)
    final = runtime.submit_review("G1", bundle, observer.sign("observer.review", ctx.fingerprint, bundle, at=NOW))
    assert final["status"] == "PASSED"
    assert final["unresolved"] == []
    assert len(final["history"]) == 2
    assert final["history"][0] == first["history"][0]
    bundle["round_number"] = 3
    with pytest.raises(ContractValidationError):
        runtime.submit_review("G1", bundle, observer.sign("observer.review", ctx.fingerprint, bundle, at=NOW))


@pytest.mark.parametrize("change", ["arguments", "cwd", "account", "resource"])
def test_t26_approved_operation_mutation_never_dispatches(tmp_path, change):
    runtime, base, user, observer = setup_workflow(tmp_path)
    resource = tmp_path / "input.txt"
    resource.write_bytes(b"original")
    op = replace(operation(tmp_path, base),
                 resource_preconditions=(("input.txt", hashlib.sha256(b"original").hexdigest()),))
    review(runtime, observer)
    approve(runtime, op, user)
    calls = []
    if change == "resource":
        resource.write_bytes(b"changed")
    else:
        child = tmp_path / "child"
        child.mkdir()
        (child / "input.txt").write_bytes(b"original")
        changes = {"arguments": {"arguments_json": canonical({"text": "other"})},
                   "cwd": {"cwd": str(child)}, "account": {"account": "other-account"}}
        altered = replace(op, **changes[change])
        with pytest.raises(ContractValidationError):
            runtime.register_operation(altered)
        # A different ID cannot borrow the old approval either.
        runtime.register_operation(replace(altered, operation_id="O2"))
    with pytest.raises(ContractValidationError):
        runtime.execute("O1" if change == "resource" else "O2", "changed", lambda _: calls.append(1))
    assert calls == []


@pytest.mark.parametrize("stage", ["reserve", "consume"])
def test_t29_durable_write_failure_prevents_dispatch(tmp_path, monkeypatch, stage):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    calls = []
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("injected disk write failure")
    monkeypatch.setattr(runtime.store, stage, broken)
    with pytest.raises(sqlite3.OperationalError):
        runtime.execute("O1", "disk-failure", lambda _: calls.append(1))
    assert calls == []
    receipt = runtime.store.get("receipt", "disk-failure")
    assert receipt is None or not receipt["dispatch_right"]


@pytest.mark.parametrize("crash", ["before-send", "after-effect"])
def test_t32_t33_crash_recovery_is_unknown_and_never_resends(tmp_path, monkeypatch, crash):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    calls = []
    finish = runtime.store.finish
    def interrupted(attempt, state, result, at):
        if state == ("RUNNING" if crash == "before-send" else "SUCCEEDED"):
            raise KeyboardInterrupt("simulated process death")
        return finish(attempt, state, result, at)
    monkeypatch.setattr(runtime.store, "finish", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runtime.execute("O1", "crashed", lambda _: calls.append(1) or {"exit_code": 0})
    runtime.store = WorkflowStore(tmp_path / "state.db")
    assert runtime.store.recover(at=NOW)[0]["state"] == "UNKNOWN"
    assert runtime.execute("O1", "crashed", lambda _: pytest.fail("must never resend"))["state"] == "UNKNOWN"
    assert calls == ([] if crash == "before-send" else [1])


def test_t35_partial_effect_is_failed_and_preserved(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    artifact = tmp_path / "partial.txt"
    def partial(_):
        artifact.write_text("partial", encoding="utf-8")
        return {"exit_code": 1, "residual": str(artifact), "rollback": "NOT_ATTEMPTED"}
    receipt = runtime.execute("O1", "partial", partial)
    assert receipt["state"] == "FAILED"
    assert receipt["result"]["residual"] == str(artifact)
    assert artifact.read_text(encoding="utf-8") == "partial"
    assert runtime.execute("O1", "partial", lambda _: pytest.fail("no automatic compensation")) == receipt
    with pytest.raises(ContractValidationError):
        runtime.close("G1")


def test_t05_digest_without_frozen_content_is_rejected(tmp_path):
    runtime, _, _, observer = setup_workflow(tmp_path)
    data = EvidenceSnapshot.capture("missing", "unavailable/path", b"facts",
                                    source="runner", collected_at=NOW).to_dict()
    data.pop("content_base64")
    with pytest.raises(ContractValidationError):
        EvidenceSnapshot.from_dict(data)
    assert [item["snapshot"]["evidence_id"] for item in runtime.store.list("evidence", "G1")] == ["E1"]


@pytest.mark.parametrize("source", ["missing", "other-goal"])
def test_t02_review_source_must_exist_and_match_goal(tmp_path, source):
    runtime, base, user, observer = setup_workflow(tmp_path)
    valid = review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    altered = deepcopy(valid)
    if source == "missing":
        altered["source_event_id"] = "does-not-exist"
    else:
        altered["goal_id"] = "G-other"
        altered["baseline_digest"] = "0" * 64
    runtime.store.commit([("review", "G1", altered)], {("review", "G1"): runtime.store.fingerprint(valid)},
                         "imported-review", {"source": source})
    calls = []
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "bad-review", lambda _: calls.append(1))
    assert calls == []


def test_t13_meta_without_original_context_cannot_pass(tmp_path):
    runtime, _, _, observer = setup_workflow(tmp_path)
    first = review(runtime, observer)
    bundle = second_round(first)
    bundle["meta_review"]["context_fingerprint"] = "summary-only-context"
    ctx = runtime.review_context("G1")
    with pytest.raises(ContractValidationError):
        runtime.submit_review("G1", bundle, observer.sign("observer.review", ctx.fingerprint, bundle, at=NOW))
    assert runtime.store.get("review", "G1") == first


def test_t15_new_review_identity_cannot_restart_stopped_dispute(tmp_path):
    runtime, base, _, observer = setup_workflow(tmp_path)
    finding = ReviewFinding("F1", "still disputed", ("E1",), "blocks", "unresolved")
    first = review(runtime, observer, finding=finding)
    bundle = second_round(first)
    ctx = runtime.review_context("G1")
    synthetic_delivery(runtime, bundle)
    stopped = runtime.submit_review("G1", bundle, observer.sign("observer.review", ctx.fingerprint, bundle, at=NOW))
    assert stopped["status"] == "STOPPED"
    restarted = deepcopy(first["history"][0])
    for binding in restarted["bindings"]:
        binding["session_id"] = "new-run-" + binding["session_id"]
    # A fresh observer event and session IDs do not create a fresh authorized baseline.
    with pytest.raises(ContractValidationError):
        runtime.submit_review("G1", restarted,
                              observer.sign("observer.review", ctx.fingerprint, restarted, at=NOW))
    with pytest.raises(ContractValidationError):
        runtime.register_operation(operation(tmp_path, base))
    assert runtime.store.get("review", "G1") == stopped
    assert runtime.store.list("receipt", "G1") == ()


def test_t57_plan_allows_semantic_acceptance_before_threshold_fixing():
    from test_gates import make_contract, make_item

    from goalanchor import StructureReview, evaluate_plan
    assert evaluate_plan(make_contract(acceptance_criteria=("acceptable latency",)),
                         (make_item(),), StructureReview()).passed


def test_t58_verify_rejects_unfixed_scope_and_unbounded_exit():
    from goalanchor import VerifyRecord, check_verify
    assert not check_verify(None).passed
    assert not check_verify(VerifyRecord("scope", "decision", "eventually enough", finite=False)).passed
    assert check_verify(VerifyRecord("one artifact", "accept artifact", "one content comparison")).passed


def test_t50_missing_usage_and_truncated_output_never_create_review_pass(tmp_path):
    from goalanchor.agent_runtime import AgentRuntimeError
    from goalanchor.platforms.platform_parse import _extract_tokens_used, extract_result_payload
    assert _extract_tokens_used("") is None
    assert _extract_tokens_used("tool timed out before reporting usage") is None
    with pytest.raises(AgentRuntimeError):
        extract_result_payload('{"verdict":"PASS", "context_fingerprint":')
    runtime, base, user, _ = setup_workflow(tmp_path)
    runtime.register_operation(operation(tmp_path, base))
    with pytest.raises(ContractValidationError):
        runtime.execute("O1", "missing-output", lambda _: pytest.fail("missing result must never dispatch"))
    assert runtime.store.get("review", "G1") is None
