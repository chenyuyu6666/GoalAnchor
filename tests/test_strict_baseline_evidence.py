from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import goalanchor
import goalanchor.workflow_store_strict as strict_store_module
import goalanchor.workflow_strict as strict_workflow_module
from goalanchor import workflow as workflow_module
from goalanchor.core import ContractValidationError
from goalanchor.questions import Question, QuestionAnswer, QuestionOption, QuestionRecord
from goalanchor.review_service import ReviewContext
from goalanchor.workflow import StrictWorkflow, StrictWorkflowError, Workflow, WorkflowPolicy
from goalanchor.workflow_protocol import (
    Baseline,
    Criterion,
    EventAuthority,
    EvidenceSnapshot,
    Operation,
    SignedEvent,
    canonical,
    fingerprint,
)
from goalanchor.workflow_store import WorkflowStore, WorkflowStoreConflict

NOW = "2026-10-16T01:00:00+08:00"
LATER = "2026-10-17T01:00:00+08:00"
HOST_BINDING_FACTS = {
    "host": "codex",
    "user_agent": "Codex Desktop/0.163.0",
    "model": "model-main",
    "mode": "app-server",
    "hook_enabled": False,
    "hook_trust": "none",
}
HOST_BINDING = canonical(HOST_BINDING_FACTS)


class FakeRuntimeBindingProvider:
    """Test-only host adapter with an exclusive identity lease."""

    def __init__(self, policy: WorkflowPolicy, host_binding: str = HOST_BINDING) -> None:
        self.policy = policy
        self._lock = threading.RLock()
        self._value: dict[str, Any] = {
            "host_binding": host_binding,
            "principal_id": "user-1",
            "account_id": "account-1",
            "session_id": "session-1",
            "generation": 1,
        }
        self.read_count = 0
        self.lease_count = 0
        self.available = True
        self.after_read: Callable[[dict[str, Any]], None] | None = None

    def current_runtime_binding(self) -> dict[str, Any]:
        if not self.available:
            unavailable = getattr(workflow_module, "TrustedBindingUnavailable", ConnectionError)
            raise unavailable("test runtime binding unavailable")
        with self._lock:
            self.read_count += 1
            value = dict(self._value)
            callback = self.after_read
        if callback is not None:
            callback(value)
        return value

    @contextmanager
    def acquire_lease(self, expected_digest: str):
        if not self.available:
            unavailable = getattr(workflow_module, "TrustedBindingUnavailable", ConnectionError)
            raise unavailable("test runtime binding unavailable")
        with self._lock:
            value = dict(self._value)
            actual = runtime_digest(value, self.policy)
            if actual != expected_digest:
                stale = getattr(workflow_module, "TrustedBindingStale", RuntimeError)
                raise stale("runtime binding changed")
            self.lease_count += 1
            yield {**value, "binding_digest": actual}

    def rotate(self, **changes: Any) -> None:
        with self._lock:
            self._value.update(changes)
            if "generation" not in changes:
                self._value["generation"] += 1


class FakeTrustedEventVerifier:
    """Returns proof only for an exact event, purpose, and binding registered by the test."""

    def __init__(self) -> None:
        self.registered: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.failure: BaseException | None = None
        self.on_verify: Callable[[SignedEvent, str, dict[str, Any]], None] | None = None
        self.refresh_responses = False
        self.refresh_expires_at = "2026-10-18T01:00:00+08:00"

    def register(
        self,
        event: SignedEvent,
        *,
        purpose: str,
        binding: dict[str, Any],
        actor_type: str,
        principal_id: str,
        account_id: str,
        session_id: str,
        expires_at: str = LATER,
    ) -> None:
        proof = {
            "actor_type": actor_type,
            "principal_id": principal_id,
            "account_id": account_id,
            "session_id": session_id,
            "source_event_id": event.event_id,
            "purpose": purpose,
            "event_kind": event.kind,
            "subject": event.subject,
            "binding_digest": fingerprint(binding),
            "expires_at": expires_at,
            "nonce": "nonce-" + event.event_id,
        }
        proof["proof_digest"] = fingerprint(proof)
        self.registered[(event.event_id, purpose)] = (canonical(binding), proof)

    def verify(self, event: SignedEvent | dict[str, Any], *, purpose: str, binding: dict[str, Any]):
        normalized = event if isinstance(event, SignedEvent) else SignedEvent.from_dict(event)
        self.calls.append((normalized.event_id, purpose, dict(binding)))
        if self.failure is not None:
            raise self.failure
        registered = self.registered.get((normalized.event_id, purpose))
        if registered is None or registered[0] != canonical(binding):
            return None
        if self.on_verify is not None:
            self.on_verify(normalized, purpose, dict(binding))
        response = dict(registered[1])
        if self.refresh_responses:
            response.pop("proof_digest")
            response["nonce"] += f"-request-{len(self.calls)}"
            response["expires_at"] = self.refresh_expires_at
            response["proof_digest"] = fingerprint(response)
        return response


def runtime_digest(value: dict[str, Any], policy: WorkflowPolicy) -> str:
    return fingerprint(
        {
            "policy_digest": policy.fingerprint,
            "host_binding": json.loads(value["host_binding"]),
            "principal_id": value["principal_id"],
            "account_id": value["account_id"],
            "session_id": value["session_id"],
            "generation": value["generation"],
        }
    )


def baseline_binding(runtime: StrictWorkflow, baseline: Baseline) -> dict[str, Any]:
    current = runtime.runtime_binding_provider.current_runtime_binding
    value = current()
    return {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "runtime_binding_digest": runtime_digest(value, runtime.policy),
        "plan_revision": baseline.revision,
        "objective": baseline.objective,
        "criteria_digest": fingerprint([item.to_dict() for item in baseline.criteria]),
    }


def evidence_binding(runtime: StrictWorkflow, baseline: Baseline, snapshot: EvidenceSnapshot) -> dict[str, Any]:
    value = runtime.runtime_binding_provider.current_runtime_binding()
    return {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "runtime_binding_digest": runtime_digest(value, runtime.policy),
        "evidence_id": snapshot.evidence_id,
        "evidence_digest": snapshot.sha256,
        "snapshot_fingerprint": snapshot.fingerprint,
    }


def make_world(
    tmp_path, *, clock=None, event_verifier=None, runtime_provider=None, allowed_tools=("write_text",),
    operation_path_schemas=None, question_ttl_seconds=None,
):
    user = EventAuthority(b"u" * 32, "trusted-ui")
    observer = EventAuthority(b"o" * 32, "trusted-runner")
    policy = WorkflowPolicy(str(tmp_path), allowed_tools, host_binding=HOST_BINDING)
    runtime_provider = runtime_provider or FakeRuntimeBindingProvider(policy)
    event_verifier = event_verifier or FakeTrustedEventVerifier()
    clock_value = clock or (lambda: NOW)
    store = WorkflowStore(tmp_path / "state.db")
    runtime_options = {}
    if operation_path_schemas is not None:
        runtime_options["operation_path_schemas"] = operation_path_schemas
    if question_ttl_seconds is not None:
        runtime_options["question_ttl_seconds"] = question_ttl_seconds
    workflow = StrictWorkflow(
        store,
        user,
        observer,
        policy,
        clock=clock_value,
        trusted_event_verifier=event_verifier,
        trusted_runtime_binding_provider=runtime_provider,
        **runtime_options,
    )
    baseline = Baseline(
        "goal-1",
        1,
        "Deliver verified artifact",
        "Write the artifact and verify its content",
        (Criterion("artifact", "artifact exists", canonical("done"), "artifact.txt"),),
    )
    return workflow, baseline, user, observer, event_verifier, runtime_provider


def direction_question(
    baseline: Baseline,
    *,
    question_id: str = "question-1",
    decision_key: str = "clarify-delivery",
    blocking_items: tuple[str, ...] = ("item-1",),
    **changes: Any,
) -> Question:
    values: dict[str, Any] = {
        "question_id": question_id,
        "goal_id": baseline.goal_id,
        "plan_revision": baseline.revision,
        "title": "Confirm delivery choice",
        "prompt": "Which supported delivery path should this plan use?",
        "options": (QuestionOption("direct", "Direct delivery"), QuestionOption("staged", "Staged delivery")),
        "multi_select": False,
        "allow_custom": False,
        "blocking_items": blocking_items,
        "kind": "DIRECTION",
        "baseline_digest": baseline.fingerprint,
        "decision_key": decision_key,
        "trigger_reason": "The approved plan permits two incompatible delivery paths.",
        "expires_at": "2026-10-16T02:00:00+08:00",
    }
    values.update(changes)
    return Question(**values)


def assert_question_decision_fails_closed(operation) -> None:
    try:
        result = operation()
    except StrictWorkflowError as exc:
        assert exc.code in {"UNTRUSTED", "STALE"}
    else:
        assert result == "WAIT"


def assert_untrusted_question_decision(operation) -> None:
    with pytest.raises(StrictWorkflowError) as captured:
        operation()
    assert captured.value.code == "UNTRUSTED"


def baseline_event(runtime: StrictWorkflow, baseline: Baseline, *, event_id: str = "baseline-event") -> SignedEvent:
    return runtime.user_authority.sign(
        "user.baseline", baseline.fingerprint, baseline.to_dict(), event_id=event_id, at=NOW
    )


def register_baseline(runtime, baseline, event, provider, *, principal_id=None, expires_at=LATER):
    binding = baseline_binding(runtime, baseline)
    current = runtime.runtime_binding_provider.current_runtime_binding()
    provider.register(
        event,
        purpose="user.baseline",
        binding=binding,
        actor_type="USER",
        principal_id=principal_id or current["principal_id"],
        account_id=current["account_id"],
        session_id=current["session_id"],
        expires_at=expires_at,
    )


def register_evidence(runtime, baseline, snapshot, event, provider, *, expires_at=LATER):
    binding = evidence_binding(runtime, baseline, snapshot)
    current = runtime.runtime_binding_provider.current_runtime_binding()
    provider.register(
        event,
        purpose="observer.evidence",
        binding=binding,
        actor_type="SERVICE",
        principal_id="observer-service",
        account_id=current["account_id"],
        session_id=current["session_id"],
        expires_at=expires_at,
    )


def write_baseline(runtime, baseline, event, provider):
    register_baseline(runtime, baseline, event, provider)
    return runtime.start_baseline(baseline, event)


def write_evidence(
    runtime, baseline, observer, provider, *, evidence_id="evidence-1", snapshot_expires_at=None,
    proof_expires_at=LATER,
):
    snapshot = EvidenceSnapshot.capture(
        evidence_id, "artifact.txt", b"done", source="trusted-runner", collected_at=NOW,
        expires_at=snapshot_expires_at,
    )
    event = observer.sign(
        "observer.evidence",
        snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()},
        event_id="evidence-event-" + evidence_id + "-" + snapshot.fingerprint[:8],
        at=NOW,
    )
    register_evidence(runtime, baseline, snapshot, event, provider, expires_at=proof_expires_at)
    runtime.add_evidence(baseline.goal_id, snapshot, event)
    return snapshot, event


def track_evidence_projection_validations(monkeypatch):
    calls: list[str] = []
    original = strict_store_module.WorkflowStore._validate_evidence_projection_record

    def tracked(
        db, goal_id, key, projection, *, strict_namespace, legacy_key_identities=None,
    ):
        calls.append(key)
        return original(
            db,
            goal_id,
            key,
            projection,
            strict_namespace=strict_namespace,
            legacy_key_identities=legacy_key_identities,
        )

    monkeypatch.setattr(
        strict_store_module.WorkflowStore,
        "_validate_evidence_projection_record",
        staticmethod(tracked),
    )
    return calls


def assert_strict_code(operation, expected: str) -> None:
    with pytest.raises(StrictWorkflowError) as captured:
        operation()
    assert captured.value.code == expected, str(captured.value)


def test_strict_runtime_binding_contract_is_public():
    assert goalanchor.StrictWorkflow is StrictWorkflow
    assert goalanchor.TrustedRuntimeBindingProvider is workflow_module.TrustedRuntimeBindingProvider
    assert goalanchor.TrustedBindingStale is workflow_module.TrustedBindingStale


def generic_snapshot(runtime: StrictWorkflow):
    return (
        runtime.store.list("goal"),
        runtime.store.list("baseline"),
        runtime.store.list("evidence"),
        runtime.store.list("strict_evidence"),
        runtime.store.audit(),
    )


def strict_storage_rows(runtime: StrictWorkflow):
    with sqlite3.connect(runtime.store.path) as db:
        return {
            "records": db.execute(
                "SELECT kind,key,payload FROM records ORDER BY kind,key"
            ).fetchall(),
            "events": db.execute(
                "SELECT seq,event_id,payload FROM events ORDER BY seq"
            ).fetchall(),
            "strict_proofs": db.execute(
                "SELECT * FROM strict_proofs ORDER BY proof_id"
            ).fetchall(),
            "strict_heads": db.execute(
                "SELECT * FROM strict_heads ORDER BY kind,goal_id,scope_digest,subject_id"
            ).fetchall(),
        }


def test_strict_baseline_requires_registered_source_proof_without_writes(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    before = generic_snapshot(runtime)
    provider.registered.clear()

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "UNTRUSTED")

    assert generic_snapshot(runtime) == before


def test_missing_trusted_event_provider_is_unavailable_without_writes(tmp_path):
    user = EventAuthority(b"u" * 32, "trusted-ui")
    observer = EventAuthority(b"o" * 32, "trusted-runner")
    policy = WorkflowPolicy(str(tmp_path), ("write_text",), host_binding=HOST_BINDING)
    runtime_provider = FakeRuntimeBindingProvider(policy)
    store = WorkflowStore(tmp_path / "missing-provider.db")
    runtime = StrictWorkflow(
        store,
        user,
        observer,
        policy,
        clock=lambda: NOW,
        trusted_runtime_binding_provider=runtime_provider,
    )
    baseline = Baseline(
        "goal-1", 1, "Deliver verified artifact", "Write then verify",
        (Criterion("artifact", "exists", canonical("done"), "artifact.txt"),),
    )
    before = generic_snapshot(runtime)

    assert_strict_code(
        lambda: runtime.start_baseline(baseline, baseline_event(runtime, baseline)), "UNAVAILABLE"
    )

    assert generic_snapshot(runtime) == before


@pytest.mark.parametrize("failure", [ConnectionError("offline"), TimeoutError("timeout"), RuntimeError("bug")])
def test_event_provider_service_failures_are_unavailable_and_write_nothing(tmp_path, failure):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider)
    provider.failure = failure
    before = generic_snapshot(runtime)

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "UNAVAILABLE")

    assert generic_snapshot(runtime) == before


def test_malformed_event_provider_proof_is_untrusted_and_write_nothing(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider)
    registered = provider.registered[(event.event_id, "user.baseline")][1]
    registered["proof_digest"] = "not-a-digest"
    before = generic_snapshot(runtime)

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "UNTRUSTED")

    assert generic_snapshot(runtime) == before


def test_expired_event_provider_proof_is_stale_without_writes(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(
        runtime, baseline, event, provider, expires_at="2026-10-16T01:00:00+08:00"
    )
    before = generic_snapshot(runtime)

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "STALE")

    assert generic_snapshot(runtime) == before


def test_runtime_binding_change_during_event_verification_is_stale(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider)
    provider.on_verify = lambda *_: runtime_provider.rotate(session_id="rotated-during-proof")

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "STALE")

    assert runtime.store.get("goal", baseline.goal_id) is None


def test_runtime_host_facts_must_match_the_pinned_policy(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider)
    other_facts = {**HOST_BINDING_FACTS, "model": "different-model"}
    runtime_provider.rotate(host_binding=canonical(other_facts))

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "UNTRUSTED")

    assert provider.calls == []


def test_invalid_local_signature_is_rejected_before_trusted_provider(tmp_path):
    runtime, baseline, user, _, provider, _ = make_world(tmp_path)
    event = user.sign("user.baseline", baseline.fingerprint, baseline.to_dict(), at=NOW)
    invalid = replace(event, signature="0" * 64)
    before = generic_snapshot(runtime)

    assert_strict_code(lambda: runtime.start_baseline(baseline, invalid), "UNTRUSTED")

    assert provider.calls == []
    assert generic_snapshot(runtime) == before


def test_strict_context_uses_bound_proofs_and_empty_evidence_is_not_pass(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)

    context = runtime.review_context(baseline.goal_id)

    assert isinstance(context, ReviewContext)
    assert context.evidence_refs == ()
    assert context.evidence_digests == ()
    assert context.strict_binding["runtime_binding_digest"] == runtime_digest(
        runtime_provider.current_runtime_binding(), runtime.policy
    )
    assert context.strict_binding["goal_generation"] == runtime.store.get("goal", baseline.goal_id)["generation"]
    assert not hasattr(context, "status")
    proof = runtime.store.read_strict_snapshot(baseline.goal_id)["baseline_proof"]
    assert proof["proof_digest"]
    assert proof["principal_id"] == "user-1"
    assert proof["expires_at"] == LATER
    assert proof["source_seq"] > 0
    assert proof["lease_generation"] == 1


