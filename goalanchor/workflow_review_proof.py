"""Admission and durable verification for provider-backed Strict review proofs.

Provider objects are trusted composition-root services. This module does not
turn unsigned host logs or model assertions into signed native evidence.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .core import ContractValidationError
from .review_delivery import build_packets, validate_receipts
from .review_proof import ReviewProofExpired, normalize_review_proof
from .review_service import ArbiterResult, MetaReviewResult, ReviewContext, ReviewerResult, evaluate_review_cycle
from .workflow_contracts import Payload, StrictWorkflowError, TrustedProofRejected, TrustedProofUnavailable
from .workflow_protocol import SignedEvent, fingerprint, parse_time
from .workflow_store import WorkflowStoreConflict

if TYPE_CHECKING:
    from .workflow_strict import StrictWorkflow


def attestation_id(event_id: str) -> str:
    return "review-attestation:" + fingerprint({"source_event_id": event_id})


def verify_provider(
    runtime: StrictWorkflow, bundle: Payload, context: ReviewContext, packets: dict[str, Payload],
) -> Payload:
    verifier = runtime.trusted_review_verifier
    if verifier is None:
        raise runtime._strict_error("UNAVAILABLE", "Trusted review proof provider is required")
    try:
        raw = verifier.verify(deepcopy(bundle), expected_policy_digest=runtime.policy.fingerprint,
                              expected_context=context, expected_host_binding=runtime.policy.host_binding)
    except TrustedProofRejected as exc:
        raise runtime._strict_error("UNTRUSTED", "Trusted review proof was rejected") from exc
    except (TrustedProofUnavailable, TimeoutError, ConnectionError) as exc:
        raise runtime._strict_error("UNAVAILABLE", "Trusted review proof provider is unavailable") from exc
    except Exception as exc:
        raise runtime._strict_error("UNAVAILABLE", "Trusted review proof verification failed") from exc
    return normalize(runtime, raw, bundle, context, packets)


def normalize(runtime: StrictWorkflow, raw: Any, bundle: Payload, context: ReviewContext,
              packets: dict[str, Payload]) -> Payload:
    try:
        return normalize_review_proof(
            raw, bundle, expected_context=context, expected_policy_digest=runtime.policy.fingerprint,
            expected_host_binding=runtime.policy.host_binding, expected_packets=packets, now=runtime.clock(),
        )
    except ReviewProofExpired as exc:
        raise runtime._strict_error("STALE", "Strict review proof expired") from exc
    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
        raise runtime._strict_error("UNTRUSTED", "Strict review proof is malformed or unbound") from exc


def require_fresh_sources(runtime: StrictWorkflow, proof: Payload, prior: list[Payload]) -> None:
    for field in ("session_id", "input_id", "turn_id", "source_event_id"):
        used = {role[field] for previous in prior for role in previous["roles"]}
        if any(role[field] in used for role in proof["roles"]):
            raise runtime._strict_error("CONFLICT", "Review round reused a prior transport identity")


def validate_history(
    runtime: StrictWorkflow, goal_id: str, context: ReviewContext, previous: Payload | None,
    *, expected: dict[tuple[str, str], str | None] | None = None,
) -> list[Payload]:
    """Recheck original events, projections and immutable proof attestations."""
    store = runtime.store
    try:
        chain = store.list("strict_review_chain", goal_id)
        head = store.get("strict_review_head", goal_id)
        if expected is not None:
            expected[("strict_review_record", goal_id)] = None if previous is None else fingerprint(previous)
            expected[("strict_review_head", goal_id)] = None if head is None else fingerprint(head)
            for entry in chain:
                expected[("strict_review_chain", f"{goal_id}:{entry['round_number']}")] = fingerprint(entry)
        if previous is None:
            if chain or head or store.get_review_attestation(goal_id, 1) is not None:
                raise runtime._strict_error("UNTRUSTED", "Strict review history has no current projection")
            return []
        history = previous.get("history")
        if (not isinstance(history, list) or not history or len(chain) != len(history)
                or previous.get("context_fingerprint") != context.fingerprint
                or previous.get("round_number") != len(history)):
            raise runtime._strict_error("UNTRUSTED", "Strict review projection is malformed")
        proofs: list[Payload] = []
        for index, (entry, bundle) in enumerate(zip(chain, history, strict=True), start=1):
            if (entry.get("goal_id") != goal_id or entry.get("round_number") != index
                    or entry.get("context_fingerprint") != context.fingerprint
                    or not store.same_json(entry.get("bundle"), bundle)):
                raise runtime._strict_error("UNTRUSTED", "Strict review history differs from its projection")
            source = store.get_event(entry["source_event_id"])
            event = SignedEvent.from_dict(source or {})
            runtime._local_event_payload(runtime.observer_authority, event, kind="observer.review",
                                         subject=context.fingerprint, expected_payload=bundle)
            packets = build_packets(context, bundle, history[:index - 1])
            validate_receipts(bundle, packets)
            evaluation = evaluate_review_cycle(
                context, bundle["risk"], tuple(ReviewerResult.from_dict(x) for x in bundle["reviewer_results"]),
                ArbiterResult.from_dict(bundle["arbiter"]), MetaReviewResult.from_dict(bundle["meta_review"]),
                round_number=bundle["round_number"], max_rounds=bundle["max_rounds"],
            )
            if not store.same_json(entry.get("result"), evaluation.to_dict()):
                raise runtime._strict_error("UNTRUSTED", "Strict review result differs from its signed input")
            derived_status, unresolved = derive_decision(runtime, goal_id, context, history[:index], evaluation)
            if (entry.get("status") != derived_status
                    or (index == len(history) and previous.get("unresolved") != unresolved)):
                raise runtime._strict_error("UNTRUSTED", "Strict review decision differs from its signed input")
            attestation = store.get_review_attestation(goal_id, index)
            companion = store.get_event(attestation_id(event.event_id))
            if attestation is None:
                if companion is not None or entry.get("proof_digest") is not None:
                    raise runtime._strict_error("UNTRUSTED", "Strict review attestation is missing")
                if runtime.trusted_review_verifier is not None:
                    raise runtime._strict_error("UNTRUSTED", "Provider-backed review cannot reuse offline history")
                continue
            if runtime.trusted_review_verifier is None:
                raise runtime._strict_error("UNAVAILABLE", "Provider-backed history cannot downgrade to offline")
            if companion is None or not store.same_json(attestation["companion_event"], companion):
                raise runtime._strict_error("UNTRUSTED", "Strict review companion event is missing or changed")
            signed = SignedEvent.from_dict(companion)
            payload = runtime.observer_authority.verify(
                signed, kind="controller.review.attested", subject=context.fingerprint,
            )
            if (set(payload) != {"schema_version", "goal_id", "round_number", "source_event_id",
                                 "source_event_digest", "proof", "record_digest", "chain_digest", "head_digest"}
                    or payload["schema_version"] != "goalanchor.review-attestation/v1"
                    or payload["goal_id"] != goal_id or payload["round_number"] != index
                    or payload["source_event_id"] != event.event_id
                    or payload["source_event_digest"] != fingerprint(event.to_dict())
                    or payload["chain_digest"] != fingerprint(entry)):
                raise runtime._strict_error("UNTRUSTED", "Strict review attestation is not bound to its history")
            saved = normalize(runtime, payload["proof"], bundle, context, packets)
            fresh = verify_provider(runtime, bundle, context, packets)
            # No refresh can replace or extend the immutable persisted proof.
            if not store.same_json(saved, fresh) or entry.get("proof_digest") != saved["proof_digest"]:
                raise runtime._strict_error("UNTRUSTED", "Trusted review proof claims changed")
            if index == len(history):
                if (payload["record_digest"] != fingerprint(previous)
                        or payload["head_digest"] != fingerprint(head or {})
                        or previous.get("proof_digest") != saved["proof_digest"]):
                    raise runtime._strict_error("UNTRUSTED", "Strict review head/projection was changed")
            require_fresh_sources(runtime, saved, proofs)
            proofs.append(saved)
        latest = chain[-1]
        if (not isinstance(head, dict) or head.get("goal_id") != goal_id
                or head.get("round_number") != len(history) or head.get("chain_key") != f"{goal_id}:{len(history)}"
                or head.get("context_fingerprint") != context.fingerprint
                or head.get("status") != latest.get("status") or previous.get("status") != latest.get("status")
                or head.get("source_event_id") != latest.get("source_event_id")
                or previous.get("source_event_id") != latest.get("source_event_id")
                or not store.same_json(previous.get("result"), latest.get("result"))):
            raise runtime._strict_error("UNTRUSTED", "Strict review head differs from its history")
        return proofs
    except StrictWorkflowError:
        raise
    except WorkflowStoreConflict as exc:
        raise runtime._strict_error(exc.code, "Strict review storage validation failed") from exc
    except (KeyError, TypeError, ValueError, ContractValidationError) as exc:
        raise runtime._strict_error("UNTRUSTED", "Strict review history is invalid") from exc


@dataclass(frozen=True)
class ReviewGuard:
    context: ReviewContext
    expected: dict[tuple[str, str], str | None]
    expires_at: str
    runtime_binding: Payload
    proof_digest: str


_GRANT_MUTABLE_FIELDS = {"state", "attempt_id", "consumed_at", "revoked_at", "revoke_event_id", "review_binding_event"}


def bind_grant(runtime: StrictWorkflow, guard: ReviewGuard | None, grant: Payload) -> None:
    if guard is None:
        return
    claims = {k: v for k, v in grant.items() if k not in _GRANT_MUTABLE_FIELDS}
    grant["review_binding_event"] = runtime.observer_authority.sign(
        "controller.grant.review-bound", grant["authorization_id"],
        {"proof_digest": guard.proof_digest, "claims": claims}, at=runtime.clock(),
    ).to_dict()


def validate_grant(runtime: StrictWorkflow, guard: ReviewGuard | None, grant: Payload) -> None:
    if guard is None:
        return
    try:
        event = SignedEvent.from_dict(grant["review_binding_event"])
        payload = runtime.observer_authority.verify(
            event, kind="controller.grant.review-bound", subject=grant["authorization_id"],
        )
        expected = {"proof_digest": guard.proof_digest,
                    "claims": {k: v for k, v in grant.items() if k not in _GRANT_MUTABLE_FIELDS}}
        if not runtime.store.same_json(payload, expected):
            raise ContractValidationError("Grant proof binding differs")
    except (KeyError, TypeError, ContractValidationError) as exc:
        raise runtime._strict_error("UNTRUSTED", "Grant is not bound to the current verified review") from exc


def context_deadline(runtime: StrictWorkflow, goal_id: str, context: ReviewContext) -> str:
    snapshot = runtime.store.read_strict_snapshot(goal_id)
    binding = context.strict_binding
    baseline = snapshot.get("baseline_proof")
    goal = snapshot.get("goal")
    if (binding is None or not isinstance(baseline, dict) or not isinstance(goal, dict)
            or goal.get("generation", 0) != binding["goal_generation"]
            or fingerprint(baseline) != binding["baseline_proof_digest"]):
        raise runtime._strict_error("STALE", "Review baseline dependencies changed")
    evidence = snapshot["evidence_proofs"]
    actual = {p["proof_id"]: fingerprint(p) for p in evidence}
    frozen = {p["proof_id"]: p["proof_digest"] for p in binding["evidence_heads"]}
    if actual != frozen:
        raise runtime._strict_error("STALE", "Review evidence dependencies changed")
    deadlines = [baseline["expires_at"], *(p["expires_at"] for p in evidence)]
    for proof in evidence:
        expires = proof["payload"]["snapshot"].get("expires_at")
        if expires:
            deadlines.append(expires)
    deadline = min(deadlines, key=parse_time)
    if not isinstance(deadline, str):
        raise runtime._strict_error("UNTRUSTED", "Review deadline is malformed")
    if parse_time(deadline) <= parse_time(runtime.clock()):
        raise runtime._strict_error("STALE", "Review dependency expired")
    return deadline


def guard_for(runtime: StrictWorkflow, goal_id: str, context: ReviewContext,
              review: Payload | None) -> ReviewGuard | None:
    goal = runtime.store.get("goal", goal_id)
    expected: dict[tuple[str, str], str | None] = {("goal", goal_id): None if goal is None else fingerprint(goal)}
    proofs = validate_history(runtime, goal_id, context, review, expected=expected)
    if not proofs:
        return None
    if review is None or context.strict_binding is None:
        raise runtime._strict_error("UNTRUSTED", "Strict review binding is missing")
    binding = runtime._current_runtime_binding()
    if binding["binding_digest"] != context.strict_binding["runtime_binding_digest"]:
        raise runtime._strict_error("STALE", "Review runtime identity changed")
    if goal is None or goal.get("generation", 0) != context.strict_binding["goal_generation"]:
        raise runtime._strict_error("STALE", "Review dependencies changed")
    deadlines = [*(p["expires_at"] for p in proofs), context_deadline(runtime, goal_id, context)]
    return ReviewGuard(context, expected, min(deadlines, key=parse_time),
                       binding, proofs[-1]["proof_digest"])


def commit_guarded(
    runtime: StrictWorkflow, guard: ReviewGuard | None, changes: Sequence[tuple[str, str, Payload]],
    expected: Mapping[tuple[str, str], str | None], event: SignedEvent,
) -> bool:
    if guard is None:
        return runtime.store.commit(changes, expected, event.event_id, event.to_dict())
    merged = dict(guard.expected)
    for key, value in expected.items():
        if key in merged and merged[key] != value:
            raise runtime._strict_error("STALE", "Review dependency changed before commit")
        merged[key] = value
    try:
        return runtime.store.commit_review_bound(
            changes, merged, ((event.event_id, event.to_dict()),),
            valid_until=guard.expires_at, clock=runtime.clock, lease_factory=runtime._runtime_lease,
            expected_runtime_digest=guard.runtime_binding["binding_digest"],
            expected_lease_generation=guard.runtime_binding["generation"],
        )
    except WorkflowStoreConflict as exc:
        raise runtime._strict_error(exc.code, "Review-bound commit failed") from exc
    except ContractValidationError as exc:
        raise runtime._strict_error("STALE", "Review dependencies changed during commit") from exc


def derive_decision(runtime: StrictWorkflow, goal_id: str, context: ReviewContext,
                    history: list[Payload], result: Any) -> tuple[str, list[str]]:
    """Derive authority solely from authenticated round inputs and risk evidence."""
    bundle = history[-1]
    findings: dict[str, Payload] = {}
    for old in history:
        for report in old["reviewer_results"]:
            for finding in report["findings"]:
                finding_id = finding["finding_id"]
                if finding_id in findings and findings[finding_id] != finding:
                    raise runtime._strict_error("CONFLICT", "Strict finding identity changed")
                findings[finding_id] = finding
    dispositions = bundle["dispositions"]
    if not isinstance(dispositions, dict) or set(dispositions) - set(findings):
        raise runtime._strict_error("UNTRUSTED", "Strict finding disposition is invalid")
    evidence_ids = set(context.evidence_refs)
    unresolved: list[str] = []
    for finding_id in findings:
        disposition = dispositions.get(finding_id, {})
        refs = disposition.get("evidence_refs", []) if isinstance(disposition, dict) else []
        status = disposition.get("status") if isinstance(disposition, dict) else None
        if status in ("FIXED", "REFUTED") and refs and set(refs) <= evidence_ids:
            if status == "REFUTED" and bundle["round_number"] < 2:
                unresolved.append(finding_id)
        elif status == "RISK_ACCEPTED":
            risk = runtime.store.get("strict_risk", f"{goal_id}:{finding_id}")
            if finding_id not in runtime.policy.acceptable_risks or not runtime._validate_strict_risk(
                goal_id, finding_id, risk, context
            ):
                unresolved.append(finding_id)
        else:
            unresolved.append(finding_id)
    if result.outcome.value == "PASS" and not unresolved:
        status = "PASSED"
    elif bundle["round_number"] >= bundle["max_rounds"] or result.outcome.value == "STOPPED":
        status = "STOPPED"
    else:
        status = "REVIEW_REQUIRED"
    return status, unresolved
