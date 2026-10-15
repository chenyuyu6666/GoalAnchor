"""Proposed minimal gap contracts. RED until the reviewed APIs are implemented.

Fixtures describe trusted controller observations, not native host verification.
"""
from copy import deepcopy
from dataclasses import replace

import pytest
from test_workflow import NOW, approve, operation, review, setup_workflow

from goalanchor.core import ContractValidationError
from goalanchor.workflow import Workflow, WorkflowPolicy
from goalanchor.workflow_protocol import canonical, fingerprint


def test_t44_signed_presentation_only_update_preserves_review_and_grant(tmp_path):
    runtime, base, user, observer = setup_workflow(tmp_path)
    before_review = review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    before_grants = runtime.store.list("grant", "G1")
    proof = {"goal_id": "G1", "baseline_digest": base.fingerprint,
             "context_fingerprint": runtime.review_context("G1").fingerprint,
             "presentation": {"title": "A clearer display title"},
             "no_impact_reason": "Only display title changes; candidate and frozen dependencies unchanged"}
    event = observer.sign("observer.presentation", base.fingerprint, proof, at=NOW)
    runtime.update_presentation("G1", proof, event)
    assert runtime.store.get("review", "G1") == before_review
    assert runtime.store.list("grant", "G1") == before_grants
    assert runtime.review_context("G1").fingerprint == proof["context_fingerprint"]
    assert runtime.store.get_event(event.event_id) == event.to_dict()
    assert runtime.execute("O1", "display-only", lambda _: {"exit_code": 0})["state"] == "SUCCEEDED"


def test_t44_no_impact_claim_cannot_hide_changed_direct_dependency(tmp_path):
    from goalanchor.workflow_protocol import EvidenceSnapshot
    runtime, base, _, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    proof = {"goal_id": "G1", "baseline_digest": base.fingerprint,
             "context_fingerprint": runtime.review_context("G1").fingerprint,
             "presentation": {"title": "display"}, "no_impact_reason": "claim unchanged"}
    snapshot = EvidenceSnapshot.capture("E2", "direct-dependency", b"new fact", source="runner", collected_at=NOW)
    runtime.add_evidence("G1", snapshot, observer.sign("observer.evidence", snapshot.fingerprint,
                         {"goal_id": "G1", "snapshot": snapshot.to_dict()}, at=NOW))
    with pytest.raises(ContractValidationError):
        runtime.update_presentation(
            "G1", proof, observer.sign("observer.presentation", base.fingerprint, proof, at=NOW)
        )


@pytest.mark.parametrize("missing", ["source_event_id", "source_question_id", "review_digest"])
def test_t45_legacy_grant_missing_proof_is_explicitly_unverified(tmp_path, missing):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    original = runtime.store.list("grant", "G1")[0]
    legacy = deepcopy(original)
    legacy.pop(missing)
    key = original["authorization_id"]
    runtime.store.commit([("grant", key, legacy)], {("grant", key): fingerprint(original)},
                         "legacy-import", {"kind": "test-import"})
    calls = []
    with pytest.raises(ContractValidationError, match="LEGACY_UNVERIFIED"):
        runtime.execute("O1", "legacy-attempt", lambda _: calls.append(1))
    assert calls == []
    assert runtime.store.list("receipt", "G1") == ()
    # Rejection does not fill in missing trust fields or rewrite historical input.
    assert runtime.store.get("grant", key) == legacy


def host_binding(**changes):
    # These are controller facts derived from initialize + thread/start + local
    # hook configuration. Model is not an initialize response field.
    facts = {"host": "codex", "user_agent": "codex_cli_rs/0.100.0",
             "model": "test-model", "mode": "approval-controlled",
             "hook_enabled": True, "hook_trust": "controller-owned"}
    facts.update(changes)
    return canonical(facts)


@pytest.mark.parametrize("change", [{"user_agent": "codex_cli_rs/0.101.0"},
                                    {"model": "other-model"}, {"mode": "advisory"},
                                    {"hook_enabled": False}, {"hook_trust": "untrusted"}])
def test_t49_host_change_invalidates_old_baseline_and_authority(tmp_path, change):
    original, old_base, user, observer = setup_workflow(tmp_path)
    policy = WorkflowPolicy(str(tmp_path), ("write_text",), host_binding=host_binding())
    runtime = Workflow(original.store, user, observer, policy, clock=lambda: NOW)
    base = replace(old_base, revision=2)
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict(), at=NOW))
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    runtime.policy = replace(policy, host_binding=host_binding(**change))
    assert runtime.policy.fingerprint != policy.fingerprint
    calls = []
    with pytest.raises(ContractValidationError, match="policy changed"):
        runtime.execute("O1", "changed-host", lambda _: calls.append(1))
    assert calls == []


def test_t49_controller_cannot_dispatch_with_unmatched_host_binding(tmp_path):
    from test_codex_controller import Client

    from goalanchor.codex_controller import CodexController
    original, old_base, user, observer = setup_workflow(tmp_path)
    policy = WorkflowPolicy(str(tmp_path), ("codex.command",), host_binding=host_binding())
    runtime = Workflow(original.store, user, observer, policy, clock=lambda: NOW)
    base = replace(old_base, revision=2)
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict(), at=NOW))
    review(runtime, observer)
    params = {"threadId": "T", "turnId": "R", "itemId": "I", "command": "echo hello", "cwd": str(tmp_path)}
    op = replace(operation(tmp_path, base), tool="codex.command", arguments_json=canonical(params))
    approve(runtime, op, user)
    client = Client()
    controller = CodexController(client, "T", "R", host_binding=host_binding(user_agent="changed-version"))
    controller.command({"id": 1, "method": "item/commandExecution/requestApproval", "params": params})
    with pytest.raises(ContractValidationError, match="host binding"):
        controller.dispatch(1, runtime, "O1", "mismatch")
    assert client.responses == []
    assert runtime.store.list("receipt", "G1") == ()