def test_exact_baseline_replay_does_not_advance_goal_generation_or_event_history(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    write_baseline(runtime, baseline, event, provider)
    before_goal = runtime.store.get("goal", baseline.goal_id)
    before_events = runtime.store.audit()

    result = runtime.start_baseline(baseline, event)

    assert result["generation"] == before_goal["generation"]
    assert runtime.store.audit() == before_events


def test_concurrent_exact_baseline_requests_append_one_proof(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider)
    provider.refresh_responses = True
    barrier = threading.Barrier(2)
    provider.on_verify = lambda *_: barrier.wait(timeout=3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.start_baseline(baseline, event), range(2)))

    assert [item["generation"] for item in results] == [1, 1]
    assert runtime.store.read_strict_snapshot(baseline.goal_id)["baseline_proof"]["source_event_id"] == event.event_id
    assert [item["event_id"] for item in runtime.store.audit()] == [event.event_id]


def test_baseline_replacement_advances_goal_generation_monotonically(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    old_goal = runtime.store.get("goal", baseline.goal_id)
    next_baseline = Baseline(
        baseline.goal_id, 2, "Revised objective", "Revised plan",
        (Criterion("artifact", "new content", canonical("next"), "artifact.txt"),),
    )
    next_event = baseline_event(runtime, next_baseline, event_id="baseline-event-2")

    updated = write_baseline(runtime, next_baseline, next_event, provider)

    assert updated["generation"] == old_goal["generation"] + 1


def test_baseline_replacement_after_evidence_keeps_historical_evidence_consistent(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    old_evidence, _ = write_evidence(
        runtime, baseline, observer, provider, evidence_id="prior-baseline-evidence"
    )
    old_snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)
    replacement = Baseline(
        baseline.goal_id,
        2,
        "Replacement objective",
        "Replacement plan",
        (Criterion("artifact", "replacement exists", canonical("new"), "artifact.txt"),),
    )
    replacement_event = baseline_event(runtime, replacement, event_id="baseline-after-evidence")
    register_baseline(runtime, replacement, replacement_event, provider)

    updated = runtime.start_baseline(replacement, replacement_event)
    context = runtime.review_context(baseline.goal_id)
    new_snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)

    assert updated["revision"] == 2
    assert new_snapshot["baseline_proof"]["baseline_digest"] == replacement.fingerprint
    assert new_snapshot["evidence_heads"] == []
    assert context.evidence_refs == ()
    old_proof_id = old_snapshot["evidence_proofs"][0]["proof_id"]
    retained_projection = runtime.store.get(
        "strict_evidence", runtime.store.strict_evidence_key(baseline.goal_id, old_evidence.evidence_id)
    )
    assert retained_projection["strict_proof_id"] == old_proof_id
    assert runtime.store.get("evidence", baseline.goal_id + ":" + old_evidence.evidence_id) is None


def test_superseded_baseline_replay_rejects_missing_historical_proof(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    first_event = baseline_event(runtime, baseline, event_id="baseline-history-1")
    write_baseline(runtime, baseline, first_event, provider)
    first_proof_id = runtime.store.read_strict_snapshot(baseline.goal_id)["baseline_proof"]["proof_id"]
    replacement = Baseline(
        baseline.goal_id,
        2,
        "Replacement objective",
        "Replacement plan",
        (Criterion("artifact", "replacement exists", canonical("new"), "artifact.txt"),),
    )
    replacement_event = baseline_event(runtime, replacement, event_id="baseline-history-2")
    write_baseline(runtime, replacement, replacement_event, provider)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TRIGGER strict_proofs_no_delete")
        db.execute("DELETE FROM strict_proofs WHERE proof_id=?", (first_proof_id,))
    with pytest.raises(workflow_module.WorkflowStoreConflict) as captured:
        runtime.store.read_strict_snapshot(baseline.goal_id)
    assert captured.value.code == "UNTRUSTED"
    before_events = runtime.store.audit()

    assert_strict_code(lambda: runtime.start_baseline(baseline, first_event), "UNTRUSTED")

    assert runtime.store.audit() == before_events


def test_superseded_evidence_replay_rejects_missing_historical_proof(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    old_snapshot, old_event = write_evidence(
        runtime, baseline, observer, provider, evidence_id="evidence-history"
    )
    old_proof_id = runtime.store.read_strict_snapshot(baseline.goal_id)["evidence_proofs"][0]["proof_id"]
    replacement = EvidenceSnapshot.capture(
        old_snapshot.evidence_id,
        old_snapshot.object_ref,
        b"replacement evidence",
        source="trusted-runner",
        collected_at=NOW,
    )
    replacement_event = observer.sign(
        "observer.evidence",
        replacement.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": replacement.to_dict()},
        event_id="evidence-history-2",
        at=NOW,
    )
    register_evidence(runtime, baseline, replacement, replacement_event, provider)
    runtime.add_evidence(baseline.goal_id, replacement, replacement_event)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TRIGGER strict_proofs_no_delete")
        db.execute("DELETE FROM strict_proofs WHERE proof_id=?", (old_proof_id,))
    with pytest.raises(workflow_module.WorkflowStoreConflict) as captured:
        runtime.store.read_strict_snapshot(baseline.goal_id)
    assert captured.value.code == "UNTRUSTED"
    before_events = runtime.store.audit()

    assert_strict_code(
        lambda: runtime.add_evidence(baseline.goal_id, old_snapshot, old_event), "UNTRUSTED"
    )

    assert runtime.store.audit() == before_events


def test_strict_context_read_does_not_write_a_delete_journal_database(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    before = hashlib.sha256(Path(runtime.store.path).read_bytes()).hexdigest()

    runtime.review_context(baseline.goal_id)

    after = hashlib.sha256(Path(runtime.store.path).read_bytes()).hexdigest()
    assert before == after
    assert set(item.name for item in tmp_path.iterdir()) == {"state.db"}


def test_evidence_context_uses_only_current_strict_heads_and_fingerprints_bindings(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    before_generation = runtime.store.get("goal", baseline.goal_id)["generation"]
    snapshot, _ = write_evidence(runtime, baseline, observer, provider)
    assert runtime.store.get("goal", baseline.goal_id)["generation"] == before_generation + 1

    context = runtime.review_context(baseline.goal_id)

    assert context.evidence_refs == (snapshot.evidence_id,)
    assert context.evidence_digests == ((snapshot.evidence_id, snapshot.sha256),)
    assert context.strict_binding["evidence_heads"][0]["evidence_id"] == snapshot.evidence_id
    for key in context.strict_binding:
        changed = context.to_dict()["strict_binding"]
        if isinstance(changed[key], int):
            changed[key] += 1
        elif isinstance(changed[key], str):
            changed[key] += "-changed"
        elif isinstance(changed[key], list):
            changed[key] = [*changed[key], {"changed": True}]
        assert replace(context, strict_binding=changed).fingerprint != context.fingerprint


def test_exact_evidence_replay_does_not_advance_goal_generation_or_event_history(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    snapshot, event = write_evidence(runtime, baseline, observer, provider)
    before_goal = runtime.store.get("goal", baseline.goal_id)
    before_events = runtime.store.audit()

    record = runtime.add_evidence(baseline.goal_id, snapshot, event)

    assert record["snapshot"] == snapshot.to_dict()
    active = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert record["strict_proof_id"] == active["evidence_heads"][0]["proof_id"]
    assert runtime.store.get("goal", baseline.goal_id)["generation"] == before_goal["generation"]
    assert runtime.store.audit() == before_events


@pytest.mark.parametrize("journal_mode_value", ["DELETE", "WAL"])
def test_strict_snapshot_keeps_one_view_during_concurrent_evidence_advance(tmp_path, journal_mode_value):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    old_snapshot, _ = write_evidence(
        runtime, baseline, observer, provider, evidence_id="shared-evidence"
    )
    before = runtime.store.read_strict_snapshot(baseline.goal_id)
    with sqlite3.connect(runtime.store.path) as db:
        if journal_mode_value == "DELETE":
            actual_mode = db.execute("PRAGMA journal_mode=DELETE").fetchone()[0].upper()
        else:
            actual_mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0].upper()
    assert actual_mode == journal_mode_value

    new_snapshot = EvidenceSnapshot.capture(
        old_snapshot.evidence_id, "artifact.txt", b"new content", source="trusted-runner", collected_at=NOW
    )
    new_event = observer.sign(
        "observer.evidence",
        new_snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": new_snapshot.to_dict()},
        event_id="evidence-refresh-event",
        at=NOW,
    )
    register_evidence(runtime, baseline, new_snapshot, new_event, provider)

    goal_read = threading.Event()
    continue_read = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    read_result: list[Any] = []
    write_result: list[Any] = []
    strict_get = runtime.store._strict_get

    def pause_after_goal(db, kind, key):
        value = strict_get(db, kind, key)
        if kind == "goal" and threading.current_thread().name == "strict-snapshot-reader":
            goal_read.set()
            if not continue_read.wait(3):
                raise TimeoutError("snapshot consistency test barrier timed out")
        return value

    runtime.store._strict_get = pause_after_goal

    def reader():
        try:
            read_result.append(runtime.store.read_strict_snapshot(baseline.goal_id))
        except BaseException as exc:
            read_result.append(exc)

    def writer():
        writer_started.set()
        try:
            write_result.append(runtime.add_evidence(baseline.goal_id, new_snapshot, new_event))
        except BaseException as exc:
            write_result.append(exc)
        finally:
            writer_done.set()

    reader_thread = threading.Thread(target=reader, name="strict-snapshot-reader")
    writer_thread = threading.Thread(target=writer, name="strict-evidence-writer")
    reader_thread.start()
    assert goal_read.wait(2)
    writer_thread.start()
    assert writer_started.wait(2)
    if journal_mode_value == "WAL":
        assert writer_done.wait(2)
    continue_read.set()
    reader_thread.join(timeout=4)
    writer_thread.join(timeout=4)

    assert not reader_thread.is_alive()
    assert not writer_thread.is_alive()
    assert len(read_result) == 1 and not isinstance(read_result[0], BaseException)
    assert len(write_result) == 1 and not isinstance(write_result[0], BaseException)
    read_snapshot = read_result[0]
    read_head = read_snapshot["evidence_heads"][0]
    read_proof = read_snapshot["evidence_proofs"][0]
    read_projection = read_snapshot["evidence_projections"][old_snapshot.evidence_id]
    assert read_head["proof_id"] == read_proof["proof_id"]
    assert read_projection["strict_proof_id"] == read_head["proof_id"]
    assert read_snapshot["goal"]["generation"] == before["goal"]["generation"]
    final_snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert final_snapshot["evidence_heads"][0]["proof_id"] != read_head["proof_id"]


def test_event_replay_accepts_refreshed_provider_nonce_without_extending_saved_proof(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    baseline_source = baseline_event(runtime, baseline)
    write_baseline(runtime, baseline, baseline_source, provider)
    evidence, evidence_source = write_evidence(runtime, baseline, observer, provider)
    before = runtime.store.read_strict_snapshot(baseline.goal_id)
    before_goal = runtime.store.get("goal", baseline.goal_id)
    before_events = runtime.store.audit()

    def refresh_proof(event, purpose, _binding):
        saved_binding, saved_proof = provider.registered[(event.event_id, purpose)]
        saved_proof.pop("proof_digest")
        saved_proof["nonce"] += "-refreshed"
        saved_proof["expires_at"] = "2026-10-18T01:00:00+08:00"
        saved_proof["proof_digest"] = fingerprint(saved_proof)
        provider.registered[(event.event_id, purpose)] = (saved_binding, saved_proof)

    provider.on_verify = refresh_proof

    runtime.start_baseline(baseline, baseline_source)
    runtime.add_evidence(baseline.goal_id, evidence, evidence_source)

    after = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert after["baseline_proof"] == before["baseline_proof"]
    assert after["evidence_proofs"] == before["evidence_proofs"]
    assert runtime.store.get("goal", baseline.goal_id)["generation"] == before_goal["generation"]
    assert runtime.store.audit() == before_events


@pytest.mark.parametrize("route", ["baseline_replay", "baseline_context", "evidence_replay"])
@pytest.mark.parametrize(
    "fresh_expiry",
    [LATER, "2026-10-18T01:00:00+08:00", "2026-10-16T12:00:00+08:00"],
)
def test_replay_never_extends_saved_proof_expiry(tmp_path, route, fresh_expiry):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    if route == "evidence_replay":
        write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
        evidence, evidence_event = write_evidence(runtime, baseline, observer, provider)
    else:
        baseline_event_value = baseline_event(runtime, baseline)
        write_baseline(runtime, baseline, baseline_event_value, provider)
    before_snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)
    before_events = runtime.store.audit()
    provider.refresh_responses = True
    provider.refresh_expires_at = fresh_expiry

    def replay():
        if route == "baseline_replay":
            return runtime.start_baseline(baseline, baseline_event_value)
        if route == "baseline_context":
            return runtime.review_context(baseline.goal_id)
        return runtime.add_evidence(baseline.goal_id, evidence, evidence_event)

    fresh_deadline = workflow_module.parse_time(fresh_expiry)
    saved_deadline = workflow_module.parse_time(LATER)
    if fresh_deadline < saved_deadline:
        assert_strict_code(replay, "STALE")
    else:
        replay()

    after_snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert after_snapshot == before_snapshot
    assert runtime.store.audit() == before_events
    assert after_snapshot["baseline_proof"]["expires_at"] == LATER
    assert all(proof["expires_at"] == LATER for proof in after_snapshot["evidence_proofs"])


def test_concurrent_exact_evidence_requests_append_one_proof(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    snapshot = EvidenceSnapshot.capture(
        "shared-evidence", "artifact.txt", b"done", source="trusted-runner", collected_at=NOW
    )
    event = observer.sign(
        "observer.evidence",
        snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()},
        event_id="shared-evidence-event",
        at=NOW,
    )
    register_evidence(runtime, baseline, snapshot, event, provider)
    provider.refresh_responses = True
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    waiting = [0]

    def synchronize_first_two(event_arg, _purpose, _binding):
        if event_arg.event_id != event.event_id:
            return
        with counter_lock:
            should_wait = waiting[0] < 2
            waiting[0] += 1
        if should_wait:
            barrier.wait(timeout=3)

    provider.on_verify = synchronize_first_two
    before_generation = runtime.store.get("goal", baseline.goal_id)["generation"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.add_evidence(baseline.goal_id, snapshot, event), range(2)))

    assert results[0] == results[1]
    assert results[0]["strict_proof_id"]
    assert runtime.store.get("goal", baseline.goal_id)["generation"] == before_generation + 1
    assert [item["event_id"] for item in runtime.store.audit()] == [
        runtime.store.read_strict_snapshot(baseline.goal_id)["baseline_proof"]["source_event_id"],
        event.event_id,
    ]


def test_existing_evidence_replay_rejects_a_replaced_head_before_return(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    original_snapshot, original_event = write_evidence(
        runtime, baseline, observer, provider, evidence_id="replayed-evidence"
    )
    replacement_snapshot = EvidenceSnapshot.capture(
        original_snapshot.evidence_id,
        original_snapshot.object_ref,
        b"replacement content",
        source=original_snapshot.source,
        collected_at=NOW,
    )
    replacement_event = observer.sign(
        "observer.evidence",
        replacement_snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": replacement_snapshot.to_dict()},
        event_id="replacement-evidence-event",
        at=NOW,
    )
    register_evidence(runtime, baseline, replacement_snapshot, replacement_event, provider)
    original_review_context = runtime.review_context
    replacement_records: list[dict[str, Any]] = []
    replaced = False

    def replace_before_final_context(goal_id: str):
        nonlocal replaced
        if not replaced:
            replaced = True
            replacement_records.append(
                runtime.add_evidence(goal_id, replacement_snapshot, replacement_event)
            )
        return original_review_context(goal_id)

    runtime.review_context = replace_before_final_context

    assert_strict_code(
        lambda: runtime.add_evidence(baseline.goal_id, original_snapshot, original_event), "STALE"
    )

    assert replaced
    assert replacement_records
    final_context = original_review_context(baseline.goal_id)
    assert final_context.strict_binding["evidence_heads"][0]["proof_id"] == replacement_records[0]["strict_proof_id"]


def test_concurrent_not_inserted_evidence_replay_rejects_a_replaced_head(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    original_snapshot = EvidenceSnapshot.capture(
        "concurrent-replay-evidence", "artifact.txt", b"original content",
        source="trusted-runner", collected_at=NOW,
    )
    original_event = observer.sign(
        "observer.evidence",
        original_snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": original_snapshot.to_dict()},
        event_id="concurrent-replay-event",
        at=NOW,
    )
    register_evidence(runtime, baseline, original_snapshot, original_event, provider)
    replacement_snapshot = EvidenceSnapshot.capture(
        original_snapshot.evidence_id, original_snapshot.object_ref, b"new content",
        source=original_snapshot.source, collected_at=NOW,
    )
    replacement_event = observer.sign(
        "observer.evidence",
        replacement_snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": replacement_snapshot.to_dict()},
        event_id="concurrent-replacement-event",
        at=NOW,
    )
    register_evidence(runtime, baseline, replacement_snapshot, replacement_event, provider)
    verification_barrier = threading.Barrier(2)
    provider.on_verify = lambda event, *_: (
        verification_barrier.wait(timeout=3) if event.event_id == original_event.event_id else None
    )
    original_commit = runtime.store.commit_strict
    replacement_done = threading.Event()
    interleave_lock = threading.Lock()
    not_inserted_threads: set[int] = set()

    def replace_after_concurrent_replay(*args, **kwargs):
        inserted, committed_proof = original_commit(*args, **kwargs)
        should_replace = False
        if args[2] == original_event.event_id and not inserted:
            with interleave_lock:
                not_inserted_threads.add(threading.get_ident())
                if not replacement_done.is_set():
                    replacement_done.set()
                    should_replace = True
        if should_replace:
            runtime.add_evidence(baseline.goal_id, replacement_snapshot, replacement_event)
        return inserted, committed_proof

    runtime.store.commit_strict = replace_after_concurrent_replay

    def attempt_replay():
        thread_id = threading.get_ident()
        try:
            return thread_id, runtime.add_evidence(baseline.goal_id, original_snapshot, original_event), None
        except StrictWorkflowError as exc:
            return thread_id, None, exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt_replay(), range(2)))

    assert replacement_done.is_set()
    loser_outcome = [outcome for outcome in outcomes if outcome[0] in not_inserted_threads]
    assert len(loser_outcome) == 1
    assert loser_outcome[0][1] is None
    assert isinstance(loser_outcome[0][2], StrictWorkflowError)
    assert loser_outcome[0][2].code == "STALE"


def test_sqlite_commit_failure_after_writes_rolls_back_and_maps_unavailable(monkeypatch, tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider)
    before_records = generic_snapshot(runtime)
    before_audit = runtime.store.audit()

    snapshot_table_sql = (
        ("events", "SELECT * FROM events"),
        ("records", "SELECT * FROM records"),
        ("strict_proofs", "SELECT * FROM strict_proofs"),
        ("strict_heads", "SELECT * FROM strict_heads"),
    )

    def database_rows():
        with sqlite3.connect(runtime.store.path) as db:
            return tuple(
                (table, tuple(sorted(db.execute(sql).fetchall(), key=repr)))
                for table, sql in snapshot_table_sql
            )

    before_database = database_rows()
    original_put = runtime.store._put
    attempted_writes = 0

    def fail_after_projection_write(db, kind, key, payload):
        nonlocal attempted_writes
        original_put(db, kind, key, payload)
        attempted_writes += 1
        if kind == "baseline":
            raise sqlite3.OperationalError("simulated SQLite write failure")

    monkeypatch.setattr(runtime.store, "_put", fail_after_projection_write)

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "UNAVAILABLE")

    assert attempted_writes == 1
    assert generic_snapshot(runtime) == before_records
    assert runtime.store.audit() == before_audit
    assert database_rows() == before_database
    assert runtime.store.read_strict_snapshot(baseline.goal_id)["baseline_proof"] is None


def test_committed_baseline_survives_runtime_lease_release_failure(caplog, monkeypatch, tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider)
    original_acquire = runtime_provider.acquire_lease

    @contextmanager
    def fail_after_release(expected_digest: str):
        with original_acquire(expected_digest) as lease:
            yield lease
        raise workflow_module.TrustedBindingUnavailable("sensitive release detail")

    monkeypatch.setattr(runtime_provider, "acquire_lease", fail_after_release)

    with caplog.at_level(logging.WARNING, logger="goalanchor.workflow_store"):
        result = runtime.start_baseline(baseline, event)

    saved = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert result["goal_id"] == baseline.goal_id
    assert saved["baseline_proof"]["source_event_id"] == event.event_id
    assert (
        "Strict transaction committed but runtime lease release failed; committed state is retained" in caplog.text
    )
    assert "sensitive release detail" not in caplog.text


def test_legacy_review_context_wire_shape_omits_strict_binding():
    context = ReviewContext(
        "goal", "prompt", (), (), "candidate", evidence_digests=(),
    )

    serialized = context.to_dict()

    assert "strict_binding" not in serialized
    assert ReviewContext.from_dict(serialized).fingerprint == context.fingerprint


def test_legacy_only_baseline_is_unverified_before_any_provider_call(tmp_path):
    runtime, baseline, user, _, provider, runtime_provider = make_world(tmp_path)
    legacy = Workflow(runtime.store, user, runtime.observer_authority, runtime.policy, clock=lambda: NOW)
    legacy.start_baseline(baseline, baseline_event(runtime, baseline, event_id="legacy-baseline"))
    provider.calls.clear()
    runtime_provider.read_count = 0

    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "LEGACY_UNVERIFIED")

    assert provider.calls == []
    assert runtime_provider.read_count == 0


def test_unmatched_legacy_evidence_never_enters_strict_context(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    legacy_snapshot = EvidenceSnapshot.capture(
        "legacy-evidence", "old.txt", b"old", source="legacy", collected_at=NOW
    )
    runtime.store.commit(
        [("evidence", baseline.goal_id + ":" + legacy_snapshot.evidence_id,
          {"goal_id": baseline.goal_id, "snapshot": legacy_snapshot.to_dict(), "source_event_id": "legacy-e"})],
        {("evidence", baseline.goal_id + ":" + legacy_snapshot.evidence_id): None},
        "legacy-evidence-event",
        {"kind": "legacy-evidence"},
    )

    context = runtime.review_context(baseline.goal_id)

    assert context.evidence_refs == ()


def test_strict_snapshot_rejects_evidence_projection_pointer_without_head(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    evidence, _ = write_evidence(runtime, baseline, observer, provider)
    active = runtime.store.read_strict_snapshot(baseline.goal_id)
    evidence_head = active["evidence_heads"][0]
    assert active["evidence_projections"][evidence.evidence_id]["strict_proof_id"] == evidence_head["proof_id"]
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TRIGGER strict_heads_no_delete")
        db.execute(
            "DELETE FROM strict_heads WHERE kind='evidence' AND goal_id=? AND subject_id=?",
            (baseline.goal_id, evidence.evidence_id),
        )

    with pytest.raises(workflow_module.WorkflowStoreConflict) as captured:
        runtime.store.read_strict_snapshot(baseline.goal_id)

    assert captured.value.code == "UNTRUSTED"


@pytest.mark.parametrize("field", ["purpose", "binding_digest"])
def test_strict_snapshot_recomputes_evidence_attestation_binding(tmp_path, field):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    evidence, _ = write_evidence(runtime, baseline, observer, provider)
    snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)
    proof = snapshot["evidence_proofs"][0]
    payload = json.loads(json.dumps(proof))
    if field == "purpose":
        payload["trusted_proof"][field] = "user.baseline"
    else:
        payload["trusted_proof"][field] = "0" * 64
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DROP TRIGGER strict_proofs_no_update")
        db.execute(
            "UPDATE strict_proofs SET payload=? WHERE proof_id=?",
            (canonical(payload), proof["proof_id"]),
        )

    with pytest.raises(workflow_module.WorkflowStoreConflict) as captured:
        runtime.store.read_strict_snapshot(baseline.goal_id)

    assert captured.value.code == "UNTRUSTED"


def test_strict_evidence_commit_rejects_projection_snapshot_mismatch_before_writes(monkeypatch, tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    snapshot = EvidenceSnapshot.capture(
        "bound-evidence", "artifact.txt", b"proved content", source="trusted-runner", collected_at=NOW
    )
    event = observer.sign(
        "observer.evidence",
        snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()},
        event_id="bound-evidence-event",
        at=NOW,
    )
    register_evidence(runtime, baseline, snapshot, event, provider)
    before_snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)
    before_events = runtime.store.audit()
    original_commit = runtime.store.commit_strict

    def tamper_projection(changes, expected, event_id, event_payload, proof, expected_head, lease_factory, **kwargs):
        bad_changes = list(changes)
        kind, key, record = bad_changes[0]
        bad_record = json.loads(json.dumps(record))
        bad_record["snapshot"]["object_ref"] = "different-object.txt"
        bad_changes[0] = (kind, key, bad_record)
        bad_proof = json.loads(json.dumps(proof))
        bad_proof["payload"]["projection"] = bad_record
        return original_commit(
            bad_changes, expected, event_id, event_payload, bad_proof, expected_head, lease_factory, **kwargs
        )

    monkeypatch.setattr(runtime.store, "commit_strict", tamper_projection)

    assert_strict_code(
        lambda: runtime.add_evidence(baseline.goal_id, snapshot, event), "UNTRUSTED"
    )

    assert runtime.store.read_strict_snapshot(baseline.goal_id) == before_snapshot
    assert runtime.store.audit() == before_events


def test_generic_baseline_projection_divergence_is_untrusted(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "UPDATE records SET payload=? WHERE kind='baseline' AND key=?",
            (canonical({**baseline.to_dict(), "objective": "tampered"}), baseline.fingerprint),
        )

    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "UNTRUSTED")


def test_generic_goal_projection_divergence_is_untrusted(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    with sqlite3.connect(runtime.store.path) as db:
        row = db.execute("SELECT payload FROM records WHERE kind='goal' AND key=?", (baseline.goal_id,)).fetchone()
        goal = json.loads(row[0])
        goal["strict_baseline_proof_id"] = "forged-proof-id"
        db.execute("UPDATE records SET payload=? WHERE kind='goal' AND key=?", (canonical(goal), baseline.goal_id))

    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "UNTRUSTED")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "CLOSED"),
        ("generation", True),
        ("generation", 1.0),
        ("generation", -7),
        ("baseline_created_at", "not-a-timestamp"),
        ("baseline_created_at", LATER),
    ],
)
def test_strict_snapshot_rejects_invalid_baseline_goal_projection(tmp_path, field, value):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    before_events = runtime.store.audit()
    with sqlite3.connect(runtime.store.path) as db:
        raw = db.execute(
            "SELECT payload FROM records WHERE kind='goal' AND key=?", (baseline.goal_id,)
        ).fetchone()[0]
        goal = json.loads(raw)
        goal[field] = value
        db.execute(
            "UPDATE records SET payload=? WHERE kind='goal' AND key=?",
            (canonical(goal), baseline.goal_id),
        )

    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "UNTRUSTED")

    assert runtime.store.audit() == before_events
    with pytest.raises(workflow_module.WorkflowStoreConflict) as captured:
        runtime.store.read_strict_snapshot(baseline.goal_id)
    assert captured.value.code == "UNTRUSTED"


def test_strict_context_detects_generation_change_during_provider_verification(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)

    def mutate(_event, _purpose, _binding):
        old = runtime.store.get("question", "legacy-question")
        runtime.store.commit(
            [("question", "legacy-question", {"goal_id": baseline.goal_id, "state": "WAIT"})],
            {("question", "legacy-question"): old},
            "legacy-question-change",
            {"kind": "legacy-question"},
        )

    provider.on_verify = mutate
    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "STALE")


def test_strict_context_proof_expiry_during_provider_verification_is_stale(tmp_path):
    clock_value = [NOW]
    runtime, baseline, _, _, provider, _ = make_world(tmp_path, clock=lambda: clock_value[0])
    event = baseline_event(runtime, baseline)
    register_baseline(
        runtime, baseline, event, provider, expires_at="2026-10-16T01:05:00+08:00"
    )
    runtime.start_baseline(baseline, event)
    provider.on_verify = lambda *_: clock_value.__setitem__(0, "2026-10-16T01:06:00+08:00")

    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "STALE")


def test_strict_context_lease_catches_rotation_after_last_provider_read(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime_provider.read_count = 0

    def rotate_after_post_provider_snapshot(_value):
        if runtime_provider.read_count == 2:
            runtime_provider.rotate(session_id="rotated-before-context-lease")

    runtime_provider.after_read = rotate_after_post_provider_snapshot

    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "STALE")


def test_runtime_identity_change_invalidates_evidence_and_strict_decisions(tmp_path):
    runtime, baseline, _, observer, provider, runtime_provider = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime_provider.rotate(session_id="session-2")
    snapshot = EvidenceSnapshot.capture(
        "evidence-1", "artifact.txt", b"done", source="trusted-runner", collected_at=NOW
    )
    event = observer.sign(
        "observer.evidence", snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()}, at=NOW,
    )

    assert_strict_code(lambda: runtime.add_evidence(baseline.goal_id, snapshot, event), "STALE")
    assert_strict_code(lambda: runtime.work_item_decision(baseline.goal_id, "write"), "STALE")
    assert_strict_code(lambda: runtime.close(baseline.goal_id), "STALE")
    runtime.store.commit(
        [("question", "legacy-q", {"goal_id": baseline.goal_id, "state": "WAIT"})],
        {("question", "legacy-q"): None},
        "legacy-q-event",
        {"kind": "legacy-question"},
    )
    assert_strict_code(lambda: runtime.work_item_decision(baseline.goal_id, "write"), "STALE")


def test_runtime_provider_unavailable_is_fail_closed(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider)
    runtime_provider.available = False

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "UNAVAILABLE")


def test_user_event_proof_identity_must_match_live_runtime_identity(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider, principal_id="different-user")

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "UNTRUSTED")
    assert runtime.store.get("goal", baseline.goal_id) is None


def test_strict_context_does_not_fall_back_to_previous_baseline_proof(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    next_baseline = Baseline(
        baseline.goal_id, 2, "New objective", "New plan",
        (Criterion("artifact", "new content", canonical("next"), "artifact.txt"),),
    )
    next_event = baseline_event(runtime, next_baseline, event_id="baseline-event-2")
    write_baseline(runtime, next_baseline, next_event, provider)
    provider.registered.pop(("baseline-event-2", "user.baseline"))
    provider.calls.clear()

    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "UNTRUSTED")

    assert provider.calls and provider.calls[0][0] == "baseline-event-2"
    assert runtime_provider.lease_count == 2


def test_no_active_strict_evidence_returns_empty_context_not_authorization(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)

    context = runtime.review_context(baseline.goal_id)

    assert context.evidence_refs == ()
    assert context.evidence_digests == ()
    assert "PASS" not in context.to_dict()


def test_expired_newest_evidence_proof_never_falls_back_to_older_head(tmp_path):
    clock_value = [NOW]
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path, clock=lambda: clock_value[0])
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    write_evidence(
        runtime, baseline, observer, provider, evidence_id="same-evidence",
        snapshot_expires_at="2026-10-16T02:00:00+08:00",
        proof_expires_at="2026-10-16T02:00:00+08:00",
    )
    first_head = runtime.store.read_strict_snapshot(baseline.goal_id)["evidence_heads"][0]
    write_evidence(
        runtime, baseline, observer, provider, evidence_id="same-evidence",
        snapshot_expires_at="2026-10-16T01:15:00+08:00",
        proof_expires_at="2026-10-16T01:15:00+08:00",
    )
    snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert snapshot["evidence_heads"][0]["source_seq"] > first_head["source_seq"]
    clock_value[0] = "2026-10-16T01:20:00+08:00"

    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "STALE")


def test_strict_operation_registration_is_namespaced_idempotent_and_content_bound(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(
        baseline,
        decision_dependencies={"operation:item-1": ("item-1",)},
    )
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    generation_before = runtime.store.get("goal", baseline.goal_id)["generation"]
    resource = tmp_path / "input.txt"
    resource.write_bytes(b"approved input")
    operation = Operation(
        "op-1",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        "{}",
        str(tmp_path),
        "item-1",
        resource_preconditions=(("input.txt", hashlib.sha256(b"approved input").hexdigest()),),
    )

    stored = runtime.register_operation(operation)

    assert stored == operation.to_dict()
    strict_record = runtime.store.get("strict_operation", "op-1")
    assert strict_record is not None
    assert set(strict_record) == {
        "goal_id",
        "baseline_digest",
        "operation_digest",
        "operation",
        "path_schema_digest",
        "registration_event_id",
        "registration_event_digest",
    }
    assert {key: strict_record[key] for key in (
        "goal_id", "baseline_digest", "operation_digest", "operation", "path_schema_digest"
    )} == {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "operation_digest": operation.fingerprint,
        "operation": operation.to_dict(),
        "path_schema_digest": fingerprint({"tool": "write_text", "path_fields": ["path"]}),
    }
    registration_event = runtime.store.get_event(strict_record["registration_event_id"])
    assert registration_event is not None
    assert strict_record["registration_event_digest"] == runtime.store.fingerprint(registration_event)
    assert registration_event["payload"]["path_schema_digest"] == strict_record["path_schema_digest"]
    assert runtime.store.get("operation", "op-1") is None
    assert runtime.store.get("strict_grant", "op-1") is None
    assert runtime.store.get("strict_receipt", "op-1") is None
    assert runtime.store.get("goal", baseline.goal_id)["generation"] == generation_before + 1
    after_first = strict_storage_rows(runtime)
    assert runtime.register_operation(operation) == stored
    assert strict_storage_rows(runtime) == after_first

    conflicting = replace(operation, arguments_json=canonical({"changed": True}))
    before_conflict = strict_storage_rows(runtime)
    assert_strict_code(lambda: runtime.register_operation(conflicting), "UNTRUSTED")
    assert strict_storage_rows(runtime) == before_conflict


@pytest.mark.parametrize("changed_dependency", ["resource", "baseline_expiry"])
def test_idempotent_operation_return_rechecks_dependencies_after_writer_lock_wait(tmp_path, changed_dependency):
    clock_value = [NOW]
    expires_at = "2026-10-16T01:00:10+08:00"
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path, clock=lambda: clock_value[0])
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    event = baseline_event(runtime, baseline)
    register_baseline(
        runtime,
        baseline,
        event,
        provider,
        expires_at=expires_at if changed_dependency == "baseline_expiry" else LATER,
    )
    runtime.start_baseline(baseline, event)
    resource = tmp_path / "input.txt"
    resource.write_bytes(b"approved")
    operation = Operation(
        "op-idempotent-lock-race",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({"path": "output.txt", "text": "done"}),
        str(tmp_path),
        "item-1",
        resource_preconditions=(
            (("input.txt", hashlib.sha256(b"approved").hexdigest()),)
            if changed_dependency == "resource" else ()
        ),
    )
    runtime.register_operation(operation)
    before = strict_storage_rows(runtime)
    starting_leases = runtime_provider.lease_count
    lock = sqlite3.connect(runtime.store.path, timeout=2, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    results: list[BaseException | dict[str, Any]] = []
    done = threading.Event()

    def register() -> None:
        try:
            results.append(runtime.register_operation(operation))
        except BaseException as exc:
            results.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=register)
    worker.start()
    try:
        assert _wait_for(
            lambda: runtime_provider.lease_count > starting_leases or done.is_set(), threading.Event(), timeout=2
        )
        if changed_dependency == "resource":
            resource.write_bytes(b"changed while waiting")
        else:
            clock_value[0] = expires_at
    finally:
        lock.rollback()
        lock.close()
    worker.join(timeout=3)

    assert done.is_set()
    assert len(results) == 1 and isinstance(results[0], StrictWorkflowError)
    assert results[0].code == "STALE"
    assert strict_storage_rows(runtime) == before