def presentation_proof(runtime, base):
    return {"goal_id": "G1", "baseline_digest": base.fingerprint,
            "context_fingerprint": runtime.review_context("G1").fingerprint,
            "presentation": {"title": "Display title"}, "no_impact_reason": "Display title only"}


def test_t44_forged_presentation_event_is_rejected_without_mutation(tmp_path):
    from goalanchor.workflow_protocol import EventAuthority
    runtime, base, _, _ = setup_workflow(tmp_path)
    proof = presentation_proof(runtime, base)
    forged = EventAuthority(b"x" * 32, "trusted-runner")
    event = forged.sign("observer.presentation", base.fingerprint, proof, at=NOW)
    before = runtime.store.audit()
    with pytest.raises(ContractValidationError):
        runtime.update_presentation("G1", proof, event)
    assert runtime.store.get("presentation", "G1") is None
    assert runtime.store.audit() == before


@pytest.mark.parametrize("presentation", [{"title": "display", "plan": "new hidden plan"},
                                          {"title": "display", "allowed_tools": ["forbidden"]},
                                          {"description": "unsupported field"}])
def test_t44_unsupported_presentation_keys_cannot_change_dependencies(tmp_path, presentation):
    runtime, base, _, observer = setup_workflow(tmp_path)
    proof = {**presentation_proof(runtime, base), "presentation": presentation}
    with pytest.raises(ContractValidationError):
        runtime.update_presentation(
            "G1", proof, observer.sign("observer.presentation", base.fingerprint, proof, at=NOW)
        )
    assert runtime.store.get("presentation", "G1") is None
    assert runtime.store.get("goal", "G1")["baseline_digest"] == base.fingerprint


def test_t44_presentation_event_replay_is_idempotent(tmp_path):
    runtime, base, _, observer = setup_workflow(tmp_path)
    proof = presentation_proof(runtime, base)
    event = observer.sign("observer.presentation", base.fingerprint, proof, at=NOW)
    first = runtime.update_presentation("G1", proof, event)
    audit = runtime.store.audit()
    assert runtime.update_presentation("G1", proof, event) == first
    assert runtime.store.audit() == audit


def test_t44_dependency_change_between_proof_check_and_commit_rejects_reuse(tmp_path, monkeypatch):
    from goalanchor.workflow_protocol import EvidenceSnapshot

    runtime, base, _, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    proof = presentation_proof(runtime, base)
    event = observer.sign("observer.presentation", base.fingerprint, proof, at=NOW)
    original_commit = runtime.store.commit

    def changed_dependency(changes, expected, event_id, payload, **kwargs):
        if any(kind == "presentation" for kind, _, _ in changes):
            snapshot = EvidenceSnapshot.capture(
                "E2", "new-facts", b"new direct facts", source="runner", collected_at=NOW
            )
            runtime.add_evidence(
                "G1", snapshot, observer.sign("observer.evidence", snapshot.fingerprint,
                                               {"goal_id": "G1", "snapshot": snapshot.to_dict()}, at=NOW)
            )
        return original_commit(changes, expected, event_id, payload, **kwargs)

    monkeypatch.setattr(runtime.store, "commit", changed_dependency)
    with pytest.raises(ContractValidationError):
        runtime.update_presentation("G1", proof, event)
    assert runtime.store.get("presentation", "G1") is None
    assert runtime.store.get_event(event.event_id) is None


@pytest.mark.parametrize("binding", ["matching", "missing"])
def test_t49_controller_matching_or_missing_binding_is_dispatch_bound(tmp_path, binding):
    from test_codex_controller import Client

    from goalanchor.codex_controller import CodexController
    original, old_base, user, observer = setup_workflow(tmp_path)
    policy = WorkflowPolicy(str(tmp_path), ("codex.command",), host_binding=host_binding())
    runtime = Workflow(original.store, user, observer, policy, clock=lambda: NOW)
    base = replace(old_base, revision=2)
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict(), at=NOW))
    review(runtime, observer)
    params = {"threadId": "T", "turnId": "R", "itemId": "I", "command": "echo hello", "cwd": str(tmp_path)}
    approve(runtime, replace(operation(tmp_path, base), tool="codex.command", arguments_json=canonical(params)), user)
    client = Client()
    controller = CodexController(client, "T", "R", host_binding=host_binding() if binding == "matching" else None)
    controller.command({"id": 1, "method": "item/commandExecution/requestApproval", "params": params})
    if binding == "matching":
        assert controller.dispatch(1, runtime, "O1", "matched")["state"] == "SUCCEEDED"
        assert client.responses == [(1, {"decision": "accept"})]
    else:
        with pytest.raises(ContractValidationError, match="host binding"):
            controller.dispatch(1, runtime, "O1", "missing")
        assert client.responses == []
        assert runtime.store.list("receipt", "G1") == ()


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", "null", "1",
                                  host_binding(hook_enabled="true"), host_binding(model=" "),
                                  host_binding(host="unknown-host"), host_binding(host=[]),
                                  host_binding(extra_required="unsupported")])
def test_t49_malformed_host_binding_is_a_contract_error(tmp_path, raw):
    from test_codex_controller import Client

    from goalanchor.codex_controller import CodexController
    with pytest.raises(ContractValidationError):
        WorkflowPolicy(str(tmp_path), ("write_text",), host_binding=raw)
    with pytest.raises(ContractValidationError):
        CodexController(Client(), "T", "R", host_binding=raw)