@pytest.mark.parametrize("changed_dependency", ["resource", "baseline_expiry"])
def test_concurrent_operation_recovery_rechecks_dependencies_after_final_lock_wait(
    monkeypatch, tmp_path, changed_dependency
):
    clock_value = [NOW]
    expires_at = "2026-10-16T01:00:10+08:00"
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path, clock=lambda: clock_value[0])
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    event = baseline_event(runtime, baseline)
    register_baseline(
        runtime,
        baseline,
        event,
        provider,
        expires_at=expires_at if changed_dependency == "baseline_expiry" else LATER,
    )
    runtime.start_baseline(baseline, event)
    lease_active = [False]
    original_current_binding = runtime_provider.current_runtime_binding
    original_acquire_lease = runtime_provider.acquire_lease

    def reject_nested_binding_read():
        if lease_active[0]:
            raise AssertionError("runtime binding cannot be reread while its exclusive lease is held")
        return original_current_binding()

    @contextmanager
    def guarded_lease(expected_digest):
        with original_acquire_lease(expected_digest) as binding:
            lease_active[0] = True
            try:
                yield binding
            finally:
                lease_active[0] = False

    monkeypatch.setattr(runtime_provider, "current_runtime_binding", reject_nested_binding_read)
    monkeypatch.setattr(runtime_provider, "acquire_lease", guarded_lease)
    resource = tmp_path / "input.txt"
    resource.write_bytes(b"approved")
    operation = Operation(
        "op-concurrent-recovery-lock-race",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({"path": "output.txt", "text": "done"}),
        str(tmp_path),
        "item-1",
        resource_preconditions=(
            (("input.txt", hashlib.sha256(b"approved").hexdigest()),)
            if changed_dependency == "resource" else ()
        ),
    )
    original_commit_many = runtime.store.commit_many
    concurrent_winner_rows: list[dict[str, Any]] = []

    def commit_winner_then_report_conflict(*args, **kwargs):
        committed = original_commit_many(*args, **kwargs)
        assert committed
        concurrent_winner_rows.append(strict_storage_rows(runtime))
        raise WorkflowStoreConflict("Concurrent exact operation commit won", "STALE")

    monkeypatch.setattr(runtime.store, "commit_many", commit_winner_then_report_conflict)
    original_get = runtime.store.get
    operation_record_lookups = 0
    final_lookup_started = threading.Event()
    continue_final_lookup = threading.Event()

    def pause_final_operation_lookup(kind, key):
        nonlocal operation_record_lookups
        if kind == "strict_operation" and key == operation.operation_id:
            operation_record_lookups += 1
            if operation_record_lookups == 3:
                final_lookup_started.set()
                assert continue_final_lookup.wait(timeout=3)
        return original_get(kind, key)

    monkeypatch.setattr(runtime.store, "get", pause_final_operation_lookup)
    results: list[BaseException | dict[str, Any]] = []
    done = threading.Event()

    def register() -> None:
        try:
            results.append(runtime.register_operation(operation))
        except BaseException as exc:
            results.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=register)
    worker.start()
    lock = None
    try:
        assert final_lookup_started.wait(timeout=3)
        lock = sqlite3.connect(runtime.store.path, timeout=2, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        if changed_dependency == "resource":
            resource.write_bytes(b"changed during conflict recovery")
        else:
            clock_value[0] = expires_at
        continue_final_lookup.set()
    finally:
        continue_final_lookup.set()
        if lock is not None:
            lock.rollback()
            lock.close()
    worker.join(timeout=3)

    assert done.is_set()
    assert len(concurrent_winner_rows) == 1
    assert len(results) == 1 and isinstance(results[0], StrictWorkflowError)
    assert results[0].code == "STALE"
    assert strict_storage_rows(runtime) == concurrent_winner_rows[0]


def test_strict_operation_registration_rejects_injected_record_without_signed_provenance(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-injected", baseline.goal_id, baseline.fingerprint, "write_text", "{}", str(tmp_path), "item-1"
    )
    injected = {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "operation_digest": operation.fingerprint,
        "operation": operation.to_dict(),
    }
    runtime.store.commit(
        [("strict_operation", operation.operation_id, injected)],
        {("strict_operation", operation.operation_id): None},
        "injected-strict-operation",
        {"kind": "untrusted-injection"},
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "UNTRUSTED")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_revalidates_signed_event_after_restart_and_tampering(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-event-tamper", baseline.goal_id, baseline.fingerprint, "write_text", "{}", str(tmp_path), "item-1"
    )
    runtime.register_operation(operation)
    reopened = StrictWorkflow(
        WorkflowStore(runtime.store.path),
        runtime.user_authority,
        runtime.observer_authority,
        runtime.policy,
        trusted_event_verifier=provider,
        trusted_runtime_binding_provider=runtime.runtime_binding_provider,
        clock=runtime.clock,
    )
    assert reopened.register_operation(operation) == operation.to_dict()
    record = runtime.store.get("strict_operation", operation.operation_id)
    assert record is not None
    raw_event = runtime.store.get_event(record["registration_event_id"])
    assert raw_event is not None
    baseline_event_payload = runtime.store.get_event("baseline-event")
    assert baseline_event_payload is not None
    record["registration_event_id"] = "baseline-event"
    record["registration_event_digest"] = runtime.store.fingerprint(baseline_event_payload)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "UPDATE records SET payload=? WHERE kind='strict_operation' AND key=?",
            (json.dumps(record, sort_keys=True, separators=(",", ":")), operation.operation_id),
        )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: reopened.register_operation(operation), "UNTRUSTED")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_rejects_unapproved_work_item_without_writes(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(
        baseline,
        decision_dependencies={"operation:item-1": ("item-1",)},
    )
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-unmapped", baseline.goal_id, baseline.fingerprint, "write_text", "{}", str(tmp_path), "item-2"
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "UNTRUSTED")

    assert strict_storage_rows(runtime) == before


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"tool": "delete_everything"}, "UNTRUSTED"),
        ({"cwd": str(Path.cwd())}, "UNTRUSTED"),
        ({"baseline_digest": "0" * 64}, "STALE"),
    ],
)
def test_strict_operation_registration_rejects_scope_and_baseline_changes_without_writes(
    tmp_path, changes, expected_code
):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-invalid", baseline.goal_id, baseline.fingerprint, "write_text", "{}", str(tmp_path), "item-1"
    )
    invalid = replace(operation, **changes)
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(invalid), expected_code)

    assert strict_storage_rows(runtime) == before


@pytest.mark.parametrize(
    ("path_key", "path_kind"),
    [("path", "traversal"), ("path", "absolute"), ("outputPath", "absolute"), ("fileName", "absolute")],
)
def test_strict_operation_registration_rejects_outside_path_in_tool_arguments(tmp_path, path_key, path_kind):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    outside = tmp_path.parent / "outside.txt"
    target = "../outside.txt" if path_kind == "traversal" else str(outside)
    operation = Operation(
        "op-argument-path",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({path_key: target, "text": "unsafe"}),
        str(tmp_path),
        "item-1",
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "UNTRUSTED")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_fails_closed_without_a_tool_path_schema(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path, allowed_tools=("custom.write",))
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-unknown-schema",
        baseline.goal_id,
        baseline.fingerprint,
        "custom.write",
        canonical({"fileName": "output.txt", "text": "done"}),
        str(tmp_path),
        "item-1",
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "UNAVAILABLE")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_uses_supplied_tool_path_schema(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(
        tmp_path,
        operation_path_schemas={"write_text": ("fileName",)},
    )
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-configured-schema",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({"fileName": "output.txt", "text": "done"}),
        str(tmp_path),
        "item-1",
    )

    assert runtime.register_operation(operation) == operation.to_dict()


def test_strict_operation_registration_rejects_outside_path_under_custom_schema(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(
        tmp_path,
        operation_path_schemas={"write_text": ("fileName",)},
    )
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-custom-schema-outside",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({"fileName": str(tmp_path.parent / "outside.txt"), "text": "unsafe"}),
        str(tmp_path),
        "item-1",
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "UNTRUSTED")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_path_schema_change_invalidates_saved_registration(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(
        tmp_path,
        operation_path_schemas={"write_text": ("fileName",)},
    )
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-schema-change",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({"fileName": "output.txt", "text": "done"}),
        str(tmp_path),
        "item-1",
    )
    runtime.register_operation(operation)
    reopened = StrictWorkflow(
        WorkflowStore(runtime.store.path),
        runtime.user_authority,
        runtime.observer_authority,
        runtime.policy,
        trusted_event_verifier=provider,
        trusted_runtime_binding_provider=runtime.runtime_binding_provider,
        operation_path_schemas={"write_text": ("fileName", "outputPath")},
        clock=runtime.clock,
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: reopened.register_operation(operation), "UNTRUSTED")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_rejects_argument_path_symlink_escape(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    outside = tmp_path.parent / "outside-symlink-target.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("This environment cannot create file symlinks")
    operation = Operation(
        "op-symlink-argument",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({"path": "escape.txt", "text": "unsafe"}),
        str(tmp_path),
        "item-1",
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "UNTRUSTED")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_rejects_outside_resource_preconditions_without_writes(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    operation = Operation(
        "op-outside-resource",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        "{}",
        str(tmp_path),
        "item-1",
        resource_preconditions=(("../outside.txt", None),),
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "UNTRUSTED")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_rejects_changed_resource_precondition_without_writes(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    resource = tmp_path / "input.txt"
    resource.write_bytes(b"changed input")
    operation = Operation(
        "op-changed-resource",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        "{}",
        str(tmp_path),
        "item-1",
        resource_preconditions=(("input.txt", hashlib.sha256(b"approved input").hexdigest()),),
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "STALE")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_does_not_treat_existing_directory_as_absent_resource(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    directory = tmp_path / "existing-directory"
    directory.mkdir()
    operation = Operation(
        "op-directory-precondition",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({"path": "output.txt", "text": "done"}),
        str(tmp_path),
        "item-1",
        resource_preconditions=(("existing-directory", None),),
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "STALE")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_rejects_expired_baseline_proof_without_writes(tmp_path):
    clock_value = [NOW]
    runtime, baseline, _, _, provider, _ = make_world(tmp_path, clock=lambda: clock_value[0])
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    clock_value[0] = LATER
    operation = Operation(
        "op-expired-baseline", baseline.goal_id, baseline.fingerprint,
        "write_text", "{}", str(tmp_path), "artifact.txt",
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.register_operation(operation), "STALE")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_rechecks_baseline_expiry_after_writer_lock_wait(tmp_path):
    clock_value = [NOW]
    expires_at = "2026-10-16T01:00:10+08:00"
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path, clock=lambda: clock_value[0])
    baseline = replace(baseline, decision_dependencies={"operation:artifact.txt": ("artifact.txt",)})
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider, expires_at=expires_at)
    runtime.start_baseline(baseline, event)
    operation = Operation(
        "op-lock-expiry", baseline.goal_id, baseline.fingerprint,
        "write_text", "{}", str(tmp_path), "artifact.txt",
    )
    before = strict_storage_rows(runtime)
    starting_leases = runtime_provider.lease_count
    lock = sqlite3.connect(runtime.store.path, timeout=2, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    results: list[BaseException | dict[str, Any]] = []
    done = threading.Event()

    def register() -> None:
        try:
            results.append(runtime.register_operation(operation))
        except BaseException as exc:
            results.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=register)
    worker.start()
    try:
        assert _wait_for(
            lambda: runtime_provider.lease_count > starting_leases or done.is_set(), threading.Event(), timeout=2
        )
        clock_value[0] = expires_at
    finally:
        lock.rollback()
        lock.close()
    worker.join(timeout=3)

    assert done.is_set()
    assert len(results) == 1 and isinstance(results[0], StrictWorkflowError)
    assert results[0].code == "STALE"
    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_rechecks_resource_after_writer_lock_wait(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    resource = tmp_path / "input.txt"
    resource.write_bytes(b"approved")
    operation = Operation(
        "op-resource-lock-race",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({"path": "output.txt", "text": "done"}),
        str(tmp_path),
        "item-1",
        resource_preconditions=(("input.txt", hashlib.sha256(b"approved").hexdigest()),),
    )
    before = strict_storage_rows(runtime)
    starting_leases = runtime_provider.lease_count
    lock = sqlite3.connect(runtime.store.path, timeout=2, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    results: list[BaseException | dict[str, Any]] = []
    done = threading.Event()

    def register() -> None:
        try:
            results.append(runtime.register_operation(operation))
        except BaseException as exc:
            results.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=register)
    worker.start()
    try:
        assert _wait_for(
            lambda: runtime_provider.lease_count > starting_leases or done.is_set(), threading.Event(), timeout=2
        )
        resource.write_bytes(b"changed while waiting")
    finally:
        lock.rollback()
        lock.close()
    worker.join(timeout=3)

    assert done.is_set()
    assert len(results) == 1 and isinstance(results[0], StrictWorkflowError)
    assert results[0].code == "STALE"
    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_rolls_back_if_baseline_expires_before_commit(monkeypatch, tmp_path):
    clock_value = [NOW]
    expires_at = "2026-10-16T01:00:10+08:00"
    runtime, baseline, _, _, provider, _ = make_world(tmp_path, clock=lambda: clock_value[0])
    baseline = replace(baseline, decision_dependencies={"operation:artifact.txt": ("artifact.txt",)})
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider, expires_at=expires_at)
    runtime.start_baseline(baseline, event)
    operation = Operation(
        "op-commit-expiry", baseline.goal_id, baseline.fingerprint,
        "write_text", "{}", str(tmp_path), "artifact.txt",
    )
    before = strict_storage_rows(runtime)
    original_put = runtime.store._put

    def expire_after_operation_write(db, kind, key, payload):
        original_put(db, kind, key, payload)
        if kind == "strict_operation":
            clock_value[0] = expires_at

    monkeypatch.setattr(runtime.store, "_put", expire_after_operation_write)

    assert_strict_code(lambda: runtime.register_operation(operation), "STALE")

    assert strict_storage_rows(runtime) == before


def test_strict_operation_registration_rolls_back_if_resource_changes_before_commit(monkeypatch, tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    resource = tmp_path / "input.txt"
    resource.write_bytes(b"approved")
    operation = Operation(
        "op-resource-commit-race",
        baseline.goal_id,
        baseline.fingerprint,
        "write_text",
        canonical({"path": "output.txt", "text": "done"}),
        str(tmp_path),
        "item-1",
        resource_preconditions=(("input.txt", hashlib.sha256(b"approved").hexdigest()),),
    )
    before = strict_storage_rows(runtime)
    original_put = runtime.store._put

    def mutate_resource_after_operation_write(db, kind, key, payload):
        original_put(db, kind, key, payload)
        if kind == "strict_operation":
            resource.write_bytes(b"changed before commit")

    monkeypatch.setattr(runtime.store, "_put", mutate_resource_after_operation_write)

    assert_strict_code(lambda: runtime.register_operation(operation), "STALE")

    assert strict_storage_rows(runtime) == before


def test_concurrent_identical_operation_registration_converges_once(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    barrier = threading.Barrier(2)
    provider.on_verify = lambda *_: barrier.wait(timeout=3)
    operation = Operation(
        "op-concurrent", baseline.goal_id, baseline.fingerprint, "write_text", "{}", str(tmp_path), "item-1"
    )
    generation_before = runtime.store.get("goal", baseline.goal_id)["generation"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runtime.register_operation, operation) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]

    assert results == [operation.to_dict(), operation.to_dict()]
    assert len(runtime.store.list("strict_operation", baseline.goal_id)) == 1
    operation_events = [item for item in runtime.store.audit() if item["payload"].get("kind") == "controller.operation"]
    assert len(operation_events) == 1
    assert runtime.store.get("goal", baseline.goal_id)["generation"] == generation_before + 1


def test_concurrent_same_id_different_operations_cannot_overwrite_winner(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"operation:item-1": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    barrier = threading.Barrier(2)
    provider.on_verify = lambda *_: barrier.wait(timeout=3)
    operation = Operation(
        "op-concurrent-conflict", baseline.goal_id, baseline.fingerprint,
        "write_text", canonical({"path": "one.txt", "text": "one"}), str(tmp_path), "item-1",
    )
    conflicting = replace(operation, arguments_json=canonical({"path": "two.txt", "text": "two"}))

    def attempt(candidate):
        try:
            return runtime.register_operation(candidate)
        except StrictWorkflowError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, candidate) for candidate in (operation, conflicting)]
        results = [future.result(timeout=5) for future in futures]

    assert sum(result in (operation.to_dict(), conflicting.to_dict()) for result in results) == 1
    errors = [result for result in results if isinstance(result, StrictWorkflowError)]
    assert len(errors) == 1 and errors[0].code == "UNTRUSTED"
    winner = runtime.store.get("strict_operation", operation.operation_id)
    assert winner is not None
    assert winner["operation"] in (operation.to_dict(), conflicting.to_dict())
    assert winner["operation_digest"] == (
        operation.fingerprint if winner["operation"] == operation.to_dict() else conflicting.fingerprint
    )


def test_host_lease_drift_while_waiting_for_db_writer_lock_writes_nothing(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider)
    path = tmp_path / "state.db"
    lock = sqlite3.connect(path, timeout=1, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    result: list[BaseException | dict[str, Any]] = []
    done = threading.Event()

    def start() -> None:
        try:
            result.append(runtime.start_baseline(baseline, event))
        except BaseException as exc:
            result.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=start)
    worker.start()
    try:
        deadline = threading.Event()
        assert _wait_for(lambda: bool(provider.calls), deadline, timeout=2)
        runtime_provider.rotate(session_id="session-after-lock-wait")
    finally:
        lock.rollback()
        lock.close()
    worker.join(timeout=3)

    assert done.is_set()
    assert len(result) == 1 and isinstance(result[0], StrictWorkflowError)
    assert result[0].code == "STALE"
    assert runtime.store.get("goal", baseline.goal_id) is None
    assert runtime.store.read_strict_snapshot(baseline.goal_id)["baseline_proof"] is None


def test_proof_expiry_while_waiting_for_db_writer_lock_writes_nothing(tmp_path):
    clock_value = [NOW]
    runtime, baseline, _, _, provider, _ = make_world(tmp_path, clock=lambda: clock_value[0])
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider, expires_at="2026-10-16T01:00:10+08:00")
    path = tmp_path / "state.db"
    lock = sqlite3.connect(path, timeout=1, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    result: list[BaseException | dict[str, Any]] = []
    done = threading.Event()

    def start() -> None:
        try:
            result.append(runtime.start_baseline(baseline, event))
        except BaseException as exc:
            result.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=start)
    worker.start()
    try:
        assert _wait_for(lambda: bool(provider.calls), threading.Event(), timeout=2)
        clock_value[0] = "2026-10-16T01:00:11+08:00"
    finally:
        lock.rollback()
        lock.close()
    worker.join(timeout=3)

    assert done.is_set()
    assert len(result) == 1 and isinstance(result[0], StrictWorkflowError)
    assert result[0].code == "STALE"
    assert runtime.store.read_strict_snapshot(baseline.goal_id)["baseline_proof"] is None


def test_proof_expiry_after_strict_writes_but_before_commit_is_stale(tmp_path):
    clock_value = [NOW]
    runtime, baseline, _, _, provider, _ = make_world(tmp_path, clock=lambda: clock_value[0])
    event = baseline_event(runtime, baseline)
    register_baseline(runtime, baseline, event, provider, expires_at="2026-10-16T01:00:10+08:00")
    touch_goal = runtime.store._strict_touch_goal

    def advance_clock_after_writes(db, goal_id):
        touch_goal(db, goal_id)
        clock_value[0] = "2026-10-16T01:00:11+08:00"

    runtime.store._strict_touch_goal = advance_clock_after_writes

    assert_strict_code(lambda: runtime.start_baseline(baseline, event), "STALE")

    assert runtime.store.read_strict_snapshot(baseline.goal_id)["baseline_proof"] is None


def test_evidence_snapshot_expiry_after_writes_but_before_commit_is_stale(tmp_path):
    clock_value = [NOW]
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path, clock=lambda: clock_value[0])
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    snapshot = EvidenceSnapshot.capture(
        "short-lived", "artifact.txt", b"done", source="trusted-runner", collected_at=NOW,
        expires_at="2026-10-16T01:00:10+08:00",
    )
    event = observer.sign(
        "observer.evidence", snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()}, at=NOW,
    )
    register_evidence(runtime, baseline, snapshot, event, provider)
    touch_goal = runtime.store._strict_touch_goal

    def advance_clock_after_writes(db, goal_id):
        touch_goal(db, goal_id)
        clock_value[0] = "2026-10-16T01:00:11+08:00"

    runtime.store._strict_touch_goal = advance_clock_after_writes

    assert_strict_code(
        lambda: runtime.add_evidence(baseline.goal_id, snapshot, event), "STALE"
    )

    updated = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert updated["evidence_heads"] == []
    assert runtime.store.get(
        "strict_evidence", runtime.store.strict_evidence_key(baseline.goal_id, snapshot.evidence_id)
    ) is None
    assert runtime.store.get("evidence", baseline.goal_id + ":" + snapshot.evidence_id) is None


def _wait_for(predicate, event: threading.Event, *, timeout: float) -> bool:
    deadline = threading.Timer(timeout, event.set)
    deadline.start()
    try:
        while not event.is_set():
            if predicate():
                return True
            event.wait(0.01)
        return False
    finally:
        deadline.cancel()


def test_strict_evidence_colon_keys_do_not_collide_with_each_other_or_legacy_rows(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    legacy_key = "goal-a:b:c"
    legacy_record = {"legacy_marker": "must remain visible through the old API"}
    runtime.store.commit(
        [("evidence", legacy_key, legacy_record)],
        {("evidence", legacy_key): None},
        "legacy-evidence-collision-seed",
        {"kind": "legacy-evidence"},
    )

    first = replace(baseline, goal_id="goal-a")
    second = replace(baseline, goal_id="goal-a:b")
    write_baseline(runtime, first, baseline_event(runtime, first, event_id="baseline-goal-a"), provider)
    write_baseline(runtime, second, baseline_event(runtime, second, event_id="baseline-goal-a-sub"), provider)

    write_evidence(runtime, first, observer, provider, evidence_id="b:c")
    write_evidence(runtime, second, observer, provider, evidence_id="c")

    before_reads = strict_storage_rows(runtime)
    assert runtime.review_context(first.goal_id).evidence_refs == ("b:c",)
    assert runtime.review_context(second.goal_id).evidence_refs == ("c",)
    assert runtime.store.get("evidence", legacy_key) == legacy_record
    assert strict_storage_rows(runtime) == before_reads


def test_ambiguous_legacy_strict_evidence_key_fails_closed(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    first = replace(baseline, goal_id="goal-a")
    second = replace(baseline, goal_id="goal-a:b")
    write_baseline(runtime, first, baseline_event(runtime, first, event_id="baseline-legacy-a"), provider)
    write_baseline(runtime, second, baseline_event(runtime, second, event_id="baseline-legacy-a-sub"), provider)
    first_snapshot, _ = write_evidence(runtime, first, observer, provider, evidence_id="b:c")
    write_evidence(runtime, second, observer, provider, evidence_id="c")
    strict_key = runtime.store.strict_evidence_key(first.goal_id, first_snapshot.evidence_id)
    legacy_key = runtime.store.legacy_evidence_key(first.goal_id, first_snapshot.evidence_id)
    first_projection = runtime.store.get("strict_evidence", strict_key)

    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_evidence' AND key=?", (strict_key,))
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('evidence',?,?)",
            (legacy_key, canonical(first_projection)),
        )

    with pytest.raises(workflow_module.WorkflowStoreConflict) as captured:
        runtime.store.read_strict_snapshot(first.goal_id)
    assert captured.value.code == "UNTRUSTED"
    assert runtime.review_context(second.goal_id).evidence_refs == ("c",)


@pytest.mark.parametrize("projection", ["missing_pointer", "wrong_key"])
def test_orphan_strict_evidence_projection_fails_closed(tmp_path, projection):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    snapshot = EvidenceSnapshot.capture(
        "orphan-evidence", "artifact.txt", b"done", source="trusted-runner", collected_at=NOW
    )
    key = runtime.store.strict_evidence_key(baseline.goal_id, snapshot.evidence_id)
    if projection == "wrong_key":
        key = "v1:wrong:wrong"
    record = {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()}
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_evidence',?,?)",
            (key, canonical(record)),
        )

    before_read = strict_storage_rows(runtime)
    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "UNTRUSTED")
    assert strict_storage_rows(runtime) == before_read


@pytest.mark.parametrize("owner_state", ["missing", "wrong"])
def test_strict_evidence_current_prefix_wrong_owner_is_untrusted_without_writes(
    tmp_path, owner_state
):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    snapshot = EvidenceSnapshot.capture(
        "orphan-owner-evidence", "artifact.txt", b"done", source="trusted-runner", collected_at=NOW
    )
    key = runtime.store.strict_evidence_key(baseline.goal_id, snapshot.evidence_id)
    record = {"snapshot": snapshot.to_dict()}
    if owner_state == "wrong":
        record["goal_id"] = "another-goal"

    with sqlite3.connect(runtime.store.path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM strict_heads WHERE kind='evidence' AND goal_id=? AND subject_id=?",
            (baseline.goal_id, snapshot.evidence_id),
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM strict_proofs WHERE kind='evidence' AND goal_id=? AND subject_id=?",
            (baseline.goal_id, snapshot.evidence_id),
        ).fetchone()[0] == 0
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_evidence',?,?)",
            (key, canonical(record)),
        )

    before_read = strict_storage_rows(runtime)
    with pytest.raises(workflow_module.WorkflowStoreConflict) as captured:
        runtime.store.read_strict_snapshot(baseline.goal_id)

    assert captured.value.code == "UNTRUSTED"
    assert strict_storage_rows(runtime) == before_read


def test_legacy_strict_evidence_fallback_and_superseded_history_are_separate(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    old_snapshot, old_event = write_evidence(
        runtime, baseline, observer, provider, evidence_id="legacy-evidence-history"
    )
    strict_key = runtime.store.strict_evidence_key(baseline.goal_id, old_snapshot.evidence_id)
    legacy_key = runtime.store.legacy_evidence_key(baseline.goal_id, old_snapshot.evidence_id)
    old_projection = runtime.store.get("strict_evidence", strict_key)
    old_proof_id = old_projection["strict_proof_id"]

    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_evidence' AND key=?", (strict_key,))
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('evidence',?,?)",
            (legacy_key, canonical(old_projection)),
        )

    fallback = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert fallback["evidence_projections"][old_snapshot.evidence_id] == old_projection
    assert runtime.add_evidence(baseline.goal_id, old_snapshot, old_event)["strict_proof_id"] == old_proof_id
    assert runtime.store.get("strict_evidence", strict_key) is None

    replacement = EvidenceSnapshot.capture(
        old_snapshot.evidence_id,
        old_snapshot.object_ref,
        b"replacement evidence",
        source="trusted-runner",
        collected_at=NOW,
    )
    replacement_event = observer.sign(
        "observer.evidence",
        replacement.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": replacement.to_dict()},
        event_id="legacy-evidence-history-replacement",
        at=NOW,
    )
    register_evidence(runtime, baseline, replacement, replacement_event, provider)
    runtime.add_evidence(baseline.goal_id, replacement, replacement_event)

    updated = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert updated["evidence_projections"][replacement.evidence_id]["snapshot"]["sha256"] == replacement.sha256
    assert runtime.store.get("evidence", legacy_key)["strict_proof_id"] == old_proof_id
    assert runtime.store.get("strict_evidence", strict_key)["strict_proof_id"] != old_proof_id
    context = runtime.review_context(baseline.goal_id)
    assert context.evidence_digests == ((replacement.evidence_id, replacement.sha256),)


def test_strict_baseline_can_take_over_legacy_root_only_at_a_higher_revision(tmp_path):
    runtime, baseline, user, observer, provider, _ = make_world(tmp_path)
    legacy = Workflow(runtime.store, user, observer, runtime.policy, clock=lambda: NOW)
    legacy_goal = legacy.start_baseline(baseline, baseline_event(runtime, baseline, event_id="legacy-root"))
    before_events = runtime.store.audit()

    equal_revision = baseline_event(runtime, baseline, event_id="strict-equal-legacy-revision")
    assert_strict_code(lambda: runtime.start_baseline(baseline, equal_revision), "STALE")
    assert runtime.store.audit() == before_events

    replacement = replace(baseline, revision=2, plan="Strictly confirmed plan")
    replacement_event = baseline_event(runtime, replacement, event_id="strict-legacy-root-successor")
    write_baseline(runtime, replacement, replacement_event, provider)
    snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)

    assert snapshot["baseline_proof"]["payload"]["baseline"]["revision"] == 2
    assert snapshot["baseline_head"]["generation"] == 1
    assert snapshot["goal"]["supersedes"] == legacy_goal["baseline_digest"]


def test_competing_baseline_revisions_use_transactional_cas(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    first = replace(baseline, revision=2, objective="Candidate one")
    second = replace(baseline, revision=3, objective="Candidate two")
    first_event = baseline_event(runtime, first, event_id="baseline-race-one")
    second_event = baseline_event(runtime, second, event_id="baseline-race-two")
    register_baseline(runtime, first, first_event, provider)
    register_baseline(runtime, second, second_event, provider)
    barrier = threading.Barrier(2)
    provider.on_verify = lambda *_: barrier.wait(timeout=3)

    def start(candidate, event):
        try:
            return ("ok", runtime.start_baseline(candidate, event))
        except StrictWorkflowError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: start(*pair), ((first, first_event), (second, second_event))))

    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert [result[1] for result in results if result[0] == "error"] == ["STALE"]
    snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert snapshot["baseline_proof"]["payload"]["baseline"]["revision"] in {2, 3}
    assert len(runtime.store.audit()) == 2


def test_current_strict_baseline_supersedes_must_match_its_predecessor(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    replacement = replace(baseline, revision=2, plan="Second plan")
    write_baseline(
        runtime,
        replacement,
        baseline_event(runtime, replacement, event_id="baseline-supersedes-successor"),
        provider,
    )
    goal = runtime.store.get("goal", baseline.goal_id)
    assert goal is not None
    goal["supersedes"] = "0" * 64
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "UPDATE records SET payload=? WHERE kind='goal' AND key=?",
            (canonical(goal), baseline.goal_id),
        )

    with pytest.raises(WorkflowStoreConflict) as captured:
        runtime.store.read_strict_snapshot(baseline.goal_id)
    assert captured.value.code == "UNTRUSTED"


def test_evidence_commit_rechecks_active_baseline_inside_write_transaction(monkeypatch, tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    snapshot = EvidenceSnapshot.capture(
        "baseline-race-evidence", "artifact.txt", b"done", source="trusted-runner", collected_at=NOW
    )
    event = observer.sign(
        "observer.evidence",
        snapshot.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": snapshot.to_dict()},
        event_id="baseline-race-evidence-event",
        at=NOW,
    )
    register_evidence(runtime, baseline, snapshot, event, provider)
    replacement = replace(baseline, revision=2, plan="Concurrent baseline replacement")
    replacement_event = baseline_event(runtime, replacement, event_id="baseline-race-replacement")
    register_baseline(runtime, replacement, replacement_event, provider)
    original_commit = runtime._store_commit
    replaced = False

    def replace_baseline_then_commit_evidence(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            runtime._store_commit = original_commit
            runtime.start_baseline(replacement, replacement_event)
            runtime._store_commit = replace_baseline_then_commit_evidence
            expected = dict(args[1])
            expected[("goal", baseline.goal_id)] = runtime.store.fingerprint(
                runtime.store.get("goal", baseline.goal_id)
            )
            args = (args[0], expected, *args[2:])
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(runtime, "_store_commit", replace_baseline_then_commit_evidence)
    before_events = runtime.store.audit()

    assert_strict_code(lambda: runtime.add_evidence(baseline.goal_id, snapshot, event), "STALE")

    updated = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert updated["baseline_proof"]["baseline_digest"] == replacement.fingerprint
    assert updated["evidence_heads"] == []
    event_ids = tuple(item["event_id"] for item in runtime.store.audit())
    previous_event_ids = tuple(item["event_id"] for item in before_events)
    assert event_ids == (*previous_event_ids, replacement_event.event_id)
    assert runtime.store.get(
        "strict_evidence", runtime.store.strict_evidence_key(baseline.goal_id, snapshot.evidence_id)
    ) is None


def test_strict_snapshot_validates_each_active_projection_once_per_read(tmp_path, monkeypatch):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    first, _ = write_evidence(runtime, baseline, observer, provider, evidence_id="evidence-a")
    second, _ = write_evidence(runtime, baseline, observer, provider, evidence_id="evidence-b")
    strict_keys = {
        runtime.store.strict_evidence_key(baseline.goal_id, first.evidence_id),
        runtime.store.strict_evidence_key(baseline.goal_id, second.evidence_id),
    }
    calls = track_evidence_projection_validations(monkeypatch)

    snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)

    assert {item["subject_id"] for item in snapshot["evidence_heads"]} == {
        first.evidence_id,
        second.evidence_id,
    }
    assert {key for key in calls if key in strict_keys} == strict_keys
    assert all(calls.count(key) == 1 for key in strict_keys)


def test_unvisited_orphan_projection_still_uses_full_validation(tmp_path, monkeypatch):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    orphan = EvidenceSnapshot.capture(
        "orphan-evidence-unmapped",
        "artifact.txt",
        b"orphan",
        source="trusted-runner",
        collected_at=NOW,
    )
    orphan_key = runtime.store.strict_evidence_key(baseline.goal_id, orphan.evidence_id)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_evidence',?,?)",
            (orphan_key, canonical({"goal_id": baseline.goal_id, "snapshot": orphan.to_dict()})),
        )
    before_read = strict_storage_rows(runtime)
    calls = track_evidence_projection_validations(monkeypatch)

    with pytest.raises(workflow_module.WorkflowStoreConflict) as captured:
        runtime.store.read_strict_snapshot(baseline.goal_id)

    assert captured.value.code == "UNTRUSTED"
    assert calls.count(orphan_key) == 1
    assert strict_storage_rows(runtime) == before_read


def test_historical_projection_without_current_head_still_uses_full_validation(tmp_path, monkeypatch):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    old_snapshot, _ = write_evidence(
        runtime, baseline, observer, provider, evidence_id="historical-evidence-unmapped"
    )
    replacement = Baseline(
        baseline.goal_id,
        2,
        "Replacement objective",
        "Replacement plan",
        (Criterion("artifact", "replacement exists", canonical("new"), "artifact.txt"),),
    )
    replacement_event = baseline_event(runtime, replacement, event_id="baseline-historical-evidence")
    register_baseline(runtime, replacement, replacement_event, provider)
    runtime.start_baseline(replacement, replacement_event)
    old_key = runtime.store.strict_evidence_key(baseline.goal_id, old_snapshot.evidence_id)
    assert runtime.store.get("strict_evidence", old_key) is not None
    calls = track_evidence_projection_validations(monkeypatch)

    snapshot = runtime.store.read_strict_snapshot(baseline.goal_id)

    assert snapshot["evidence_heads"] == []
    assert calls.count(old_key) == 1


def test_coordinated_evidence_projection_corruption_is_rejected_without_read_side_effects(
    tmp_path, monkeypatch,
):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    snapshot, _ = write_evidence(runtime, baseline, observer, provider)
    record_key = runtime.store.strict_evidence_key(baseline.goal_id, snapshot.evidence_id)
    calls = track_evidence_projection_validations(monkeypatch)
    strict = runtime.store.read_strict_snapshot(baseline.goal_id)
    assert calls.count(record_key) == 1
    calls.clear()
    evidence_head = next(
        item for item in strict["evidence_heads"] if item["subject_id"] == snapshot.evidence_id
    )
    proof_id = evidence_head["proof_id"]

    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DROP TRIGGER strict_proofs_no_update")
        proof_row = db.execute(
            "SELECT payload FROM strict_proofs WHERE proof_id=?", (proof_id,)
        ).fetchone()
        assert proof_row is not None
        proof = json.loads(proof_row[0])
        projection = proof["payload"]["projection"]
        projection["trusted_source"]["principal_id"] = "coordinated-corruption"
        db.execute(
            "UPDATE strict_proofs SET payload=? WHERE proof_id=?",
            (canonical(proof), proof_id),
        )
        db.execute(
            "UPDATE records SET payload=? WHERE kind='strict_evidence' AND key=?",
            (canonical(projection), record_key),
        )
        corrupted_state = {
            "records": db.execute(
                "SELECT kind,key,payload FROM records ORDER BY kind,key"
            ).fetchall(),
            "events": db.execute(
                "SELECT seq,event_id,payload FROM events ORDER BY seq"
            ).fetchall(),
            "strict_proofs": db.execute(
                "SELECT * FROM strict_proofs ORDER BY proof_id"
            ).fetchall(),
            "strict_heads": db.execute(
                "SELECT * FROM strict_heads ORDER BY kind,goal_id,scope_digest,subject_id"
            ).fetchall(),
        }

    assert_strict_code(lambda: runtime.review_context(baseline.goal_id), "UNTRUSTED")
    assert calls.count(record_key) == 1

    with sqlite3.connect(runtime.store.path) as db:
        assert {
            "records": db.execute(
                "SELECT kind,key,payload FROM records ORDER BY kind,key"
            ).fetchall(),
            "events": db.execute(
                "SELECT seq,event_id,payload FROM events ORDER BY seq"
            ).fetchall(),
            "strict_proofs": db.execute(
                "SELECT * FROM strict_proofs ORDER BY proof_id"
            ).fetchall(),
            "strict_heads": db.execute(
                "SELECT * FROM strict_heads ORDER BY kind,goal_id,scope_digest,subject_id"
            ).fetchall(),
        } == corrupted_state


def _direction_head_key(goal_id: str, baseline_digest: str, decision_key: str) -> str:
    return fingerprint({
        "schema": "strict-direction-question-head/v1",
        "goal_id": goal_id,
        "baseline_digest": baseline_digest,
        "decision_key": decision_key,
        "kind": "DIRECTION",
    })


def test_strict_direction_question_is_signed_persisted_and_blocks_only_approved_items(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    baseline = replace(
        baseline,
        decision_dependencies={
            "clarify-delivery": ("item-1", "item-2"),
            "nonblocking-note": (),
        },
    )
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(
        baseline, blocking_items=("item-1", "item-2"),
    )
    generation_before = runtime.store.get("goal", baseline.goal_id)["generation"]
    before = strict_storage_rows(runtime)

    record = runtime.request_question(question)

    assert record == QuestionRecord(question, "WAITING", None)
    envelope = runtime.store.get("strict_question", question.question_id)
    assert envelope is not None
    assert envelope["goal_id"] == baseline.goal_id
    assert envelope["baseline_digest"] == baseline.fingerprint
    assert envelope["decision_key"] == question.decision_key
    assert envelope["record"] == record.to_dict()
    assert envelope["question_fingerprint"] == question.fingerprint
    assert envelope["question_record_fingerprint"] == fingerprint(record.to_dict())
    assert envelope["dedupe_key"] == question.dedupe_key
    assert envelope["runtime_binding_digest"] == runtime_digest(
        runtime_provider.current_runtime_binding(), runtime.policy
    )
    assert envelope["lease_generation"] == 1
    assert envelope["principal_id"] == "user-1"
    assert envelope["account_id"] == "account-1"
    assert envelope["session_id"] == "session-1"
    baseline_proof = runtime.store.read_strict_snapshot(baseline.goal_id)["baseline_proof"]
    assert baseline_proof is not None
    assert envelope["baseline_proof_id"] == baseline_proof["proof_id"]
    assert envelope["creation_event_id"]
    assert envelope["creation_event_digest"]

    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    head = runtime.store.get("strict_question_request_index", head_key)
    assert head == {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "decision_key": "clarify-delivery",
        "kind": "DIRECTION",
        "question_id": question.question_id,
        "question_revision": 1,
        "question_fingerprint": question.fingerprint,
        "dedupe_key": question.dedupe_key,
    }
    raw_event = runtime.store.get_event(envelope["creation_event_id"])
    assert raw_event is not None
    event = SignedEvent.from_dict(raw_event)
    assert event.kind == "controller.question.created"
    assert event.subject == question.fingerprint
    assert event.event_id == envelope["creation_event_id"]
    assert runtime.observer_authority.verify(event) == {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "baseline_proof_id": baseline_proof["proof_id"],
        "decision_key": question.decision_key,
        "head_key": head_key,
        "question_id": question.question_id,
        "question_revision": 1,
        "question_fingerprint": question.fingerprint,
        "question_record_fingerprint": fingerprint(record.to_dict()),
        "dedupe_key": question.dedupe_key,
        "expires_at": question.expires_at,
        "runtime_binding_digest": envelope["runtime_binding_digest"],
        "lease_generation": 1,
    }
    assert envelope["creation_event_digest"] == fingerprint(event.to_dict())
    assert runtime.store.get("goal", baseline.goal_id)["generation"] == generation_before + 1
    assert runtime.work_item_decision(baseline.goal_id, "item-1") == "WAIT"
    assert runtime.work_item_decision(baseline.goal_id, "item-2") == "WAIT"
    assert runtime.work_item_decision(baseline.goal_id, "unrelated-item") == "ALLOW"
    assert runtime.work_item_decision(baseline.goal_id, "nonblocking-work") == "ALLOW"
    assert strict_storage_rows(runtime) != before


def test_strict_direction_question_derives_bounded_expiry_when_omitted(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path, question_ttl_seconds=60)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline, expires_at=None)

    record = runtime.request_question(question)

    assert record.question.expires_at == "2026-10-16T01:01:00+08:00"
    assert record.state == "WAITING"


@pytest.mark.parametrize("ttl", [True, 0, -1, 86400.5, "60", 604801])
def test_strict_question_ttl_configuration_rejects_invalid_values(tmp_path, ttl):
    with pytest.raises(ContractValidationError):
        make_world(tmp_path, question_ttl_seconds=ttl)


@pytest.mark.parametrize("ttl", [1, 604800])
def test_strict_question_ttl_configuration_accepts_inclusive_bounds(tmp_path, ttl):
    runtime, *_ = make_world(tmp_path, question_ttl_seconds=ttl)
    assert runtime is not None


def test_strict_question_ttl_configuration_rejects_explicit_none(tmp_path):
    runtime, *_ = make_world(tmp_path)

    with pytest.raises(ContractValidationError):
        StrictWorkflow(
            runtime.store,
            runtime.user_authority,
            runtime.observer_authority,
            runtime.policy,
            trusted_event_verifier=runtime.trusted_event_verifier,
            trusted_runtime_binding_provider=runtime.runtime_binding_provider,
            question_ttl_seconds=None,
            clock=runtime.clock,
        )


def test_strict_direction_question_replay_returns_canonical_record_without_new_writes(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    first = runtime.request_question(question)
    after_first = strict_storage_rows(runtime)

    replay = runtime.request_question(replace(question, question_id="caller-retry-id"))

    assert replay == first
    assert replay.question.question_id == question.question_id
    assert strict_storage_rows(runtime) == after_first

    changed = replace(question, prompt="A different decision under the same Baseline key.")
    before_conflict = strict_storage_rows(runtime)
    assert_strict_code(lambda: runtime.request_question(changed), "CONFLICT")
    assert strict_storage_rows(runtime) == before_conflict


def test_strict_direction_question_rejects_invalid_contracts_without_writes(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(
        baseline,
        decision_dependencies={"clarify-delivery": ("item-1", "item-2")},
    )
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    valid = direction_question(baseline, blocking_items=("item-1", "item-2"))
    invalid_questions = (
        (replace(valid, trigger_reason=None), "UNTRUSTED"),
        (replace(valid, decision_key=None), "UNTRUSTED"),
        (replace(valid, decision_key="not-in-baseline"), "UNTRUSTED"),
        (replace(valid, blocking_items=("item-1",)), "UNTRUSTED"),
        (replace(valid, blocking_items=("item-2", "item-1")), "UNTRUSTED"),
        (replace(valid, question_revision=2), "UNTRUSTED"),
        (replace(valid, plan_revision=2), "STALE"),
        (replace(valid, operation_digest="op-1"), "UNTRUSTED"),
        (replace(valid, approval_option_id="direct"), "UNTRUSTED"),
        (replace(valid, baseline_digest="0" * 64), "STALE"),
        (replace(valid, expires_at="2026-10-16T00:59:00+08:00"), "STALE"),
        (replace(valid, expires_at="2026-10-17T01:01:00+08:00"), "UNTRUSTED"),
    )

    for invalid, expected_code in invalid_questions:
        before = strict_storage_rows(runtime)
        assert_strict_code(lambda question=invalid: runtime.request_question(question), expected_code)
        assert strict_storage_rows(runtime) == before


def test_strict_direction_question_requires_approved_dependency_map(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    before = strict_storage_rows(runtime)

    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_question(question)
    assert captured.value.code == "UNTRUSTED"

    assert strict_storage_rows(runtime) == before


def test_legacy_question_rows_do_not_block_strict_work_item_decisions(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.store.commit(
        [("question", "legacy-direction", {"goal_id": baseline.goal_id, "state": "WAIT"})],
        {("question", "legacy-direction"): None},
        "legacy-direction-event",
        {"kind": "legacy-question"},
    )

    assert runtime.work_item_decision(baseline.goal_id, "item-1") == "ALLOW"


def test_strict_direction_question_does_not_overwrite_an_existing_question_id(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(
        baseline,
        decision_dependencies={
            "clarify-delivery": ("item-1",),
            "clarify-release": ("item-2",),
        },
    )
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    first = direction_question(baseline, question_id="reused-question-id")
    runtime.request_question(first)
    conflicting_id = direction_question(
        baseline,
        question_id="reused-question-id",
        decision_key="clarify-release",
        blocking_items=("item-2",),
    )
    before = strict_storage_rows(runtime)

    assert_strict_code(lambda: runtime.request_question(conflicting_id), "CONFLICT")

    assert strict_storage_rows(runtime) == before


@pytest.mark.parametrize("changed_identity", ["generation", "host"])
def test_strict_direction_question_rejects_changed_baseline_runtime_binding(
    tmp_path, changed_identity
):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    if changed_identity == "generation":
        runtime_provider.rotate(generation=2)
    else:
        runtime_provider.rotate(host_binding=canonical({**HOST_BINDING_FACTS, "model": "model-next"}))
    before = strict_storage_rows(runtime)

    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_question(question)
    assert captured.value.code in {"STALE", "UNTRUSTED"}
    assert strict_storage_rows(runtime) == before
    assert_question_decision_fails_closed(lambda: runtime.work_item_decision(baseline.goal_id, "item-1"))


def test_strict_direction_question_with_empty_dependency_list_does_not_block_work(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"nonblocking-note": ()})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.request_question(
        direction_question(baseline, decision_key="nonblocking-note", blocking_items=())
    )

    assert runtime.work_item_decision(baseline.goal_id, "item-1") == "ALLOW"


def test_strict_direction_question_rejects_same_user_after_runtime_generation_changes(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime_provider.rotate(generation=2)
    before = strict_storage_rows(runtime)

    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_question(question)
    assert captured.value.code == "STALE"
    assert strict_storage_rows(runtime) == before
    assert_question_decision_fails_closed(lambda: runtime.work_item_decision(baseline.goal_id, "item-1"))


def test_strict_direction_question_rejects_same_user_after_host_binding_changes(tmp_path):
    runtime, baseline, _, _, provider, runtime_provider = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    changed_host = {**HOST_BINDING_FACTS, "model": "model-next"}
    runtime_provider.rotate(host_binding=canonical(changed_host))
    before = strict_storage_rows(runtime)

    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_question(question)
    assert captured.value.code in {"STALE", "UNTRUSTED"}
    assert strict_storage_rows(runtime) == before
    assert_question_decision_fails_closed(lambda: runtime.work_item_decision(baseline.goal_id, "item-1"))


def test_strict_direction_question_rejects_fabricated_answered_projection(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    assert envelope is not None
    fabricated = QuestionRecord(
        question,
        "ANSWERED",
        QuestionAnswer(
            "forged-answer",
            question.question_id,
            question.fingerprint,
            selected=("direct",),
            source_event_id="unsigned-user-answer",
        ),
    )
    envelope["record"] = fabricated.to_dict()
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "UPDATE records SET payload=? WHERE kind='strict_question' AND key=?",
            (canonical(envelope), question.question_id),
        )

    assert_question_decision_fails_closed(lambda: runtime.work_item_decision(baseline.goal_id, "item-1"))


@pytest.mark.parametrize(
    "tamper",
    ["envelope", "event_digest", "missing_event", "wrong_event", "head_mismatch", "wrong_head_key"],
)
def test_strict_direction_question_tampering_never_yields_allow(tmp_path, tamper):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    assert envelope is not None
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    head = runtime.store.get("strict_question_request_index", head_key)
    assert head is not None

    with sqlite3.connect(runtime.store.path) as db:
        if tamper == "envelope":
            envelope["record"]["question"]["prompt"] = "Changed after controller signature"
            db.execute(
                "UPDATE records SET payload=? WHERE kind='strict_question' AND key=?",
                (canonical(envelope), question.question_id),
            )
        elif tamper == "event_digest":
            envelope["creation_event_digest"] = "0" * 64
            db.execute(
                "UPDATE records SET payload=? WHERE kind='strict_question' AND key=?",
                (canonical(envelope), question.question_id),
            )
        elif tamper in {"missing_event", "wrong_event"}:
            if tamper == "missing_event":
                envelope["creation_event_id"] = "missing-controller-event"
                envelope["creation_event_digest"] = "0" * 64
            else:
                baseline_event_payload = runtime.store.get_event("baseline-event")
                assert baseline_event_payload is not None
                envelope["creation_event_id"] = "baseline-event"
                envelope["creation_event_digest"] = fingerprint(SignedEvent.from_dict(baseline_event_payload).to_dict())
            db.execute(
                "UPDATE records SET payload=? WHERE kind='strict_question' AND key=?",
                (canonical(envelope), question.question_id),
            )
        elif tamper == "head_mismatch":
            head["question_id"] = "missing-question"
            head["question_fingerprint"] = "0" * 64
            db.execute(
                "UPDATE records SET payload=? WHERE kind='strict_question_request_index' AND key=?",
                (canonical(head), head_key),
            )
        else:
            db.execute(
                "DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,)
            )
            db.execute(
                "INSERT INTO records(kind,key,payload) VALUES('strict_question_request_index',?,?)",
                ("wrong-index-key", canonical(head)),
            )
    corrupted = strict_storage_rows(runtime)

    assert_question_decision_fails_closed(lambda: runtime.work_item_decision(baseline.goal_id, "item-1"))

    assert strict_storage_rows(runtime) == corrupted


def test_coordinated_decision_head_rewrite_cannot_unblock_original_dependency(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(
        baseline,
        decision_dependencies={
            "decision-a": ("item-a",),
            "decision-b": ("item-b",),
        },
    )
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(
        baseline,
        decision_key="decision-a",
        blocking_items=("item-a",),
    )
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    old_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "decision-a")
    new_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "decision-b")
    head = runtime.store.get("strict_question_request_index", old_key)
    assert envelope is not None and head is not None
    envelope["decision_key"] = "decision-b"
    head["decision_key"] = "decision-b"
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "UPDATE records SET payload=? WHERE kind='strict_question' AND key=?",
            (canonical(envelope), question.question_id),
        )
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (old_key,))
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_question_request_index',?,?)",
            (new_key, canonical(head)),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-a")
    )

    assert strict_storage_rows(runtime) == corrupted


@pytest.mark.parametrize("field", ["question_id", "expires_at"])
def test_strict_question_envelope_duplicate_fields_match_signed_question(tmp_path, field):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    assert envelope is not None
    if field == "question_id":
        envelope["question_id"] = "outer-only-question-id"
        row_key = "outer-only-question-id"
        with sqlite3.connect(runtime.store.path) as db:
            db.execute(
                "UPDATE records SET key=?,payload=? WHERE kind='strict_question' AND key=?",
                (row_key, canonical(envelope), question.question_id),
            )
    else:
        envelope["expires_at"] = "2026-10-16T01:30:00+08:00"
        with sqlite3.connect(runtime.store.path) as db:
            db.execute(
                "UPDATE records SET payload=? WHERE kind='strict_question' AND key=?",
                (canonical(envelope), question.question_id),
            )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_orphan_question_with_tampered_outer_owner_cannot_be_hidden(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(
        baseline,
        decision_dependencies={"clarify-delivery": ("item-1",), "nonblocking-note": ()},
    )
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    valid_question = direction_question(
        baseline, decision_key="nonblocking-note", blocking_items=()
    )
    runtime.request_question(valid_question)
    valid_envelope = runtime.store.get("strict_question", valid_question.question_id)
    assert valid_envelope is not None
    orphan_question = direction_question(
        baseline,
        question_id="orphan-with-tampered-owner",
        decision_key="clarify-delivery",
        blocking_items=("item-1",),
    )
    orphan_record = QuestionRecord(orphan_question, "WAITING", None)
    orphan = {
        **valid_envelope,
        "goal_id": "another-goal",
        "decision_key": "clarify-delivery",
        "head_key": _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery"),
        "question_id": orphan_question.question_id,
        "question_fingerprint": orphan_question.fingerprint,
        "question_record_fingerprint": fingerprint(orphan_record.to_dict()),
        "dedupe_key": orphan_question.dedupe_key,
        "record": orphan_record.to_dict(),
        "creation_event_id": "missing-orphan-event",
        "creation_event_digest": "0" * 64,
    }
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_question',?,?)",
            (orphan_question.question_id, canonical(orphan)),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_orphan_current_baseline_head_with_wrong_owner_cannot_be_ignored(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    orphan_head = {
        "goal_id": "another-goal",
        "baseline_digest": baseline.fingerprint,
        "decision_key": "clarify-delivery",
        "kind": "DIRECTION",
        "question_id": "missing-question-record",
        "question_revision": 1,
        "question_fingerprint": "0" * 64,
        "dedupe_key": "1" * 64,
    }
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_question_request_index',?,?)",
            (head_key, canonical(orphan_head)),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_orphan_head_under_active_key_is_not_hidden_by_rewriting_owner_and_digest(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    active_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    hidden_head = {
        "goal_id": "another-goal",
        "baseline_digest": "0" * 64,
        "decision_key": "clarify-delivery",
        "kind": "DIRECTION",
        "question_id": "missing-question-record",
        "question_revision": 1,
        "question_fingerprint": "0" * 64,
        "dedupe_key": "1" * 64,
    }
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_question_request_index',?,?)",
            (active_key, canonical(hidden_head)),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


@pytest.mark.parametrize("malformed_location", ["outer", "inner"])
def test_malformed_question_id_json_never_escapes_as_raw_type_error(tmp_path, malformed_location):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    malformed_question = {
        "question_id": "malformed-question",
        "goal_id": baseline.goal_id,
        "plan_revision": baseline.revision,
        "title": "Malformed row",
        "prompt": "This record is not a valid Question",
        "options": [],
        "multi_select": False,
        "allow_custom": False,
        "blocking_items": ["item-1"],
        "kind": "DIRECTION",
        "operation_digest": None,
        "approval_option_id": None,
        "question_revision": 1,
        "baseline_digest": baseline.fingerprint,
        "decision_key": "clarify-delivery",
        "trigger_reason": "Malformed row test",
        "expires_at": "2026-10-16T02:00:00+08:00",
        "authorization_context_digest": None,
    }
    envelope = {
        "goal_id": baseline.goal_id,
        "baseline_digest": baseline.fingerprint,
        "question_id": "malformed-question",
        "record": {"question": malformed_question, "state": "WAITING", "answer": None},
    }
    if malformed_location == "outer":
        envelope["question_id"] = []
    else:
        envelope["record"]["question"]["question_id"] = []
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_question',?,?)",
            ("malformed-question", canonical(envelope)),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )
    assert strict_storage_rows(runtime) == corrupted


def test_strict_work_item_decision_rejects_noncanonical_padded_id(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.request_question(direction_question(baseline))

    assert_strict_code(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1 "),
        "UNTRUSTED",
    )


def test_orphan_creation_event_with_whitespace_json_reformatting_still_blocks(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    assert envelope is not None
    event_id = envelope["creation_event_id"]
    raw_event = runtime.store.get_event(event_id)
    assert raw_event is not None
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_question' AND key=?", (question.question_id,))
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,))
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (json.dumps(raw_event, ensure_ascii=False, sort_keys=True, indent=2), event_id),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_orphan_creation_event_with_tampered_baseline_digest_is_verified_before_filtering(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    assert envelope is not None
    event_id = envelope["creation_event_id"]
    raw_event = runtime.store.get_event(event_id)
    assert raw_event is not None
    raw_event["payload"]["baseline_digest"] = "0" * 64
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_question' AND key=?", (question.question_id,))
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,))
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (canonical(raw_event), event_id),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_orphan_creation_event_with_tampered_kind_is_not_filtered_before_verification(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    assert envelope is not None
    event_id = envelope["creation_event_id"]
    raw_event = runtime.store.get_event(event_id)
    assert raw_event is not None
    raw_event["kind"] = "controller.other"
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_question' AND key=?", (question.question_id,))
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,))
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (canonical(raw_event), event_id),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_orphan_creation_event_with_tampered_kind_and_head_key_cannot_be_hidden(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    assert envelope is not None
    event_id = envelope["creation_event_id"]
    raw_event = runtime.store.get_event(event_id)
    assert raw_event is not None
    raw_event["kind"] = "controller.other"
    raw_event["payload"]["head_key"] = "not-an-active-question-head"
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_question' AND key=?", (question.question_id,))
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,))
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (canonical(raw_event), event_id),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_orphan_creation_event_with_tampered_kind_head_and_decision_still_blocks(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    assert envelope is not None
    event_id = envelope["creation_event_id"]
    raw_event = runtime.store.get_event(event_id)
    assert raw_event is not None
    raw_event["kind"] = "controller.other"
    raw_event["payload"]["head_key"] = "not-an-active-question-head"
    raw_event["payload"]["decision_key"] = "not-an-approved-decision"
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_question' AND key=?", (question.question_id,))
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,))
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (canonical(raw_event), event_id),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_orphan_creation_event_with_all_active_claims_tampered_cannot_be_hidden(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    assert envelope is not None
    event_id = envelope["creation_event_id"]
    raw_event = runtime.store.get_event(event_id)
    assert raw_event is not None
    raw_event["kind"] = "controller.other"
    raw_event["subject"] = "tampered-question-subject"
    payload = raw_event["payload"]
    payload["goal_id"] = "other-goal"
    payload["baseline_digest"] = "0" * 64
    payload["baseline_proof_id"] = "tampered-baseline-proof"
    payload["head_key"] = "not-an-active-question-head"
    payload["decision_key"] = "not-an-approved-decision"
    payload["question_id"] = ""
    payload["question_fingerprint"] = "0" * 64
    payload["question_revision"] = 99
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_question' AND key=?", (question.question_id,))
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,))
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (canonical(raw_event), event_id),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_orphan_creation_event_with_removed_signature_cannot_be_hidden(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    assert envelope is not None
    event_id = envelope["creation_event_id"]
    raw_event = runtime.store.get_event(event_id)
    assert raw_event is not None
    raw_event["kind"] = "controller.other"
    raw_event.pop("signature")
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_question' AND key=?", (question.question_id,))
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,))
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (canonical(raw_event), event_id),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


@pytest.mark.parametrize("expired_kind", ["baseline", "question"])
def test_expired_strict_baseline_or_question_never_yields_allow(tmp_path, expired_kind):
    clock_value = [NOW]
    runtime, baseline, _, _, provider, _ = make_world(tmp_path, clock=lambda: clock_value[0])
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    baseline_expiry = "2026-10-16T01:00:10+08:00"
    baseline_event_value = baseline_event(runtime, baseline)
    register_baseline(
        runtime,
        baseline,
        baseline_event_value,
        provider,
        expires_at=baseline_expiry if expired_kind == "baseline" else LATER,
    )
    runtime.start_baseline(baseline, baseline_event_value)
    question_expiry = "2026-10-16T01:00:20+08:00"
    runtime.request_question(direction_question(baseline, expires_at=question_expiry))
    clock_value[0] = baseline_expiry if expired_kind == "baseline" else question_expiry

    assert_question_decision_fails_closed(lambda: runtime.work_item_decision(baseline.goal_id, "item-1"))


def test_strict_question_expiry_while_waiting_for_writer_lock_leaves_no_question_writes(
    tmp_path, monkeypatch
):
    clock_value = [NOW]
    runtime, baseline, _, _, provider, _ = make_world(tmp_path, clock=lambda: clock_value[0])
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline, expires_at="2026-10-16T01:00:10+08:00")
    before = strict_storage_rows(runtime)
    entered_commit = threading.Event()
    original_commit = runtime.store.commit_strict_question

    def signal_then_commit(*args, **kwargs):
        entered_commit.set()
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "commit_strict_question", signal_then_commit)
    lock = sqlite3.connect(runtime.store.path, timeout=3, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    results: list[BaseException | QuestionRecord] = []
    done = threading.Event()

    def request() -> None:
        try:
            results.append(runtime.request_question(question))
        except BaseException as exc:
            results.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=request)
    worker.start()
    try:
        assert entered_commit.wait(timeout=3)
        clock_value[0] = question.expires_at
    finally:
        lock.rollback()
        lock.close()
    worker.join(timeout=3)

    assert done.is_set()
    assert len(results) == 1 and isinstance(results[0], StrictWorkflowError)
    assert results[0].code == "STALE"
    assert strict_storage_rows(runtime) == before


def test_strict_work_item_decision_detects_active_baseline_creation_event_without_question_or_head(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, question.decision_key)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_question' AND key=?", (question.question_id,))
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,))
    corrupted = strict_storage_rows(runtime)

    assert_question_decision_fails_closed(lambda: runtime.work_item_decision(baseline.goal_id, "item-1"))

    assert strict_storage_rows(runtime) == corrupted


def test_strict_work_item_decision_detects_question_without_creation_event_or_head(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(
        baseline,
        decision_dependencies={"clarify-delivery": ("item-1",), "nonblocking-note": ()},
    )
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(
        baseline, decision_key="nonblocking-note", blocking_items=(),
    )
    runtime.request_question(question)
    original = runtime.store.get("strict_question", question.question_id)
    assert original is not None
    orphan_question = direction_question(
        baseline, question_id="orphan-question", decision_key="clarify-delivery", blocking_items=("item-1",),
    )
    orphan = {
        **original,
        "record": QuestionRecord(orphan_question, "WAITING", None).to_dict(),
        "question_fingerprint": orphan_question.fingerprint,
        "question_record_fingerprint": fingerprint(QuestionRecord(orphan_question, "WAITING", None).to_dict()),
        "dedupe_key": orphan_question.dedupe_key,
        "creation_event_id": "missing-question-event",
        "creation_event_digest": "0" * 64,
    }
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO records(kind,key,payload) VALUES('strict_question',?,?)",
            (orphan_question.question_id, canonical(orphan)),
        )
    corrupted = strict_storage_rows(runtime)

    assert_question_decision_fails_closed(lambda: runtime.work_item_decision(baseline.goal_id, "item-1"))

    assert strict_storage_rows(runtime) == corrupted


@pytest.mark.parametrize("different_payload", [False, True])
def test_strict_direction_question_concurrent_requests_have_one_canonical_head(
    tmp_path, different_payload
):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    barrier = threading.Barrier(2)
    verification_lock = threading.Lock()
    synchronized_verifications = [0]

    def pause_baseline_proof(event: SignedEvent, purpose: str, _binding: dict[str, Any]) -> None:
        if purpose == "user.baseline":
            with verification_lock:
                synchronized_verifications[0] += 1
                synchronize = synchronized_verifications[0] <= 2
            if synchronize:
                barrier.wait(timeout=3)

    provider.on_verify = pause_baseline_proof
    first = direction_question(baseline, question_id="concurrent-question-a")
    second = direction_question(
        baseline,
        question_id="concurrent-question-b",
        prompt=("A competing request with different content." if different_payload else first.prompt),
    )
    generation_before = runtime.store.get("goal", baseline.goal_id)["generation"]

    def request(question):
        try:
            return runtime.request_question(question)
        except StrictWorkflowError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request, (first, second)))

    stored_questions = runtime.store.list("strict_question", baseline.goal_id)
    index_rows = runtime.store.list("strict_question_request_index", baseline.goal_id)
    creation_events = [
        item["payload"]
        for item in runtime.store.audit()
        if isinstance(item.get("payload"), dict)
        and item["payload"].get("kind") == "controller.question.created"
    ]
    assert len(stored_questions) == 1
    assert len(index_rows) == 1
    assert len(creation_events) == 1
    assert runtime.store.get("goal", baseline.goal_id)["generation"] == generation_before + 1
    if different_payload:
        assert sum(isinstance(result, QuestionRecord) for result in results) == 1
        failures = [result for result in results if isinstance(result, StrictWorkflowError)]
        assert len(failures) == 1 and failures[0].code == "CONFLICT"
    else:
        assert results[0] == results[1]


def test_strict_question_snapshot_read_racing_with_replan_is_one_consistent_read(tmp_path, monkeypatch):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    old_baseline = replace(
        baseline,
        decision_dependencies={"clarify-delivery": ("item-1",)},
    )
    write_baseline(runtime, old_baseline, baseline_event(runtime, old_baseline), provider)
    old_question = direction_question(old_baseline)
    runtime.request_question(old_question)
    old_generation = runtime.store.get("goal", old_baseline.goal_id)["generation"]
    original_reader = runtime.store._read_strict_baseline_snapshot
    baseline_read = threading.Event()
    continue_read = threading.Event()
    read_thread_id: list[int] = []

    def pause_after_baseline_snapshot(db, goal_id):
        snapshot = original_reader(db, goal_id)
        if read_thread_id and threading.get_ident() == read_thread_id[0]:
            baseline_read.set()
            assert continue_read.wait(timeout=3)
        return snapshot

    monkeypatch.setattr(runtime.store, "_read_strict_baseline_snapshot", pause_after_baseline_snapshot)
    snapshots: list[dict[str, Any]] = []
    failures: list[BaseException] = []

    def read_snapshot() -> None:
        read_thread_id.append(threading.get_ident())
        try:
            snapshots.append(runtime.store.read_strict_question_snapshot(old_baseline.goal_id))
        except BaseException as exc:
            failures.append(exc)

    reader = threading.Thread(target=read_snapshot)
    reader.start()
    assert baseline_read.wait(timeout=3)
    new_baseline = replace(
        old_baseline,
        revision=2,
        objective="Deliver the artifact by a staged route",
        decision_dependencies={"clarify-delivery": ("item-2",)},
    )
    new_event = baseline_event(runtime, new_baseline, event_id="baseline-event-replanned")
    register_baseline(runtime, new_baseline, new_event, provider)
    runtime.start_baseline(new_baseline, new_event)
    runtime.request_question(
        direction_question(
            new_baseline,
            question_id="question-after-replan",
            decision_key="clarify-delivery",
            blocking_items=("item-2",),
        )
    )
    continue_read.set()
    reader.join(timeout=3)

    assert not failures
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot["goal"]["generation"] == old_generation
    assert snapshot["baseline_digest"] == old_baseline.fingerprint
    assert {
        item["envelope"]["baseline_digest"] for item in snapshot["questions"]
    } == {old_baseline.fingerprint}
    assert len(snapshot["question_heads"]) == 1


def test_strict_question_event_id_collision_fails_after_expected_state_validation(tmp_path, monkeypatch):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    collision_event = runtime.observer_authority.sign(
        "controller.unrelated", "reserved", {}, event_id="question-event-collision", at=NOW
    )
    runtime.store.commit(
        [("unrelated", "event-collision", {"goal_id": "other-goal"})],
        {("unrelated", "event-collision"): None},
        collision_event.event_id,
        collision_event.to_dict(),
    )
    original_sign = runtime.observer_authority.sign

    def collide_question_event(kind, subject, payload, *, event_id=None, at=None):
        if kind == "controller.question.created":
            event_id = collision_event.event_id
        return original_sign(kind, subject, payload, event_id=event_id, at=at)

    monkeypatch.setattr(runtime.observer_authority, "sign", collide_question_event)
    before = strict_storage_rows(runtime)

    with pytest.raises(StrictWorkflowError) as captured:
        runtime.request_question(direction_question(baseline))
    assert captured.value.code == "CONFLICT"

    assert strict_storage_rows(runtime) == before


def test_strict_question_snapshot_is_read_only_and_contains_all_active_rows(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(
        baseline,
        decision_dependencies={"clarify-delivery": ("item-1",), "clarify-other": ("item-2",)},
    )
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    first = direction_question(baseline)
    second = direction_question(
        baseline,
        question_id="question-2",
        decision_key="clarify-other",
        blocking_items=("item-2",),
    )
    runtime.request_question(first)
    runtime.request_question(second)
    before = strict_storage_rows(runtime)

    snapshot = runtime.store.read_strict_question_snapshot(baseline.goal_id)

    assert snapshot["goal"]["goal_id"] == baseline.goal_id
    assert snapshot["baseline_digest"] == baseline.fingerprint
    assert len(snapshot["question_heads"]) == 2
    assert {row["envelope"]["question_id"] for row in snapshot["questions"]} == {"question-1", "question-2"}
    assert len(snapshot["question_events"]) == 2
    assert strict_storage_rows(runtime) == before


def test_orphan_creation_event_rekeyed_as_store_journal_is_untrusted(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    question = direction_question(baseline)
    runtime.request_question(question)
    envelope = runtime.store.get("strict_question", question.question_id)
    head_key = _direction_head_key(baseline.goal_id, baseline.fingerprint, "clarify-delivery")
    assert envelope is not None
    event_id = envelope["creation_event_id"]
    raw_event = runtime.store.get_event(event_id)
    assert raw_event is not None
    raw_event["kind"] = "controller.other"
    raw_event.pop("signature")
    rekeyed_event_id = "store:rekeyed-question-event"
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DELETE FROM records WHERE kind='strict_question' AND key=?", (question.question_id,))
        db.execute("DELETE FROM records WHERE kind='strict_question_request_index' AND key=?", (head_key,))
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET event_id=?,payload=? WHERE event_id=?",
            (rekeyed_event_id, canonical(raw_event), event_id),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


@pytest.mark.parametrize("action", [[], {}], ids=["list", "object"])
def test_store_journal_unhashable_action_is_untrusted_without_writes(tmp_path, action):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO events(event_id,payload) VALUES(?,?)",
            (
                "store:malformed-action",
                canonical({"action": action, "record": {"record_id": "r-1"}, "at": NOW}),
            ),
        )
    before = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == before


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [
        ("_STRICT_EVENT_HISTORY_MAX_ROWS", 1),
        ("_STRICT_EVENT_HISTORY_MAX_BYTES", 1),
        ("_STRICT_EVENT_HISTORY_MAX_ROW_BYTES", 1),
    ],
)
def test_event_history_limits_run_before_baseline_projection_read(
    tmp_path, monkeypatch, limit_name, limit_value
):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    event_payload = canonical({"kind": "legacy-padding", "payload": "x" * 4096})
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO events(event_id,payload) VALUES(?,?)",
            ("preflight-padding-event", event_payload),
        )
    for name, default in (
        ("_STRICT_EVENT_HISTORY_MAX_ROWS", 100_000),
        ("_STRICT_EVENT_HISTORY_MAX_BYTES", 64 * 1024 * 1024),
        ("_STRICT_EVENT_HISTORY_MAX_ROW_BYTES", 16 * 1024 * 1024),
    ):
        monkeypatch.setattr(strict_store_module, name, default, raising=False)
    monkeypatch.setattr(strict_store_module, limit_name, limit_value, raising=False)
    before = strict_storage_rows(runtime)
    baseline_read_called = False
    original = runtime.store._read_strict_baseline_snapshot

    def record_baseline_read(db, goal_id):
        nonlocal baseline_read_called
        baseline_read_called = True
        return original(db, goal_id)

    monkeypatch.setattr(runtime.store, "_read_strict_baseline_snapshot", record_baseline_read)

    assert_strict_code(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1"),
        "UNAVAILABLE",
    )

    assert baseline_read_called is False
    assert strict_storage_rows(runtime) == before


def test_oversized_evidence_event_is_rejected_before_snapshot_decode(tmp_path, monkeypatch):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    evidence = EvidenceSnapshot.capture(
        "preflight-evidence", "artifact.txt", b"x" * 8192,
        source="trusted-runner", collected_at=NOW,
    )
    event = observer.sign(
        "observer.evidence",
        evidence.fingerprint,
        {"goal_id": baseline.goal_id, "snapshot": evidence.to_dict()},
        event_id="preflight-evidence-event",
        at=NOW,
    )
    register_evidence(runtime, baseline, evidence, event, provider)
    runtime.add_evidence(baseline.goal_id, evidence, event)
    with sqlite3.connect(runtime.store.path) as db:
        row = db.execute(
            "SELECT length(CAST(payload AS BLOB)) FROM events WHERE event_id=?", (event.event_id,)
        ).fetchone()
    assert row is not None and type(row[0]) is int and row[0] > 1
    for name, default in (
        ("_STRICT_EVENT_HISTORY_MAX_ROWS", 100_000),
        ("_STRICT_EVENT_HISTORY_MAX_BYTES", 64 * 1024 * 1024),
        ("_STRICT_EVENT_HISTORY_MAX_ROW_BYTES", 16 * 1024 * 1024),
    ):
        monkeypatch.setattr(strict_store_module, name, default, raising=False)
    monkeypatch.setattr(strict_store_module, "_STRICT_EVENT_HISTORY_MAX_ROW_BYTES", row[0] - 1, raising=False)
    decoded = False
    original_from_dict = EvidenceSnapshot.from_dict.__func__

    def record_snapshot_decode(cls, value):
        nonlocal decoded
        decoded = True
        return original_from_dict(cls, value)

    monkeypatch.setattr(EvidenceSnapshot, "from_dict", classmethod(record_snapshot_decode))
    before = strict_storage_rows(runtime)

    assert_strict_code(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1"),
        "UNAVAILABLE",
    )

    assert decoded is False
    assert strict_storage_rows(runtime) == before


@pytest.mark.parametrize("failure_stage", ["lookup", "insert"])
def test_event_signature_cache_failure_before_question_commit_writes_nothing(
    tmp_path, failure_stage
):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)

    class FailingCache:
        def get(self, key, default=None):
            if failure_stage == "lookup":
                raise RuntimeError("injected cache lookup failure")
            return default

        def __setitem__(self, key, value):
            if failure_stage == "insert":
                raise RuntimeError("injected cache insertion failure")

    runtime._event_signature_cache = FailingCache()
    before = strict_storage_rows(runtime)

    assert_strict_code(
        lambda: runtime.request_question(direction_question(baseline)),
        "UNAVAILABLE",
    )

    assert strict_storage_rows(runtime) == before


def test_event_authority_source_and_key_are_immutable_after_construction():
    authority = EventAuthority(b"immutable-key" * 3, "trusted-controller")

    with pytest.raises(AttributeError):
        authority.source = "rotated-source"
    with pytest.raises(AttributeError):
        authority._key = b"rotated-key" * 4


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0123456789abcdef" * 4, True),
        ("0" * 64, True),
        ("A" * 64, False),
        ("g" * 64, False),
        ("0" * 63, False),
        ("0" * 65, False),
        ("0" * 31 + " " + "0" * 32, False),
        ("０" * 64, False),
        (b"0" * 64, False),
        (None, False),
    ],
    ids=[
        "mixed-lower-hex", "all-digits", "uppercase", "non-hex", "short",
        "long", "whitespace", "unicode", "bytes", "none",
    ],
)
def test_canonical_event_digest_format_is_exact(value, expected):
    assert strict_workflow_module._is_canonical_event_digest(value) is expected


def test_malformed_transient_event_digest_fails_closed_without_writes(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.request_question(direction_question(baseline))

    snapshot, _goal, active_baseline, baseline_proof, binding = runtime._read_verified_question_context(
        baseline.goal_id
    )
    event_history = list(snapshot["event_history"])
    first_event = dict(event_history[0])
    first_event["payload_digest"] = "A" * 64
    event_history[0] = first_event
    snapshot["event_history"] = event_history
    before = strict_storage_rows(runtime)

    assert_strict_code(
        lambda: runtime._verify_strict_question_state(snapshot, active_baseline, baseline_proof, binding),
        "UNTRUSTED",
    )

    assert strict_storage_rows(runtime) == before


@pytest.mark.parametrize("changed_field", ["seq", "event_id"])
def test_signed_event_decode_cache_key_binds_sequence_and_row_id(
    tmp_path, monkeypatch, changed_field
):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.request_question(direction_question(baseline))
    unrelated = observer.sign(
        "observer.audit", "cache-key-binding", {"note": "row identity"},
        event_id="cache-key-binding-event", at=NOW,
    )
    runtime.store.commit_many([], {}, ((unrelated.event_id, unrelated.to_dict()),))
    assert runtime.work_item_decision(baseline.goal_id, "item-1") == "WAIT"
    key = next(
        cache_key for cache_key in runtime.store._strict_event_decode_cache
        if cache_key[1] == unrelated.event_id
    )

    parse_count = 0
    original_from_dict = SignedEvent.from_dict.__func__

    def count_target_parse(cls, payload):
        nonlocal parse_count
        if isinstance(payload, dict) and payload.get("event_id") == unrelated.event_id:
            parse_count += 1
        return original_from_dict(cls, payload)

    monkeypatch.setattr(SignedEvent, "from_dict", classmethod(count_target_parse))
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DROP TRIGGER events_no_update")
        if changed_field == "seq":
            db.execute(
                "UPDATE events SET seq=? WHERE event_id=?",
                (key[0] + 1_000_000, unrelated.event_id),
            )
        else:
            db.execute(
                "UPDATE events SET event_id=? WHERE event_id=?",
                ("changed-cache-key-row-id", unrelated.event_id),
            )
    before = strict_storage_rows(runtime)

    if changed_field == "seq":
        assert runtime.work_item_decision(baseline.goal_id, "item-1") == "WAIT"
    else:
        assert_untrusted_question_decision(
            lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
        )

    assert parse_count >= 1
    assert strict_storage_rows(runtime) == before


def test_signed_event_decode_cache_evicts_least_recently_used(tmp_path, monkeypatch):
    monkeypatch.setattr(strict_store_module, "_STRICT_EVENT_DECODE_CACHE_MAX_ENTRIES", 2, raising=False)
    monkeypatch.setattr(strict_store_module, "_STRICT_EVENT_DECODE_CACHE_MAX_BYTES", 1024 * 1024, raising=False)
    store = WorkflowStore(tmp_path / "decode-cache-lru.db")
    authority = EventAuthority(b"lru-test-key" * 3, "trusted-runner")
    events = [
        authority.sign(
            "observer.audit", f"lru-{index}", {"index": index},
            event_id=f"lru-event-{index}", at=NOW,
        )
        for index in range(3)
    ]
    digests = ["0" * 63 + str(index) for index in range(3)]
    sizes = [len(canonical(event.to_dict()).encode("utf-8")) for event in events]

    for index in range(2):
        store._cache_strict_source_event(index + 1, events[index].event_id, digests[index], events[index], sizes[index])
    assert store._cached_strict_source_event(1, events[0].event_id, digests[0]) is events[0]
    store._cache_strict_source_event(3, events[2].event_id, digests[2], events[2], sizes[2])

    assert store._cached_strict_source_event(1, events[0].event_id, digests[0]) is events[0]
    assert store._cached_strict_source_event(2, events[1].event_id, digests[1]) is None
    assert store._cached_strict_source_event(3, events[2].event_id, digests[2]) is events[2]
    assert len(store._strict_event_decode_cache) == 2
    assert store._strict_event_decode_cache_bytes == sizes[0] + sizes[2]


def test_warm_decision_reuses_cached_signed_event_model(tmp_path, monkeypatch):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.request_question(direction_question(baseline))
    unrelated = observer.sign(
        "observer.audit", "audit-subject", {"note": "unchanged"},
        event_id="warm-decode-cache-event", at=NOW,
    )
    runtime.store.commit_many([], {}, ((unrelated.event_id, unrelated.to_dict()),))

    parse_count = 0
    original_from_dict = SignedEvent.from_dict.__func__

    def count_target_parse(cls, payload):
        nonlocal parse_count
        if isinstance(payload, dict) and payload.get("event_id") == unrelated.event_id:
            parse_count += 1
        return original_from_dict(cls, payload)

    monkeypatch.setattr(SignedEvent, "from_dict", classmethod(count_target_parse))

    assert runtime.work_item_decision(baseline.goal_id, "item-1") == "WAIT"
    first_call_parse_count = parse_count
    assert first_call_parse_count >= 1

    assert runtime.work_item_decision(baseline.goal_id, "item-1") == "WAIT"
    assert parse_count == first_call_parse_count


@pytest.mark.parametrize(
    ("max_entries", "max_bytes"),
    [(2, 4 * 1024 * 1024), (100, 1)],
    ids=["entry-bound", "byte-bound"],
)
def test_signed_event_decode_cache_respects_entry_and_byte_limits(
    tmp_path, monkeypatch, max_entries, max_bytes
):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    monkeypatch.setattr(strict_store_module, "_STRICT_EVENT_DECODE_CACHE_MAX_ENTRIES", max_entries, raising=False)
    monkeypatch.setattr(strict_store_module, "_STRICT_EVENT_DECODE_CACHE_MAX_BYTES", max_bytes, raising=False)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.request_question(direction_question(baseline))
    unrelated = observer.sign(
        "observer.audit", "audit-subject", {"note": "bounded"},
        event_id="bounded-decode-cache-event", at=NOW,
    )
    runtime.store.commit_many([], {}, ((unrelated.event_id, unrelated.to_dict()),))

    assert runtime.work_item_decision(baseline.goal_id, "item-1") == "WAIT"

    assert len(runtime.store._strict_event_decode_cache) <= max_entries
    assert runtime.store._strict_event_decode_cache_bytes <= max_bytes


@pytest.mark.parametrize("failure_stage", ["lookup", "insert"])
def test_signed_event_decode_cache_failure_is_unavailable_without_writes(
    tmp_path, failure_stage
):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)

    class FailingCache:
        def get(self, key, default=None):
            if failure_stage == "lookup":
                raise RuntimeError("injected decode-cache lookup failure")
            return default

        def __setitem__(self, key, value):
            if failure_stage == "insert":
                raise RuntimeError("injected decode-cache insertion failure")

    runtime.store._strict_event_decode_cache = FailingCache()
    before = strict_storage_rows(runtime)

    assert_strict_code(
        lambda: runtime.request_question(direction_question(baseline)),
        "UNAVAILABLE",
    )

    assert strict_storage_rows(runtime) == before


def test_cached_signature_does_not_survive_event_bytes_change(tmp_path):
    runtime, baseline, _, observer, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.request_question(direction_question(baseline))
    unrelated = observer.sign(
        "observer.audit", "audit-subject", {"note": "unchanged"},
        event_id="unrelated-signed-history-event", at=NOW,
    )
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "INSERT INTO events(event_id,payload) VALUES(?,?)",
            (unrelated.event_id, canonical(unrelated.to_dict())),
        )

    assert runtime.work_item_decision(baseline.goal_id, "item-1") == "WAIT"
    tampered = runtime.store.get_event(unrelated.event_id)
    assert tampered is not None
    tampered["subject"] = "evil-subject!"
    assert len(canonical(tampered).encode("utf-8")) == len(canonical(unrelated.to_dict()).encode("utf-8"))
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (canonical(tampered), unrelated.event_id),
        )
    corrupted = strict_storage_rows(runtime)

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )

    assert strict_storage_rows(runtime) == corrupted


def test_replacing_event_authority_does_not_reuse_warm_signature_cache(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.request_question(direction_question(baseline))
    assert runtime.work_item_decision(baseline.goal_id, "item-1") == "WAIT"
    runtime.observer_authority = EventAuthority(b"replacement-observer-key" * 2, "trusted-runner")

    assert_untrusted_question_decision(
        lambda: runtime.work_item_decision(baseline.goal_id, "item-1")
    )


def test_concurrent_strict_decisions_share_signature_cache_safely(tmp_path):
    runtime, baseline, _, _, provider, _ = make_world(tmp_path)
    baseline = replace(baseline, decision_dependencies={"clarify-delivery": ("item-1",)})
    write_baseline(runtime, baseline, baseline_event(runtime, baseline), provider)
    runtime.request_question(direction_question(baseline))
    before = strict_storage_rows(runtime)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(
            lambda _index: runtime.work_item_decision(baseline.goal_id, "item-1"), range(16)
        ))

    assert results == ["WAIT"] * 16
    assert strict_storage_rows(runtime) == before
