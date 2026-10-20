"""Strict workflow implementation with host-controlled trust boundaries."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import RLock
from typing import Any, NoReturn, TypeGuard

from .core import ContractValidationError
from .host_contract import validate_host_binding
from .questions import Question, QuestionRecord
from .review_service import ReviewContext
from .workflow_contracts import (
    Payload,
    StrictWorkflowError,
    TrustedBindingStale,
    TrustedBindingUnavailable,
    TrustedEventProofProvider,
    TrustedProofRejected,
    TrustedProofUnavailable,
    TrustedReviewProofProvider,
    TrustedRuntimeBindingProvider,
    WorkflowPolicy,
)
from .workflow_legacy import Workflow
from .workflow_protocol import (
    Baseline,
    EventAuthority,
    EvidenceSnapshot,
    Operation,
    SignedEvent,
    canonical,
    fingerprint,
    parse_time,
    strict_loads,
    utc_now,
)
from .workflow_store import WorkflowStore, WorkflowStoreConflict

_PATH_ARGUMENT_KEYS = frozenset({
    "path", "paths", "filepath", "filepaths", "file_path", "file_paths", "file", "files",
    "filename", "filenames", "file_name", "file_names", "directory", "directories", "dir", "dirs",
    "cwd", "target", "destination", "source", "output", "input", "workspace", "root", "object_ref",
})
_DEFAULT_OPERATION_PATH_SCHEMAS: dict[str, frozenset[str]] = {"write_text": frozenset({"path"})}
_STRICT_EVENT_SIGNATURE_CACHE_MAX_ENTRIES = 100_000
_STRICT_EVENT_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def _is_canonical_event_digest(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _STRICT_EVENT_DIGEST_RE.fullmatch(value) is not None


def _normalize_argument_key(key: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).casefold().replace("-", "_")


def _path_argument_values(value: Any, path_fields: frozenset[str], key: str = "") -> Iterator[str]:
    normalized = _normalize_argument_key(key)
    is_path_like = normalized in _PATH_ARGUMENT_KEYS or normalized.endswith(
        ("_path", "_file", "_dir", "_directory")
    )
    if normalized in path_fields:
        if isinstance(value, str) and value.strip():
            yield value
            return
        if isinstance(value, (list, tuple)) and value and all(isinstance(item, str) and item.strip() for item in value):
            yield from value
            return
        raise ContractValidationError("Path-bearing operation arguments must contain non-empty path strings")
    if is_path_like:
        raise ContractValidationError("Path-bearing operation argument is not declared by its tool schema")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            if isinstance(child_key, str):
                yield from _path_argument_values(child, path_fields, child_key)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _path_argument_values(child, path_fields)


class StrictWorkflow(Workflow):
    """Fail-closed strict path; legacy ``Workflow`` remains wire-compatible.

    ``operation_path_schemas`` is trusted composition-root metadata that lists
    argument keys containing filesystem paths. Strict registration fails closed
    when a tool has no schema; agents cannot supply or modify these declarations.
    """

    def __init__(
        self,
        store: WorkflowStore,
        user_authority: EventAuthority,
        observer_authority: EventAuthority,
        policy: WorkflowPolicy,
        *,
        trusted_event_verifier: TrustedEventProofProvider | None = None,
        trusted_review_verifier: TrustedReviewProofProvider | None = None,
        trusted_runtime_binding_provider: TrustedRuntimeBindingProvider | None = None,
        operation_path_schemas: Mapping[str, Sequence[str]] | None = None,
        question_ttl_seconds: int = 86400,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        super().__init__(store, user_authority, observer_authority, policy, clock=clock)
        self._event_signature_cache: OrderedDict[tuple[int, str, str, str], bool] = OrderedDict()
        self._event_signature_cache_lock = RLock()
        if type(question_ttl_seconds) is not int or not 1 <= question_ttl_seconds <= 604800:
            raise ContractValidationError("question TTL must be an integer from 1 to 604800 seconds")
        self._question_ttl_seconds = question_ttl_seconds
        self._trusted_event_verifier = trusted_event_verifier
        self._trusted_review_verifier = trusted_review_verifier
        self._trusted_runtime_binding_provider = trusted_runtime_binding_provider
        self._operation_path_schemas = dict(_DEFAULT_OPERATION_PATH_SCHEMAS)
        if operation_path_schemas is not None:
            if not isinstance(operation_path_schemas, Mapping):
                raise ContractValidationError("operation_path_schemas must be an object")
            for tool, fields in operation_path_schemas.items():
                if not isinstance(tool, str) or not tool.strip():
                    raise ContractValidationError("operation path schema tool must be non-empty text")
                normalized_tool = tool.strip()
                if isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence):
                    raise ContractValidationError("operation path schema fields must be an array")
                normalized_fields = frozenset(_normalize_argument_key(field.strip()) for field in fields
                                              if isinstance(field, str) and field.strip())
                if len(normalized_fields) != len(fields) or any(
                    not isinstance(field, str) or not field.strip() for field in fields
                ):
                    raise ContractValidationError("operation path schema fields must be unique non-empty text")
                self._operation_path_schemas[normalized_tool] = (
                    self._operation_path_schemas.get(normalized_tool, frozenset()) | normalized_fields
                )

    @property
    def trusted_event_verifier(self) -> TrustedEventProofProvider | None:
        return self._trusted_event_verifier

    @property
    def trusted_review_verifier(self) -> TrustedReviewProofProvider | None:
        return self._trusted_review_verifier

    @property
    def runtime_binding_provider(self) -> TrustedRuntimeBindingProvider | None:
        return self._trusted_runtime_binding_provider

    def _strict_error(self, code: str, message: str) -> StrictWorkflowError:
        return StrictWorkflowError(message, code)

    def _verify_cached_event_signature(
        self,
        event: SignedEvent,
        authority: EventAuthority,
        *,
        event_id: str,
        payload_digest: str,
    ) -> None:
        cache_key = (
            id(authority),
            authority.verification_fingerprint,
            event_id,
            payload_digest,
        )
        try:
            with self._event_signature_cache_lock:
                cached = self._event_signature_cache.get(cache_key)
                if cached is True:
                    self._event_signature_cache.move_to_end(cache_key)
                    return
        except Exception as exc:
            raise self._strict_error("UNAVAILABLE", "Strict event signature cache lookup failed") from exc

        try:
            authority.verify(event)
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Strict source event signature is invalid") from exc
        except Exception as exc:
            raise self._strict_error("UNAVAILABLE", "Strict source event verification failed") from exc

        try:
            with self._event_signature_cache_lock:
                self._event_signature_cache[cache_key] = True
                self._event_signature_cache.move_to_end(cache_key)
                while len(self._event_signature_cache) > _STRICT_EVENT_SIGNATURE_CACHE_MAX_ENTRIES:
                    self._event_signature_cache.popitem(last=False)
        except Exception as exc:
            raise self._strict_error("UNAVAILABLE", "Strict event signature cache insertion failed") from exc

    def _normalize_runtime_binding(self, raw: Any, *, allow_digest: bool = False) -> Payload:
        required = {"host_binding", "principal_id", "account_id", "session_id", "generation"}
        allowed = required | ({"binding_digest"} if allow_digest else set())
        if not isinstance(raw, Mapping) or set(raw) not in (required, allowed):
            raise self._strict_error("UNTRUSTED", "Trusted runtime binding has an invalid schema")
        if self.policy.host_binding is None:
            raise self._strict_error("UNAVAILABLE", "Strict host binding is unavailable")
        try:
            host_binding = validate_host_binding(raw["host_binding"])
            host_facts = strict_loads(host_binding)
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Trusted runtime host facts are malformed") from exc
        if host_binding != self.policy.host_binding or not isinstance(host_facts, dict):
            raise self._strict_error("UNTRUSTED", "Trusted runtime host facts do not match policy")
        identity = {}
        for name in ("principal_id", "account_id", "session_id"):
            value = raw[name]
            if not isinstance(value, str) or not value.strip():
                raise self._strict_error("UNTRUSTED", "Trusted runtime identity is incomplete")
            identity[name] = value.strip()
        generation = raw["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise self._strict_error("UNTRUSTED", "Trusted runtime generation is invalid")
        digest = fingerprint({
            "policy_digest": self.policy.fingerprint,
            "host_binding": host_facts,
            **identity,
            "generation": generation,
        })
        if allow_digest and "binding_digest" in raw and raw["binding_digest"] != digest:
            raise self._strict_error("UNTRUSTED", "Runtime lease digest does not match its facts")
        return {
            "host_binding": host_binding,
            **identity,
            "generation": generation,
            "binding_digest": digest,
        }

    def _current_runtime_binding(self) -> Payload:
        provider = self._trusted_runtime_binding_provider
        if provider is None:
            raise self._strict_error("UNAVAILABLE", "Trusted runtime binding provider is unavailable")
        try:
            raw = provider.current_runtime_binding()
        except TrustedBindingStale as exc:
            raise self._strict_error("STALE", "Trusted runtime identity changed") from exc
        except (TrustedBindingUnavailable, TimeoutError, ConnectionError) as exc:
            raise self._strict_error("UNAVAILABLE", "Trusted runtime binding is unavailable") from exc
        except Exception as exc:
            raise self._strict_error("UNAVAILABLE", "Trusted runtime binding could not be read") from exc
        return self._normalize_runtime_binding(raw)

    @contextmanager
    def _runtime_lease(self, expected_digest: str) -> Iterator[Payload]:
        provider = self._trusted_runtime_binding_provider
        if provider is None:
            raise self._strict_error("UNAVAILABLE", "Trusted runtime lease provider is unavailable")
        lease_context = None
        try:
            lease_context = provider.acquire_lease(expected_digest)
            raw = lease_context.__enter__()
        except TrustedBindingStale as exc:
            raise self._strict_error("STALE", "Trusted runtime identity changed before commit") from exc
        except (TrustedBindingUnavailable, TimeoutError, ConnectionError) as exc:
            raise self._strict_error("UNAVAILABLE", "Trusted runtime lease is unavailable") from exc
        except Exception as exc:
            raise self._strict_error("UNAVAILABLE", "Trusted runtime lease could not be acquired") from exc
        try:
            current = self._normalize_runtime_binding(raw, allow_digest=True)
            if current["binding_digest"] != expected_digest:
                raise self._strict_error("STALE", "Trusted runtime identity changed before commit")
        except BaseException:
            try:
                lease_context.__exit__(*sys.exc_info())
            finally:
                raise
        try:
            yield current
        except BaseException:
            try:
                lease_context.__exit__(*sys.exc_info())
            finally:
                raise
        else:
            try:
                lease_context.__exit__(None, None, None)
            except (TrustedBindingUnavailable, TimeoutError, ConnectionError) as exc:
                raise self._strict_error("UNAVAILABLE", "Trusted runtime lease release failed") from exc
            except Exception as exc:
                raise self._strict_error("UNAVAILABLE", "Trusted runtime lease release failed") from exc

    def _trusted_event_proof(
        self,
        event: SignedEvent,
        *,
        purpose: str,
        binding: dict[str, Any],
        actor_type: str,
        expected_identity: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        provider = self._trusted_event_verifier
        if provider is None:
            raise self._strict_error("UNAVAILABLE", "Trusted host event provider is unavailable")
        try:
            proof = provider.verify(event, purpose=purpose, binding=binding)
        except (TrustedProofUnavailable, TimeoutError, ConnectionError) as exc:
            raise self._strict_error("UNAVAILABLE", "Trusted host event provider is unavailable") from exc
        except TrustedProofRejected as exc:
            raise self._strict_error("UNTRUSTED", "Trusted host event was rejected") from exc
        except Exception as exc:
            raise self._strict_error("UNAVAILABLE", "Trusted host event verification failed") from exc
        if not isinstance(proof, dict):
            raise self._strict_error("UNTRUSTED", "Trusted host event proof is missing")
        required = {
            "actor_type",
            "principal_id",
            "account_id",
            "session_id",
            "source_event_id",
            "purpose",
            "event_kind",
            "subject",
            "binding_digest",
            "expires_at",
            "nonce",
            "proof_digest",
        }
        if set(proof) != required:
            raise self._strict_error("UNTRUSTED", "Trusted host event proof has an invalid schema")
        if (
            proof["actor_type"] != actor_type
            or proof["source_event_id"] != event.event_id
            or proof["purpose"] != purpose
            or proof["event_kind"] != event.kind
            or proof["subject"] != event.subject
            or proof["binding_digest"] != fingerprint(binding)
            or not all(isinstance(proof[name], str) and proof[name].strip() for name in (
                "principal_id", "account_id", "session_id", "nonce", "proof_digest"
            ))
        ):
            raise self._strict_error("UNTRUSTED", "Trusted host event proof does not match its binding")
        if expected_identity is not None and any(
            proof.get(name) != expected_identity.get(name)
            for name in ("principal_id", "account_id", "session_id")
        ):
            raise self._strict_error("UNTRUSTED", "Trusted host identity changed")
        try:
            digest = proof["proof_digest"]
            if len(digest) != 64 or digest != digest.lower() or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("invalid proof digest")
            expires_at = parse_time(proof["expires_at"])
        except (TypeError, ValueError, ContractValidationError) as exc:
            raise self._strict_error("UNTRUSTED", "Trusted host event proof is malformed") from exc
        if expires_at <= parse_time(self.clock()):
            raise self._strict_error("STALE", "Trusted host event proof expired")
        return deepcopy(proof)

    def _local_event_payload(
        self,
        authority: EventAuthority,
        event: SignedEvent,
        *,
        kind: str,
        subject: str,
        expected_payload: Payload,
    ) -> Payload:
        try:
            payload = self._verify(authority, event, kind, subject)
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Signed source event is invalid") from exc
        if canonical(payload) != canonical(expected_payload):
            raise self._strict_error("UNTRUSTED", "Signed source event payload mismatch")
        return payload

    @staticmethod
    def _baseline_binding(baseline: Baseline, runtime_digest: str) -> Payload:
        return {
            "goal_id": baseline.goal_id,
            "baseline_digest": baseline.fingerprint,
            "runtime_binding_digest": runtime_digest,
            "plan_revision": baseline.revision,
            "objective": baseline.objective,
            "criteria_digest": fingerprint([item.to_dict() for item in baseline.criteria]),
        }

    @staticmethod
    def _evidence_binding(
        goal_id: str,
        baseline: Baseline,
        snapshot: EvidenceSnapshot,
        runtime_digest: str,
    ) -> Payload:
        return {
            "goal_id": goal_id,
            "baseline_digest": baseline.fingerprint,
            "runtime_binding_digest": runtime_digest,
            "evidence_id": snapshot.evidence_id,
            "evidence_digest": snapshot.sha256,
            "snapshot_fingerprint": snapshot.fingerprint,
        }

    def _load_baseline(self, snapshot: Payload, goal_id: str) -> tuple[Payload, Baseline]:
        goal = snapshot.get("goal")
        head = snapshot.get("baseline_head")
        proof = snapshot.get("baseline_proof")
        if goal is None:
            raise self._strict_error("STALE", "Strict goal is unavailable")
        if head is None or proof is None:
            if goal.get("baseline_digest"):
                raise self._strict_error("LEGACY_UNVERIFIED", "Baseline has no strict source proof")
            raise self._strict_error("STALE", "Strict baseline is unavailable")
        try:
            baseline_data = proof["payload"]["baseline"]
            baseline = Baseline.from_dict(baseline_data)
        except (KeyError, TypeError, ContractValidationError) as exc:
            raise self._strict_error("UNTRUSTED", "Strict baseline proof payload is malformed") from exc
        self._assert_persisted_provider_claims(proof)
        try:
            source_event = snapshot["source_events"][proof["source_event_id"]]["payload"]
            signed_created_at = source_event["created_at"]
        except (KeyError, TypeError) as exc:
            raise self._strict_error("UNTRUSTED", "Strict baseline source event is missing") from exc
        expected_projection = self.store.expected_baseline_goal_projection(proof, source_event)
        expected_goal_fields = set(expected_projection) | {
            "state", "generation", "baseline_created_at", "supersedes",
        }
        if (
            proof.get("kind") != "baseline"
            or proof.get("goal_id") != goal_id
            or proof.get("baseline_digest") != baseline.fingerprint
            or baseline.goal_id != goal_id
            or proof.get("policy_digest") != self.policy.fingerprint
            or proof.get("source_seq") != head.get("source_seq")
            or proof.get("proof_id") != head.get("proof_id")
            or goal.get("baseline_digest") != baseline.fingerprint
            or goal.get("strict_baseline_proof_id") != proof.get("proof_id")
            or not self.store.same_json(snapshot.get("baseline_projection"), baseline.to_dict())
            or goal.get("state") != "PLAN"
            or type(goal.get("generation")) is not int
            or goal.get("generation", -1) < 0
            or goal.get("baseline_created_at") != signed_created_at
            or set(goal) != expected_goal_fields
            or not self.store.same_json(
                {key: goal[key] for key in expected_projection}, expected_projection
            )
        ):
            raise self._strict_error("UNTRUSTED", "Strict baseline projection does not match its proof")
        if parse_time(proof["expires_at"]) <= parse_time(self.clock()):
            raise self._strict_error("STALE", "Strict baseline proof expired")
        return goal, baseline

    def _source_event(
        self,
        snapshot: Payload,
        proof: Payload,
        authority: EventAuthority,
        *,
        kind: str,
        subject: str,
        expected_payload: Payload,
    ) -> SignedEvent:
        try:
            stored = snapshot["source_events"][proof["source_event_id"]]
            event = SignedEvent.from_dict(stored["payload"])
        except (KeyError, TypeError, ContractValidationError) as exc:
            raise self._strict_error("UNTRUSTED", "Strict source event is missing or malformed") from exc
        if (
            stored.get("seq") != proof.get("source_seq")
            or self.store.fingerprint(event.to_dict()) != proof.get("source_event_digest")
        ):
            raise self._strict_error("UNTRUSTED", "Strict source event content changed")
        self._local_event_payload(
            authority, event, kind=kind, subject=subject, expected_payload=expected_payload
        )
        return event

    def _reverify_source_proof(
        self,
        event: SignedEvent,
        proof: Payload,
        *,
        purpose: str,
        binding: Payload,
        actor_type: str,
    ) -> None:
        trusted = proof.get("trusted_proof")
        if not isinstance(trusted, dict):
            raise self._strict_error("UNTRUSTED", "Strict proof has no trusted provider result")
        self._assert_persisted_provider_claims(proof)
        verified = self._trusted_event_proof(
            event,
            purpose=purpose,
            binding=binding,
            actor_type=actor_type,
            expected_identity={
                name: proof[name] for name in ("principal_id", "account_id", "session_id")
            },
        )
        self._ensure_saved_expiry_is_covered(verified, proof)
        if not self._same_trusted_claims(verified, trusted):
            raise self._strict_error("UNTRUSTED", "Trusted provider result changed")

    def _assert_persisted_provider_claims(self, proof: Payload) -> None:
        trusted = proof.get("trusted_proof")
        if not isinstance(trusted, Mapping):
            raise self._strict_error("UNTRUSTED", "Strict proof has no trusted provider result")
        persisted_fields = (
            "actor_type", "principal_id", "account_id", "session_id", "source_event_id",
            "proof_digest", "expires_at",
        )
        if any(proof.get(name) != trusted.get(name) for name in persisted_fields):
            raise self._strict_error("UNTRUSTED", "Persisted provider claims do not match the strict proof")

    @staticmethod
    def _same_trusted_claims(current: Mapping[str, Any], persisted: Mapping[str, Any]) -> bool:
        immutable = (
            "actor_type", "principal_id", "account_id", "session_id", "source_event_id", "purpose",
            "event_kind", "subject", "binding_digest",
        )
        return all(current.get(name) == persisted.get(name) for name in immutable)

    def _ensure_saved_expiry_is_covered(
        self, current: Mapping[str, Any], persisted: Mapping[str, Any]
    ) -> None:
        try:
            saved_expiry = parse_time(persisted["expires_at"])
            current_expiry = parse_time(current["expires_at"])
        except (KeyError, TypeError, ContractValidationError) as exc:
            raise self._strict_error("UNTRUSTED", "Trusted proof expiry is malformed") from exc
        if saved_expiry > current_expiry:
            raise self._strict_error("STALE", "Fresh provider proof does not cover the saved proof expiry")

    @staticmethod
    def _deadline(*values: str | None) -> str:
        deadlines = [parse_time(value) for value in values if value is not None]
        return min(deadlines).isoformat()

    def _store_commit(self, *args: Any, **kwargs: Any) -> tuple[bool, Payload]:
        try:
            return self.store.commit_strict(*args, **kwargs)
        except StrictWorkflowError:
            raise
        except WorkflowStoreConflict as exc:
            raise self._strict_error(exc.code, str(exc)) from exc
        except ContractValidationError as exc:
            message = str(exc)
            code = "STALE" if "Stale" in message or "expired" in message.lower() else "UNTRUSTED"
            raise self._strict_error(code, message) from exc
        except sqlite3.Error as exc:
            raise self._strict_error("UNAVAILABLE", "Strict workflow storage is unavailable") from exc

    def _confirm_commit(self, goal_id: str, proof_id: str) -> Payload:
        try:
            snapshot = self.store.read_strict_snapshot(goal_id)
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Strict commit readback failed") from exc
        goal, _ = self._load_baseline(snapshot, goal_id)
        proof = snapshot.get("baseline_proof")
        if (
            proof is None
            or proof.get("proof_id") != proof_id
            or snapshot.get("baseline_head", {}).get("proof_id") != proof_id
        ):
            raise self._strict_error("STALE", "Strict baseline is no longer the active head")
        return goal

    def start_baseline(self, baseline: Baseline, event: SignedEvent) -> Payload:
        self._local_event_payload(
            self.user_authority,
            event,
            kind="user.baseline",
            subject=baseline.fingerprint,
            expected_payload=baseline.to_dict(),
        )
        try:
            snapshot = self.store.read_strict_snapshot(
                baseline.goal_id,
                candidate_event_id=event.event_id,
                candidate_baseline_digest=baseline.fingerprint,
            )
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Strict baseline snapshot is invalid") from exc
        old = snapshot.get("goal")
        active_proof = snapshot.get("baseline_proof")
        candidate = snapshot.get("candidate_event")
        replay_proof: Mapping[str, Any] | None = None
        if candidate is not None:
            if not isinstance(candidate, Mapping):
                raise self._strict_error("UNTRUSTED", "Baseline replay event is malformed")
            if active_proof is None:
                raise self._strict_error("STALE", "Baseline event is no longer the active proof")
            if not isinstance(active_proof, Mapping):
                raise self._strict_error("UNTRUSTED", "Baseline replay proof is malformed")
            replay_proof = active_proof
            if not self.store.same_json(candidate.get("payload"), event.to_dict()):
                raise self._strict_error("UNTRUSTED", "Baseline event ID was reused with different content")
            if (
                replay_proof.get("source_event_id") != event.event_id
                or replay_proof.get("baseline_digest") != baseline.fingerprint
            ):
                raise self._strict_error("STALE", "Baseline event is no longer the active proof")
            _, current_baseline = self._load_baseline(snapshot, baseline.goal_id)
            if current_baseline.fingerprint != baseline.fingerprint:
                raise self._strict_error("STALE", "Baseline replay does not match the current contract")
        elif old is not None and baseline.revision <= old.get("revision", 0):
            raise self._strict_error("STALE", "Baseline revision must advance")
        current = self._current_runtime_binding()
        if replay_proof is not None and replay_proof.get("runtime_binding_digest") != current["binding_digest"]:
            raise self._strict_error("STALE", "Baseline runtime identity has changed")
        binding = self._baseline_binding(baseline, current["binding_digest"])
        provider_proof = self._trusted_event_proof(
            event, purpose="user.baseline", binding=binding, actor_type="USER"
        )
        if any(
            provider_proof[name] != current[name]
            for name in ("principal_id", "account_id", "session_id")
        ):
            raise self._strict_error("UNTRUSTED", "Baseline approval identity differs from the live user")
        if replay_proof is not None:
            self._ensure_saved_expiry_is_covered(provider_proof, replay_proof)
            if not self._same_trusted_claims(provider_proof, replay_proof["trusted_proof"]):
                raise self._strict_error(
                    "UNTRUSTED", "Baseline replay proof changed its trusted identity binding"
                )
        after = self._current_runtime_binding()
        if after["binding_digest"] != current["binding_digest"]:
            raise self._strict_error("STALE", "Runtime identity changed during baseline verification")
        if replay_proof is not None:
            with self._runtime_lease(current["binding_digest"]):
                try:
                    final = self.store.read_strict_snapshot(
                        baseline.goal_id, candidate_event_id=event.event_id,
                    )
                except ContractValidationError as exc:
                    raise self._strict_error("STALE", "Baseline replay changed during verification") from exc
                if self.store.fingerprint(final) != self.store.fingerprint(snapshot):
                    raise self._strict_error("STALE", "Baseline replay dependencies changed")
                final_goal, _ = self._load_baseline(final, baseline.goal_id)
                if final["baseline_proof"]["proof_id"] != replay_proof["proof_id"]:
                    raise self._strict_error("STALE", "Baseline replay is no longer the active head")
                return final_goal
        proof = {
            "kind": "baseline",
            "goal_id": baseline.goal_id,
            "scope_digest": "",
            "subject_id": "current",
            "baseline_digest": baseline.fingerprint,
            "source_event_id": event.event_id,
            "source_event_digest": self.store.fingerprint(event.to_dict()),
            "proof_digest": provider_proof["proof_digest"],
            "actor_type": provider_proof["actor_type"],
            "principal_id": provider_proof["principal_id"],
            "account_id": provider_proof["account_id"],
            "session_id": provider_proof["session_id"],
            "expires_at": provider_proof["expires_at"],
            "policy_digest": self.policy.fingerprint,
            "runtime_binding_digest": current["binding_digest"],
            "lease_generation": current["generation"],
            "trusted_proof": provider_proof,
            "payload": {},
        }
        proof_id = self.store.strict_proof_identifier(proof)
        goal_projection = {
            "goal_id": baseline.goal_id,
            "revision": baseline.revision,
            "baseline": baseline.to_dict(),
            "baseline_digest": baseline.fingerprint,
            "policy_digest": self.policy.fingerprint,
            "baseline_event_id": event.event_id,
            "strict_baseline_proof_id": proof_id,
            "runtime_binding_digest": current["binding_digest"],
            "trusted_identity": {
                "principal_id": provider_proof["principal_id"],
                "account_id": provider_proof["account_id"],
                "session_id": provider_proof["session_id"],
                "source_event_id": provider_proof["source_event_id"],
                "proof_digest": provider_proof["proof_digest"],
            },
        }
        old_generation = 0 if old is None else old.get("generation", 0)
        if isinstance(old_generation, bool) or not isinstance(old_generation, int) or old_generation < 0:
            raise self._strict_error("UNTRUSTED", "Existing goal generation is invalid")
        proof["payload"] = {
            "baseline": baseline.to_dict(),
            "goal_projection": goal_projection,
        }
        goal = {
            **goal_projection,
            "generation": old_generation,
            "state": "PLAN",
            "baseline_created_at": event.created_at,
            "supersedes": None if old is None else old["baseline_digest"],
        }
        old_baseline_projection = snapshot.get("candidate_baseline_projection")
        expected = {
            ("baseline", baseline.fingerprint): (
                None if old_baseline_projection is None else self.store.fingerprint(old_baseline_projection)
            ),
            ("goal", baseline.goal_id): None if old is None else self.store.fingerprint(old),
        }
        deadline = self._deadline(provider_proof["expires_at"])
        inserted, stored_proof = self._store_commit(
            [("baseline", baseline.fingerprint, baseline.to_dict()), ("goal", baseline.goal_id, goal)],
            expected,
            event.event_id,
            event.to_dict(),
            proof,
            snapshot.get("baseline_head"),
            self._runtime_lease,
            valid_until=deadline,
            clock=self.clock,
        )
        if not inserted:
            self._local_event_payload(
                self.user_authority,
                event,
                kind="user.baseline",
                subject=baseline.fingerprint,
                expected_payload=baseline.to_dict(),
            )
        return self._confirm_commit(baseline.goal_id, stored_proof["proof_id"])

    def _confirm_evidence_replay_context(
        self,
        goal_id: str,
        baseline_proof_id: str,
        evidence_id: str,
        proof_id: str,
        projection: Payload,
    ) -> Payload:
        context = self.review_context(goal_id)
        binding = context.strict_binding
        if not isinstance(binding, Mapping) or binding.get("baseline_proof_id") != baseline_proof_id:
            raise self._strict_error("STALE", "Strict evidence replay baseline is no longer current")
        evidence_heads = binding.get("evidence_heads")
        if not isinstance(evidence_heads, (list, tuple)):
            raise self._strict_error("STALE", "Strict evidence replay head is unavailable")
        matches = [
            item
            for item in evidence_heads
            if isinstance(item, Mapping) and item.get("evidence_id") == evidence_id
        ]
        if len(matches) != 1 or matches[0].get("proof_id") != proof_id:
            raise self._strict_error("STALE", "Strict evidence replay is no longer the active head")
        return projection

    def add_evidence(self, goal_id: str, snapshot: EvidenceSnapshot, event: SignedEvent) -> Payload:
        try:
            initial = self.store.read_strict_snapshot(
                goal_id, candidate_event_id=event.event_id, candidate_evidence_id=snapshot.evidence_id,
            )
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Strict evidence snapshot is invalid") from exc
        goal, baseline = self._load_baseline(initial, goal_id)
        self._local_event_payload(
            self.observer_authority,
            event,
            kind="observer.evidence",
            subject=snapshot.fingerprint,
            expected_payload={"goal_id": goal_id, "snapshot": snapshot.to_dict()},
        )
        if not snapshot.valid_at(self.clock()):
            raise self._strict_error("STALE", "Evidence expired or not yet applicable")
        current_head = next(
            (item for item in initial["evidence_heads"] if item["subject_id"] == snapshot.evidence_id), None
        )
        current_proof = next(
            (item for item in initial["evidence_proofs"] if item["proof_id"] == current_head["proof_id"]), None
        ) if current_head is not None else None
        candidate = initial.get("candidate_event")
        if candidate is not None:
            if not self.store.same_json(candidate.get("payload"), event.to_dict()):
                raise self._strict_error("UNTRUSTED", "Evidence event ID was reused with different content")
            if current_proof is None or current_proof.get("source_event_id") != event.event_id:
                raise self._strict_error("STALE", "Evidence event is no longer the active proof")
        baseline_event = self._source_event(
            initial,
            initial["baseline_proof"],
            self.user_authority,
            kind="user.baseline",
            subject=baseline.fingerprint,
            expected_payload=baseline.to_dict(),
        )
        current = self._current_runtime_binding()
        if current["binding_digest"] != initial["baseline_proof"]["runtime_binding_digest"]:
            raise self._strict_error("STALE", "Baseline runtime identity has changed")
        if candidate is not None and current_proof is not None:
            self._reverify_source_proof(
                event,
                current_proof,
                purpose="observer.evidence",
                binding=self._evidence_binding(goal_id, baseline, snapshot, current["binding_digest"]),
                actor_type="SERVICE",
            )
            return self._confirm_evidence_replay_context(
                goal_id,
                initial["baseline_proof"]["proof_id"],
                snapshot.evidence_id,
                current_proof["proof_id"],
                current_proof["payload"]["projection"],
            )
        self._reverify_source_proof(
            baseline_event,
            initial["baseline_proof"],
            purpose="user.baseline",
            binding=self._baseline_binding(baseline, current["binding_digest"]),
            actor_type="USER",
        )
        evidence_binding = self._evidence_binding(goal_id, baseline, snapshot, current["binding_digest"])
        provider_proof = self._trusted_event_proof(
            event, purpose="observer.evidence", binding=evidence_binding, actor_type="SERVICE"
        )
        after = self._current_runtime_binding()
        if after["binding_digest"] != current["binding_digest"]:
            raise self._strict_error("STALE", "Runtime identity changed during evidence verification")
        deadline = self._deadline(
            initial["baseline_proof"]["expires_at"], provider_proof["expires_at"], snapshot.expires_at
        )
        proof = {
            "kind": "evidence",
            "goal_id": goal_id,
            "scope_digest": baseline.fingerprint,
            "subject_id": snapshot.evidence_id,
            "baseline_digest": baseline.fingerprint,
            "source_event_id": event.event_id,
            "source_event_digest": self.store.fingerprint(event.to_dict()),
            "proof_digest": provider_proof["proof_digest"],
            "actor_type": provider_proof["actor_type"],
            "principal_id": provider_proof["principal_id"],
            "account_id": provider_proof["account_id"],
            "session_id": provider_proof["session_id"],
            "expires_at": provider_proof["expires_at"],
            "policy_digest": self.policy.fingerprint,
            "runtime_binding_digest": current["binding_digest"],
            "lease_generation": current["generation"],
            "trusted_proof": provider_proof,
            "payload": {},
        }
        proof_id = self.store.strict_proof_identifier(proof)
        record = {
            "goal_id": goal_id,
            "snapshot": snapshot.to_dict(),
            "source_event_id": event.event_id,
            "strict_proof_id": proof_id,
            "trusted_source": {
                "principal_id": provider_proof["principal_id"],
                "account_id": provider_proof["account_id"],
                "session_id": provider_proof["session_id"],
                "proof_digest": provider_proof["proof_digest"],
            },
        }
        proof["payload"] = {
            "snapshot": snapshot.to_dict(),
            "snapshot_fingerprint": snapshot.fingerprint,
            "projection": record,
        }
        key = self.store.strict_evidence_key(goal_id, snapshot.evidence_id)
        old_projection = initial.get("candidate_strict_evidence_projection")
        expected = {
            ("strict_evidence", key): None if old_projection is None else self.store.fingerprint(old_projection),
            ("goal", goal_id): self.store.fingerprint(goal),
        }
        inserted, committed_proof = self._store_commit(
            [("strict_evidence", key, record)],
            expected,
            event.event_id,
            event.to_dict(),
            proof,
            current_head,
            self._runtime_lease,
            valid_until=deadline,
            clock=self.clock,
        )
        if not inserted:
            return self._confirm_evidence_replay_context(
                goal_id,
                initial["baseline_proof"]["proof_id"],
                snapshot.evidence_id,
                committed_proof["proof_id"],
                committed_proof["payload"]["projection"],
            )
        updated = self.store.read_strict_snapshot(goal_id)
        proof_after = next(
            (item for item in updated["evidence_proofs"] if item["proof_id"] == proof_id), None
        )
        head_after = next(
            (item for item in updated["evidence_heads"] if item["subject_id"] == snapshot.evidence_id), None
        )
        if proof_after is None or head_after is None:
            raise self._strict_error("STALE", "Strict evidence commit is no longer active")
        self._load_evidence(
            head_after, proof_after, updated["evidence_projections"].get(snapshot.evidence_id), baseline,
            updated["source_events"][proof_after["source_event_id"]]["payload"],
        )
        return record

    def review_context(self, goal_id: str) -> ReviewContext:
        try:
            snapshot = self.store.read_strict_snapshot(goal_id)
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Strict context snapshot is invalid") from exc
        goal, baseline = self._load_baseline(snapshot, goal_id)
        baseline_proof = snapshot["baseline_proof"]
        baseline_event = self._source_event(
            snapshot,
            baseline_proof,
            self.user_authority,
            kind="user.baseline",
            subject=baseline.fingerprint,
            expected_payload=baseline.to_dict(),
        )
        evidence_snapshots: list[EvidenceSnapshot] = []
        evidence_events: list[tuple[SignedEvent, Payload, EvidenceSnapshot]] = []
        for head, proof, projection in zip(
            snapshot["evidence_heads"], snapshot["evidence_proofs"],
            [snapshot["evidence_projections"].get(item["subject_id"]) for item in snapshot["evidence_heads"]],
            strict=True,
        ):
            evidence = self._load_evidence(
                head, proof, projection, baseline,
                snapshot["source_events"][proof["source_event_id"]]["payload"],
            )
            if not evidence.valid_at(self.clock()) or parse_time(proof["expires_at"]) <= parse_time(self.clock()):
                raise self._strict_error("STALE", "Strict evidence proof expired")
            event_payload = {"goal_id": goal_id, "snapshot": evidence.to_dict()}
            evidence_event = self._source_event(
                snapshot,
                proof,
                self.observer_authority,
                kind="observer.evidence",
                subject=evidence.fingerprint,
                expected_payload=event_payload,
            )
            evidence_snapshots.append(evidence)
            evidence_events.append((evidence_event, proof, evidence))
        current = self._current_runtime_binding()
        expected_runtime = baseline_proof["runtime_binding_digest"]
        if current["binding_digest"] != expected_runtime:
            raise self._strict_error("STALE", "Baseline runtime identity has changed")
        for proof in snapshot["evidence_proofs"]:
            if (
                proof["runtime_binding_digest"] != expected_runtime
                or proof["policy_digest"] != self.policy.fingerprint
            ):
                raise self._strict_error("STALE", "Evidence belongs to a different runtime binding")
        self._reverify_source_proof(
            baseline_event,
            baseline_proof,
            purpose="user.baseline",
            binding=self._baseline_binding(baseline, expected_runtime),
            actor_type="USER",
        )
        for evidence_event, proof, evidence in evidence_events:
            self._reverify_source_proof(
                evidence_event,
                proof,
                purpose="observer.evidence",
                binding=self._evidence_binding(goal_id, baseline, evidence, expected_runtime),
                actor_type="SERVICE",
            )
        after = self._current_runtime_binding()
        if after["binding_digest"] != expected_runtime:
            raise self._strict_error("STALE", "Runtime identity changed during context verification")
        self._require_context_deadlines(
            baseline_proof, snapshot["evidence_proofs"], evidence_snapshots
        )
        evidence_snapshots.sort(key=lambda item: item.evidence_id)
        evidence_heads = [
            {
                "evidence_id": head["subject_id"],
                "proof_id": proof["proof_id"],
                "proof_digest": self.store.fingerprint(proof),
                "source_seq": proof["source_seq"],
            }
            for head, proof in zip(snapshot["evidence_heads"], snapshot["evidence_proofs"], strict=True)
        ]
        strict_binding = {
            "schema_version": "goalanchor.strict-context/v1",
            "goal_generation": goal.get("generation", 0),
            "baseline_proof_id": baseline_proof["proof_id"],
            "baseline_proof_digest": self.store.fingerprint(baseline_proof),
            "baseline_source_seq": baseline_proof["source_seq"],
            "evidence_heads": evidence_heads,
            "policy_digest": self.policy.fingerprint,
            "runtime_binding_digest": expected_runtime,
        }
        candidate = canonical({
            "baseline": baseline.to_dict(),
            "baseline_digest": baseline.fingerprint,
            "snapshots": [item.to_dict() for item in evidence_snapshots],
        })
        with self._runtime_lease(expected_runtime) as final_binding:
            try:
                final_snapshot = self.store.read_strict_snapshot(goal_id)
            except ContractValidationError as exc:
                raise self._strict_error("STALE", "Strict context changed during final verification") from exc
            if self.store.fingerprint(final_snapshot) != self.store.fingerprint(snapshot):
                raise self._strict_error("STALE", "Strict context dependencies changed during verification")
            self._require_context_deadlines(
                baseline_proof, snapshot["evidence_proofs"], evidence_snapshots
            )
            if final_binding["binding_digest"] != expected_runtime:
                raise self._strict_error("STALE", "Runtime identity changed before context return")
            return ReviewContext(
                goal=baseline.objective,
                prompt=baseline.plan,
                constraints=baseline.constraints,
                evidence_refs=tuple(item.evidence_id for item in evidence_snapshots),
                candidate=candidate,
                evidence_digests=tuple((item.evidence_id, item.sha256) for item in evidence_snapshots),
                strict_binding=strict_binding,
            )

    def _load_evidence(
        self,
        head: Payload,
        proof: Payload,
        projection: Payload | None,
        baseline: Baseline,
        event: Mapping[str, Any],
    ) -> EvidenceSnapshot:
        try:
            snapshot = EvidenceSnapshot.from_dict(proof["payload"]["snapshot"])
        except (KeyError, TypeError, ContractValidationError) as exc:
            raise self._strict_error("UNTRUSTED", "Strict evidence proof payload is malformed") from exc
        self._assert_persisted_provider_claims(proof)
        expected_projection = self.store.expected_evidence_projection(proof, event)
        if (
            proof.get("kind") != "evidence"
            or proof.get("goal_id") != baseline.goal_id
            or proof.get("baseline_digest") != baseline.fingerprint
            or proof.get("scope_digest") != baseline.fingerprint
            or proof.get("subject_id") != snapshot.evidence_id
            or proof.get("proof_id") != head.get("proof_id")
            or proof.get("source_seq") != head.get("source_seq")
            or snapshot.fingerprint != proof["payload"].get("snapshot_fingerprint")
            or projection is None
            or not self.store.same_json(projection, expected_projection)
            or projection.get("strict_proof_id") != proof.get("proof_id")
            or not self.store.same_json(projection.get("snapshot"), snapshot.to_dict())
        ):
            raise self._strict_error("UNTRUSTED", "Strict evidence projection does not match its proof")
        return snapshot

    def _require_context_deadlines(
        self,
        baseline_proof: Payload,
        evidence_proofs: list[Payload],
        evidence_snapshots: list[EvidenceSnapshot],
    ) -> None:
        now = parse_time(self.clock())
        if parse_time(baseline_proof["expires_at"]) <= now:
            raise self._strict_error("STALE", "Strict baseline proof expired")
        if any(parse_time(proof["expires_at"]) <= now for proof in evidence_proofs):
            raise self._strict_error("STALE", "Strict evidence proof expired")
        if any(not item.valid_at(now.isoformat()) for item in evidence_snapshots):
            raise self._strict_error("STALE", "Strict evidence snapshot expired")

    def _not_implemented(self, operation: str) -> NoReturn:
        raise self._strict_error("UNAVAILABLE", f"Strict {operation} is not available yet")

    @staticmethod
    def _strict_direction_head_key(goal_id: str, baseline_digest: str, decision_key: str) -> str:
        return fingerprint({
            "schema": "strict-direction-question-head/v1",
            "goal_id": goal_id,
            "baseline_digest": baseline_digest,
            "decision_key": decision_key,
            "kind": "DIRECTION",
        })

    def _assert_question_runtime_binding(self, proof: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
        if (
            proof.get("runtime_binding_digest") != binding.get("binding_digest")
            or proof.get("lease_generation") != binding.get("generation")
            or any(
                proof.get(name) != binding.get(name)
                for name in ("principal_id", "account_id", "session_id")
            )
        ):
            raise self._strict_error("STALE", "Strict question runtime binding differs from its Baseline proof")

    def _read_verified_question_context(
        self, goal_id: str
    ) -> tuple[Payload, Payload, Baseline, Payload, Payload]:
        try:
            snapshot = self.store.read_strict_question_snapshot(goal_id)
        except WorkflowStoreConflict as exc:
            raise self._strict_error(exc.code, "Strict question snapshot is invalid") from exc
        except sqlite3.Error as exc:
            raise self._strict_error("UNAVAILABLE", "Strict question storage is unavailable") from exc
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Strict question snapshot is malformed") from exc

        goal, baseline = self._load_baseline(snapshot, goal_id)
        proof = snapshot.get("baseline_proof")
        if proof is None:
            raise self._strict_error("STALE", "Strict question Baseline proof is unavailable")
        current = self._current_runtime_binding()
        self._assert_question_runtime_binding(proof, current)
        baseline_event = self._source_event(
            snapshot,
            proof,
            self.user_authority,
            kind="user.baseline",
            subject=baseline.fingerprint,
            expected_payload=baseline.to_dict(),
        )
        self._reverify_source_proof(
            baseline_event,
            proof,
            purpose="user.baseline",
            binding=self._baseline_binding(baseline, current["binding_digest"]),
            actor_type="USER",
        )
        after_verification = self._current_runtime_binding()
        self._assert_question_runtime_binding(proof, after_verification)
        if after_verification["binding_digest"] != current["binding_digest"]:
            raise self._strict_error("STALE", "Runtime binding changed during question verification")
        return snapshot, goal, baseline, proof, current

    def _normalize_strict_direction_question(self, question: Question, baseline: Baseline) -> Question:
        if not isinstance(question, Question) or question.kind != "DIRECTION":
            raise self._strict_error("UNTRUSTED", "Strict question must be a direction question")
        if (
            question.trigger_reason is None
            or not question.trigger_reason.strip()
            or question.decision_key is None
            or not question.decision_key.strip()
            or question.operation_digest is not None
            or question.approval_option_id is not None
            or question.authorization_context_digest is not None
            or question.question_revision != 1
        ):
            raise self._strict_error("UNTRUSTED", "Strict direction question has invalid authority fields")
        if (
            question.goal_id != baseline.goal_id
            or question.plan_revision != baseline.revision
            or question.baseline_digest != baseline.fingerprint
        ):
            raise self._strict_error("STALE", "Strict question is not bound to the active Baseline")
        dependency_map = baseline.decision_dependency_map
        if not dependency_map or question.decision_key not in dependency_map:
            raise self._strict_error("UNTRUSTED", "Question decision key is not approved by the Baseline")
        dependencies = dependency_map[question.decision_key]
        if tuple(question.blocking_items) != dependencies:
            raise self._strict_error("UNTRUSTED", "Question blockers differ from the approved Baseline mapping")

        now = parse_time(self.clock())
        expires_at = question.expires_at
        if expires_at is None:
            expires_at = (now + timedelta(seconds=self._question_ttl_seconds)).isoformat()
            question = replace(question, expires_at=expires_at)
        expires = parse_time(expires_at)
        if expires <= now:
            raise self._strict_error("STALE", "Strict question has expired")
        if expires > now + timedelta(seconds=self._question_ttl_seconds):
            raise self._strict_error("UNTRUSTED", "Strict question expiry exceeds the configured TTL")
        return question

    def _verify_strict_question_state(
        self,
        snapshot: Payload,
        baseline: Baseline,
        baseline_proof: Payload,
        binding: Payload,
    ) -> tuple[dict[str, Payload], dict[str, Payload], dict[str, QuestionRecord]]:
        self._assert_question_runtime_binding(baseline_proof, binding)
        goal_id = baseline.goal_id
        baseline_digest = baseline.fingerprint
        dependency_map = baseline.decision_dependency_map
        raw_questions = snapshot.get("questions")
        raw_heads = snapshot.get("question_heads")
        raw_events = snapshot.get("event_history")
        if not isinstance(raw_questions, (tuple, list)):
            raise self._strict_error("UNTRUSTED", "Strict question records are malformed")
        if not isinstance(raw_heads, (tuple, list)):
            raise self._strict_error("UNTRUSTED", "Strict question heads are malformed")
        if not isinstance(raw_events, (tuple, list)):
            raise self._strict_error("UNTRUSTED", "Strict question snapshot collections are malformed")

        questions: dict[str, Payload] = {}
        question_identities: dict[str, tuple[str, str]] = {}
        for row in raw_questions:
            if not isinstance(row, Mapping) or not isinstance(row.get("envelope"), Mapping):
                raise self._strict_error("UNTRUSTED", "Strict question row is malformed")
            envelope = dict(row["envelope"])
            record_data = envelope.get("record")
            question_data = record_data.get("question") if isinstance(record_data, Mapping) else None
            inner_baseline_digest = (
                question_data.get("baseline_digest") if isinstance(question_data, Mapping) else None
            )
            referenced = row.get("referenced")
            if not isinstance(referenced, bool):
                raise self._strict_error("UNTRUSTED", "Strict question reference marker is malformed")
            if (
                envelope.get("baseline_digest") != baseline_digest
                and inner_baseline_digest != baseline_digest
                and not referenced
            ):
                continue
            expected_fields = {
                "goal_id", "baseline_digest", "baseline_proof_id", "decision_key", "head_key",
                "question_id", "question_revision", "question_fingerprint", "question_record_fingerprint",
                "dedupe_key", "record", "created_at", "expires_at", "principal_id", "account_id",
                "session_id", "runtime_binding_digest", "lease_generation", "creation_event_id",
                "creation_event_digest",
            }
            if (
                set(envelope) != expected_fields
                or row.get("key") != envelope.get("question_id")
                or envelope.get("goal_id") != goal_id
                or envelope.get("baseline_digest") != baseline_digest
            ):
                raise self._strict_error("UNTRUSTED", "Strict question envelope has an invalid schema")
            try:
                record = QuestionRecord.from_dict(envelope["record"])
            except ContractValidationError as exc:
                raise self._strict_error("UNTRUSTED", "Strict question record is malformed") from exc
            question = record.question
            decision_key = question.decision_key
            if not isinstance(decision_key, str) or decision_key not in dependency_map:
                raise self._strict_error("UNTRUSTED", "Strict question decision key is not approved")
            expected_head_key = self._strict_direction_head_key(goal_id, baseline_digest, decision_key)
            if (
                record.state != "WAITING"
                or record.answer is not None
                or question.kind != "DIRECTION"
                or question.goal_id != goal_id
                or envelope.get("question_id") != question.question_id
                or envelope.get("decision_key") != decision_key
                or envelope.get("head_key") != expected_head_key
                or question.plan_revision != baseline.revision
                or question.baseline_digest != baseline_digest
                or tuple(question.blocking_items) != dependency_map[decision_key]
                or question.trigger_reason is None
                or not question.trigger_reason.strip()
                or question.operation_digest is not None
                or question.approval_option_id is not None
                or question.authorization_context_digest is not None
                or question.question_revision != 1
                or question.expires_at is None
                or envelope.get("expires_at") != question.expires_at
                or envelope.get("question_fingerprint") != question.fingerprint
                or envelope.get("question_record_fingerprint") != fingerprint(record.to_dict())
                or envelope.get("dedupe_key") != question.dedupe_key
                or envelope.get("question_revision") != 1
                or envelope.get("baseline_proof_id") != baseline_proof.get("proof_id")
                or envelope.get("runtime_binding_digest") != binding.get("binding_digest")
                or envelope.get("lease_generation") != binding.get("generation")
                or any(
                    envelope.get(name) != binding.get(name)
                    for name in ("principal_id", "account_id", "session_id")
                )
            ):
                raise self._strict_error("UNTRUSTED", "Strict question record does not match its signed Baseline")
            if parse_time(question.expires_at) <= parse_time(self.clock()):
                raise self._strict_error("STALE", "Strict question has expired and remains fail-closed")
            question_id = question.question_id
            if question_id in questions:
                raise self._strict_error("UNTRUSTED", "Strict question ID is duplicated")
            questions[question_id] = envelope
            question_identities[question_id] = (decision_key, expected_head_key)

        heads: dict[str, Payload] = {}
        head_question_ids: set[str] = set()
        for row in raw_heads:
            if not isinstance(row, Mapping) or not isinstance(row.get("head"), Mapping):
                raise self._strict_error("UNTRUSTED", "Strict question head row is malformed")
            head = dict(row["head"])
            referenced_head_key = row.get("referenced")
            if not isinstance(referenced_head_key, bool):
                raise self._strict_error("UNTRUSTED", "Strict question head reference marker is malformed")
            if head.get("baseline_digest") != baseline_digest:
                if referenced_head_key:
                    raise self._strict_error("UNTRUSTED", "Active Baseline question key has a stale payload")
                continue
            if head.get("goal_id") != goal_id:
                raise self._strict_error("UNTRUSTED", "Active Baseline question head has the wrong goal owner")
            decision_key = head.get("decision_key")
            if not isinstance(decision_key, str) or decision_key not in dependency_map:
                raise self._strict_error("UNTRUSTED", "Strict question head is not in the Baseline")
            expected_key = self._strict_direction_head_key(goal_id, baseline_digest, decision_key)
            expected_fields = {
                "goal_id", "baseline_digest", "decision_key", "kind", "question_id",
                "question_revision", "question_fingerprint", "dedupe_key",
            }
            if (
                set(head) != expected_fields
                or head.get("kind") != "DIRECTION"
                or row.get("key") != expected_key
                or head.get("question_revision") != 1
                or decision_key in heads
            ):
                raise self._strict_error("UNTRUSTED", "Strict question head has an invalid schema")
            head_question_id = head.get("question_id")
            if not isinstance(head_question_id, str):
                raise self._strict_error("UNTRUSTED", "Strict question head has no question ID")
            head_envelope = questions.get(head_question_id)
            if (
                head_envelope is None
                or head_envelope.get("decision_key") != decision_key
                or row.get("key") != head_envelope.get("head_key")
                or question_identities.get(head_question_id) != (decision_key, expected_key)
                or head.get("question_fingerprint") != head_envelope.get("question_fingerprint")
                or head.get("dedupe_key") != head_envelope.get("dedupe_key")
            ):
                raise self._strict_error("UNTRUSTED", "Strict question head has no matching question")
            heads[decision_key] = head
            head_question_ids.add(head_question_id)

        events: dict[str, Payload] = {}
        event_question_ids: set[str] = set()
        for row in raw_events:
            if not isinstance(row, Mapping) or not isinstance(row.get("event"), (Mapping, SignedEvent)):
                raise self._strict_error("UNTRUSTED", "Strict signed event history row is malformed")
            raw_event = row["event"]
            if isinstance(raw_event, SignedEvent):
                event = raw_event
            else:
                try:
                    event = SignedEvent.from_dict(raw_event)
                except ContractValidationError as exc:
                    raise self._strict_error("UNTRUSTED", "Strict signed event history is malformed") from exc
            event_id = row.get("event_id")
            if not isinstance(event_id, str) or event.event_id != event_id:
                raise self._strict_error("UNTRUSTED", "Strict signed event history identity is invalid")
            payload_digest = row.get("payload_digest")
            if not _is_canonical_event_digest(payload_digest):
                raise self._strict_error("UNTRUSTED", "Strict signed event storage digest is malformed")
            authorities = tuple(
                authority
                for authority in (self.user_authority, self.observer_authority)
                if event.source == authority.source
            )
            if not authorities:
                raise self._strict_error("UNTRUSTED", "Strict signed event history has an unknown source")
            signature_valid = False
            for authority in authorities:
                try:
                    self._verify_cached_event_signature(
                        event,
                        authority,
                        event_id=event_id,
                        payload_digest=payload_digest,
                    )
                except StrictWorkflowError as exc:
                    if exc.code != "UNTRUSTED":
                        raise
                    continue
                signature_valid = True
                break
            if not signature_valid:
                raise self._strict_error("UNTRUSTED", "Strict signed event history signature is invalid")
            if event.kind != "controller.question.created":
                continue
            try:
                self.observer_authority.verify(event, kind="controller.question.created")
            except ContractValidationError as exc:
                raise self._strict_error("UNTRUSTED", "Strict question event signature is invalid") from exc
            payload = event.payload
            event_question_id = payload.get("question_id")
            if (
                payload.get("baseline_digest") != baseline_digest
                and (not isinstance(event_question_id, str) or event_question_id not in questions)
            ):
                continue
            if not isinstance(event_question_id, str):
                raise self._strict_error("UNTRUSTED", "Strict question event has no question ID")
            event_envelope = questions.get(event_question_id)
            if event_envelope is None or event_envelope.get("creation_event_id") != event_id:
                raise self._strict_error("UNTRUSTED", "Strict question creation event is orphaned")
            record = QuestionRecord.from_dict(event_envelope["record"])
            question = record.question
            event_decision_key = question.decision_key
            if not isinstance(event_decision_key, str):
                raise self._strict_error("UNTRUSTED", "Strict question decision key is missing")
            expected_head_key = self._strict_direction_head_key(
                goal_id, baseline_digest, event_decision_key
            )
            expected_payload = {
                "goal_id": goal_id,
                "baseline_digest": baseline_digest,
                "baseline_proof_id": baseline_proof["proof_id"],
                "decision_key": event_decision_key,
                "head_key": expected_head_key,
                "question_id": question.question_id,
                "question_revision": 1,
                "question_fingerprint": question.fingerprint,
                "question_record_fingerprint": fingerprint(record.to_dict()),
                "dedupe_key": question.dedupe_key,
                "expires_at": question.expires_at,
                "runtime_binding_digest": binding["binding_digest"],
                "lease_generation": binding["generation"],
            }
            if (
                event.subject != question.fingerprint
                or event_envelope.get("question_id") != question.question_id
                or event_envelope.get("decision_key") != event_decision_key
                or event_envelope.get("expires_at") != question.expires_at
                or event.created_at != event_envelope.get("created_at")
                or self.store.fingerprint(event.to_dict()) != event_envelope.get("creation_event_digest")
                or event_envelope.get("head_key") != expected_head_key
                or not self.store.same_json(payload, expected_payload)
            ):
                raise self._strict_error("UNTRUSTED", "Strict question creation event does not match its record")
            self._local_event_payload(
                self.observer_authority,
                event,
                kind="controller.question.created",
                subject=question.fingerprint,
                expected_payload=expected_payload,
            )
            events[event_id] = event_envelope
            event_question_ids.add(event_question_id)

        if set(questions) != head_question_ids or head_question_ids != event_question_ids:
            raise self._strict_error("UNTRUSTED", "Strict questions, heads, and events are not bidirectionally bound")
        return heads, events, {
            question_id: QuestionRecord.from_dict(envelope["record"])
            for question_id, envelope in questions.items()
        }

    def _validate_question_context_binding(self, proof: Payload, binding: Payload) -> None:
        self._assert_question_runtime_binding(proof, binding)
        if parse_time(proof["expires_at"]) <= parse_time(self.clock()):
            raise self._strict_error("STALE", "Strict Baseline proof expired")

    def review_packets(self, goal_id: str, bundle: Payload) -> dict[str, Payload]:
        self._not_implemented("review packet")

    def submit_review(self, goal_id: str, bundle: Payload, event: SignedEvent) -> Payload:
        self._not_implemented("review submission")

    def request_question(self, question: Question) -> QuestionRecord:
        if not isinstance(question, Question):
            raise self._strict_error("UNTRUSTED", "Strict question must be a Question value")
        snapshot, goal, baseline, baseline_proof, binding = self._read_verified_question_context(
            question.goal_id
        )
        normalized = self._normalize_strict_direction_question(question, baseline)
        head_key = self._strict_direction_head_key(
            baseline.goal_id, baseline.fingerprint, normalized.decision_key or ""
        )
        deadline = self._deadline(baseline_proof["expires_at"], normalized.expires_at)
        with self._runtime_lease(binding["binding_digest"]) as lease:
            self._validate_question_context_binding(baseline_proof, lease)
            heads, _events, records = self._verify_strict_question_state(
                snapshot, baseline, baseline_proof, lease
            )
            current_head = heads.get(normalized.decision_key or "")
            if current_head is not None:
                current_record = records[current_head["question_id"]]
                if current_record.question.dedupe_key != normalized.dedupe_key:
                    raise self._strict_error("CONFLICT", "A different question already owns this decision head")
                return current_record
            if any(
                isinstance(row, Mapping) and row.get("key") == normalized.question_id
                for row in snapshot.get("questions", ())
            ):
                raise self._strict_error("CONFLICT", "Strict question ID is already in use")

        record = QuestionRecord(normalized, "WAITING", None)
        record_digest = fingerprint(record.to_dict())
        event_payload = {
            "goal_id": baseline.goal_id,
            "baseline_digest": baseline.fingerprint,
            "baseline_proof_id": baseline_proof["proof_id"],
            "decision_key": normalized.decision_key,
            "head_key": head_key,
            "question_id": normalized.question_id,
            "question_revision": 1,
            "question_fingerprint": normalized.fingerprint,
            "question_record_fingerprint": record_digest,
            "dedupe_key": normalized.dedupe_key,
            "expires_at": normalized.expires_at,
            "runtime_binding_digest": binding["binding_digest"],
            "lease_generation": binding["generation"],
        }
        event = self.observer_authority.sign(
            "controller.question.created",
            normalized.fingerprint,
            event_payload,
            at=self.clock(),
        )
        envelope = {
            "goal_id": baseline.goal_id,
            "baseline_digest": baseline.fingerprint,
            "baseline_proof_id": baseline_proof["proof_id"],
            "decision_key": normalized.decision_key,
            "head_key": head_key,
            "question_id": normalized.question_id,
            "question_revision": 1,
            "question_fingerprint": normalized.fingerprint,
            "question_record_fingerprint": record_digest,
            "dedupe_key": normalized.dedupe_key,
            "record": record.to_dict(),
            "created_at": event.created_at,
            "expires_at": normalized.expires_at,
            "principal_id": binding["principal_id"],
            "account_id": binding["account_id"],
            "session_id": binding["session_id"],
            "runtime_binding_digest": binding["binding_digest"],
            "lease_generation": binding["generation"],
            "creation_event_id": event.event_id,
            "creation_event_digest": self.store.fingerprint(event.to_dict()),
        }
        head = {
            "goal_id": baseline.goal_id,
            "baseline_digest": baseline.fingerprint,
            "decision_key": normalized.decision_key,
            "kind": "DIRECTION",
            "question_id": normalized.question_id,
            "question_revision": 1,
            "question_fingerprint": normalized.fingerprint,
            "dedupe_key": normalized.dedupe_key,
        }
        expected = {
            ("goal", baseline.goal_id): self.store.fingerprint(goal),
            ("baseline", baseline.fingerprint): self.store.fingerprint(snapshot["baseline_projection"]),
            ("strict_question", normalized.question_id): None,
            ("strict_question_request_index", head_key): None,
        }
        try:
            self.store.commit_strict_question(
                [
                    ("strict_question", normalized.question_id, envelope),
                    ("strict_question_request_index", head_key, head),
                ],
                expected,
                event.event_id,
                event.to_dict(),
                snapshot["baseline_head"],
                self._runtime_lease,
                binding["binding_digest"],
                binding["generation"],
                valid_until=deadline,
                clock=self.clock,
            )
        except WorkflowStoreConflict as exc:
            if exc.code in {"STALE", "CONFLICT"}:
                return self._recover_strict_question_create(normalized, exc)
            raise self._strict_error(exc.code, "Strict question creation was rejected") from exc
        except StrictWorkflowError:
            raise
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Strict question commit is invalid") from exc
        except sqlite3.Error as exc:
            raise self._strict_error("UNAVAILABLE", "Strict question storage is unavailable") from exc
        return record

    def _recover_strict_question_create(
        self, question: Question, original: WorkflowStoreConflict
    ) -> QuestionRecord:
        snapshot, _goal, baseline, proof, binding = self._read_verified_question_context(question.goal_id)
        if baseline.fingerprint != question.baseline_digest:
            raise self._strict_error("STALE", "Baseline changed during Strict question creation") from original
        with self._runtime_lease(binding["binding_digest"]) as lease:
            self._validate_question_context_binding(proof, lease)
            heads, _events, records = self._verify_strict_question_state(snapshot, baseline, proof, lease)
            head = heads.get(question.decision_key or "")
            if head is not None:
                current = records[head["question_id"]]
                if current.question.dedupe_key == question.dedupe_key:
                    return current
                raise self._strict_error("CONFLICT", "A different question won the decision head") from original
            if any(
                isinstance(row, Mapping) and row.get("key") == question.question_id
                for row in snapshot.get("questions", ())
            ):
                raise self._strict_error("CONFLICT", "Strict question ID is already in use") from original
        if original.code == "CONFLICT":
            raise self._strict_error("CONFLICT", "Strict question event ID is already in use") from original
        raise self._strict_error("STALE", "Strict question creation lost a concurrent Baseline update") from original

    def request_authorization(self, operation_id: str) -> QuestionRecord:
        self._not_implemented("authorization question")

    def answer(self, question_id: str, event: SignedEvent) -> QuestionRecord:
        self._not_implemented("question answer")

    def transition_question(self, question_id: str, state: str, event: SignedEvent) -> QuestionRecord:
        self._not_implemented("question transition")

    def update_presentation(self, goal_id: str, proof: Payload, event: SignedEvent) -> Payload:
        self._not_implemented("presentation update")

    def work_item_decision(self, goal_id: str, work_item_id: str) -> str:
        if (
            not isinstance(goal_id, str)
            or not goal_id.strip()
            or goal_id != goal_id.strip()
            or not isinstance(work_item_id, str)
            or not work_item_id.strip()
            or work_item_id != work_item_id.strip()
        ):
            raise self._strict_error("UNTRUSTED", "Strict work-item decision requires stable identifiers")
        snapshot, _goal, baseline, proof, binding = self._read_verified_question_context(goal_id)
        with self._runtime_lease(binding["binding_digest"]) as lease:
            self._validate_question_context_binding(proof, lease)
            heads, _events, records = self._verify_strict_question_state(snapshot, baseline, proof, lease)
            relevant_decisions = [
                decision_key
                for decision_key, dependencies in baseline.decision_dependency_map.items()
                if work_item_id in dependencies
            ]
            for decision_key in relevant_decisions:
                head = heads.get(decision_key)
                if head is None:
                    continue
                record = records.get(head["question_id"])
                if record is None or record.state != "WAITING" or record.answer is not None:
                    raise self._strict_error("UNTRUSTED", "Strict question state is not a verified pending decision")
                return "WAIT"
            return "ALLOW"

    def _validate_strict_operation_record(
        self, record: Any, operation: Operation, baseline: Baseline
    ) -> None:
        path_schema_digest = self._operation_path_schema_digest(operation.tool)
        expected = {
            "goal_id": operation.goal_id,
            "baseline_digest": baseline.fingerprint,
            "operation_digest": operation.fingerprint,
            "operation": operation.to_dict(),
            "path_schema_digest": path_schema_digest,
        }
        expected_keys = set(expected) | {"registration_event_id", "registration_event_digest"}
        if not isinstance(record, Mapping) or set(record) != expected_keys:
            raise self._strict_error("UNTRUSTED", "Strict operation record has an invalid schema")
        if any(not self.store.same_json(record.get(key), value) for key, value in expected.items()):
            raise self._strict_error("UNTRUSTED", "Strict operation record content changed")
        event_id = record.get("registration_event_id")
        event_digest = record.get("registration_event_digest")
        if not isinstance(event_id, str) or not event_id or not isinstance(event_digest, str):
            raise self._strict_error("UNTRUSTED", "Strict operation registration provenance is missing")
        raw_event = self.store.get_event(event_id)
        if not isinstance(raw_event, Mapping):
            raise self._strict_error("UNTRUSTED", "Strict operation registration event is missing")
        try:
            event = SignedEvent.from_dict(raw_event)
            if event.event_id != event_id or self.store.fingerprint(event.to_dict()) != event_digest:
                raise ContractValidationError("Strict operation registration event digest changed")
            event_payload = {
                "operation": operation.to_dict(),
                "operation_digest": operation.fingerprint,
                "path_schema_digest": path_schema_digest,
            }
            self._local_event_payload(
                self.observer_authority,
                event,
                kind="controller.operation",
                subject=operation.fingerprint,
                expected_payload=event_payload,
            )
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Strict operation registration event is invalid") from exc

    def _operation_path_schema_digest(self, tool: str) -> str:
        fields = self._operation_path_schemas.get(tool)
        if fields is None:
            raise self._strict_error("UNAVAILABLE", "Strict operation tool path schema is unavailable")
        return fingerprint({"tool": tool, "path_fields": sorted(fields)})

    def _validate_operation_scope(self, operation: Operation) -> str:
        try:
            path_fields = self._operation_path_schemas.get(operation.tool)
            if path_fields is None:
                raise self._strict_error("UNAVAILABLE", "Strict operation tool path schema is unavailable")
            workspace = Path(self.policy.workspace).resolve()
            cwd = Path(operation.cwd).resolve()
            if not cwd.is_relative_to(workspace):
                raise self._strict_error("UNTRUSTED", "Operation cwd is outside the policy workspace")
            for raw_path in _path_argument_values(operation.arguments, path_fields):
                path = (cwd / raw_path).resolve()
                if not path.is_relative_to(workspace):
                    raise self._strict_error("UNTRUSTED", "Operation argument path is outside the policy workspace")
            for raw_path, expected_digest in operation.resource_preconditions:
                declared_resource = cwd / raw_path
                is_symlink = declared_resource.is_symlink()
                resource = declared_resource.resolve()
                if not resource.is_relative_to(workspace):
                    raise self._strict_error("UNTRUSTED", "Operation resource is outside the policy workspace")
                exists = resource.exists() or is_symlink
                actual_digest = hashlib.sha256(resource.read_bytes()).hexdigest() if resource.is_file() else (
                    "present" if exists else None
                )
                if actual_digest != expected_digest:
                    raise self._strict_error("STALE", "Operation resource precondition changed")
            return self._operation_path_schema_digest(operation.tool)
        except StrictWorkflowError:
            raise
        except (OSError, RuntimeError, ValueError, ContractValidationError) as exc:
            raise self._strict_error("UNTRUSTED", "Operation workspace or resource is unavailable") from exc

    def register_operation(self, operation: Operation) -> Payload:
        if not isinstance(operation, Operation):
            raise self._strict_error("UNTRUSTED", "Strict operation must be an Operation value")
        try:
            snapshot = self.store.read_strict_snapshot(operation.goal_id)
        except WorkflowStoreConflict as exc:
            raise self._strict_error(exc.code, "Strict operation baseline snapshot is invalid") from exc
        except ContractValidationError as exc:
            raise self._strict_error("UNTRUSTED", "Strict operation baseline snapshot is invalid") from exc
        except sqlite3.Error as exc:
            raise self._strict_error("UNAVAILABLE", "Strict operation baseline storage is unavailable") from exc
        _goal, baseline = self._load_baseline(snapshot, operation.goal_id)
        baseline_proof = snapshot.get("baseline_proof")
        if baseline_proof is None:
            raise self._strict_error("STALE", "Strict operation baseline proof is unavailable")
        if operation.baseline_digest != baseline.fingerprint:
            raise self._strict_error("STALE", "Operation baseline is not the active Strict baseline")
        if operation.tool not in self.policy.allowed_tools or operation.tool in self.policy.denied_tools:
            raise self._strict_error("UNTRUSTED", "Operation tool is forbidden by hard policy")
        path_schema_digest = self._validate_operation_scope(operation)

        decision_key = f"operation:{operation.work_item_id}"
        dependencies = baseline.decision_dependency_map.get(decision_key)
        if dependencies is None or operation.work_item_id not in dependencies:
            raise self._strict_error("UNTRUSTED", "Operation work item is not approved by the active baseline")

        baseline_event = self._source_event(
            snapshot,
            baseline_proof,
            self.user_authority,
            kind="user.baseline",
            subject=baseline.fingerprint,
            expected_payload=baseline.to_dict(),
        )
        current = self._current_runtime_binding()
        if current["binding_digest"] != baseline_proof.get("runtime_binding_digest"):
            raise self._strict_error("STALE", "Baseline runtime identity has changed")
        self._reverify_source_proof(
            baseline_event,
            baseline_proof,
            purpose="user.baseline",
            binding=self._baseline_binding(baseline, current["binding_digest"]),
            actor_type="USER",
        )
        after_verification = self._current_runtime_binding()
        if after_verification["binding_digest"] != current["binding_digest"]:
            raise self._strict_error("STALE", "Runtime identity changed during operation verification")

        record_content = {
            "goal_id": operation.goal_id,
            "baseline_digest": baseline.fingerprint,
            "operation_digest": operation.fingerprint,
            "operation": operation.to_dict(),
            "path_schema_digest": path_schema_digest,
        }
        deadline = self._deadline(baseline_proof["expires_at"])
        try:
            with self._runtime_lease(current["binding_digest"]):
                if parse_time(self.clock()) >= parse_time(deadline):
                    raise self._strict_error("STALE", "Strict baseline proof expired")
                try:
                    active_snapshot = self.store.read_strict_snapshot(operation.goal_id)
                except WorkflowStoreConflict as exc:
                    raise self._strict_error(exc.code, "Strict baseline changed during operation registration") from exc
                except ContractValidationError as exc:
                    raise self._strict_error("STALE", "Strict baseline changed during operation registration") from exc
                except sqlite3.Error as exc:
                    raise self._strict_error("UNAVAILABLE", "Strict operation baseline storage is unavailable") from exc
                active_goal, active_baseline = self._load_baseline(active_snapshot, operation.goal_id)
                active_proof = active_snapshot.get("baseline_proof")
                if (
                    active_proof is None
                    or active_baseline.fingerprint != baseline.fingerprint
                    or active_proof.get("proof_id") != baseline_proof.get("proof_id")
                    or active_proof.get("runtime_binding_digest") != current["binding_digest"]
                ):
                    raise self._strict_error("STALE", "Active Strict baseline changed during operation registration")
                old = self.store.get("strict_operation", operation.operation_id)
                if old is not None:
                    try:
                        replay_snapshot = self.store.read_strict_snapshot(operation.goal_id)
                    except WorkflowStoreConflict as exc:
                        raise self._strict_error(exc.code, "Strict baseline changed during operation replay") from exc
                    except ContractValidationError as exc:
                        raise self._strict_error("STALE", "Strict baseline changed during operation replay") from exc
                    except sqlite3.Error as exc:
                        raise self._strict_error(
                            "UNAVAILABLE", "Strict operation baseline storage is unavailable"
                        ) from exc
                    _replay_goal, replay_baseline = self._load_baseline(replay_snapshot, operation.goal_id)
                    replay_proof = replay_snapshot.get("baseline_proof")
                    if (
                        replay_proof is None
                        or replay_baseline.fingerprint != baseline.fingerprint
                        or replay_proof.get("proof_id") != baseline_proof.get("proof_id")
                        or replay_proof.get("runtime_binding_digest") != current["binding_digest"]
                    ):
                        raise self._strict_error("STALE", "Active Strict baseline changed during operation replay")
                    self._validate_strict_operation_record(old, operation, replay_baseline)
                    if self._validate_operation_scope(operation) != path_schema_digest:
                        raise self._strict_error("STALE", "Operation path schema changed during replay")
                    if parse_time(self.clock()) >= parse_time(deadline):
                        raise self._strict_error("STALE", "Strict baseline proof expired")
                    return operation.to_dict()
                expected = {
                    ("goal", operation.goal_id): self.store.fingerprint(active_goal),
                    ("baseline", baseline.fingerprint): self.store.fingerprint(active_snapshot["baseline_projection"]),
                    ("strict_operation", operation.operation_id): None,
                }
                event = self.observer_authority.sign(
                    "controller.operation",
                    operation.fingerprint,
                    {
                        "operation": operation.to_dict(),
                        "operation_digest": operation.fingerprint,
                        "path_schema_digest": path_schema_digest,
                    },
                    at=self.clock(),
                )

                def validate_scope_at_commit() -> None:
                    self._validate_operation_scope(operation)

                record = {
                    **record_content,
                    "registration_event_id": event.event_id,
                    "registration_event_digest": self.store.fingerprint(event.to_dict()),
                }
                self.store.commit_many(
                    [("strict_operation", operation.operation_id, record)],
                    expected,
                    ((event.event_id, event.to_dict()),),
                    valid_until=deadline,
                    clock=self.clock,
                    pre_commit_check=validate_scope_at_commit,
                )
        except StrictWorkflowError:
            raise
        except ContractValidationError as exc:
            # Concurrent exact registrations converge to the winning canonical record.
            try:
                latest = self.store.get("strict_operation", operation.operation_id)
            except sqlite3.Error as store_exc:
                raise self._strict_error("UNAVAILABLE", "Strict operation storage is unavailable") from store_exc
            if latest is not None:
                self._validate_strict_operation_record(latest, operation, baseline)
                try:
                    current_snapshot = self.store.read_strict_snapshot(operation.goal_id)
                    _latest_goal, latest_baseline = self._load_baseline(current_snapshot, operation.goal_id)
                except WorkflowStoreConflict as stale_exc:
                    raise self._strict_error(stale_exc.code, "Strict operation changed during registration") from exc
                except StrictWorkflowError:
                    raise
                except ContractValidationError as stale_exc:
                    raise self._strict_error("STALE", "Strict operation changed during registration") from stale_exc
                except sqlite3.Error as store_exc:
                    raise self._strict_error(
                        "UNAVAILABLE", "Strict operation baseline storage is unavailable"
                    ) from store_exc
                if latest_baseline.fingerprint == baseline.fingerprint:
                    latest_binding = self._current_runtime_binding()
                    if latest_binding["binding_digest"] != current["binding_digest"]:
                        raise self._strict_error(
                            "STALE", "Runtime identity changed during operation replay"
                        ) from exc
                    with self._runtime_lease(latest_binding["binding_digest"]) as recovery_binding:
                        if parse_time(self.clock()) >= parse_time(deadline):
                            raise self._strict_error("STALE", "Strict baseline proof expired") from exc
                        try:
                            current_record = self.store.get("strict_operation", operation.operation_id)
                        except sqlite3.Error as store_exc:
                            raise self._strict_error(
                                "UNAVAILABLE", "Strict operation storage is unavailable"
                            ) from store_exc
                        if current_record is not None:
                            try:
                                final_snapshot = self.store.read_strict_snapshot(operation.goal_id)
                            except WorkflowStoreConflict as stale_exc:
                                raise self._strict_error(
                                    stale_exc.code, "Strict operation changed during conflict recovery"
                                ) from exc
                            except ContractValidationError as stale_exc:
                                raise self._strict_error(
                                    "STALE", "Strict operation changed during conflict recovery"
                                ) from stale_exc
                            except sqlite3.Error as store_exc:
                                raise self._strict_error(
                                    "UNAVAILABLE", "Strict operation baseline storage is unavailable"
                                ) from store_exc
                            _final_goal, final_baseline = self._load_baseline(final_snapshot, operation.goal_id)
                            final_proof = final_snapshot.get("baseline_proof")
                            if (
                                final_proof is None
                                or final_baseline.fingerprint != baseline.fingerprint
                                or final_proof.get("proof_id") != baseline_proof.get("proof_id")
                                or final_proof.get("runtime_binding_digest") != recovery_binding["binding_digest"]
                            ):
                                raise self._strict_error(
                                    "STALE", "Active Strict baseline changed during conflict recovery"
                                ) from exc
                            if recovery_binding["binding_digest"] != latest_binding["binding_digest"]:
                                raise self._strict_error(
                                    "STALE", "Runtime identity changed during conflict recovery"
                                ) from exc
                            self._validate_strict_operation_record(current_record, operation, final_baseline)
                            if self._validate_operation_scope(operation) != path_schema_digest:
                                raise self._strict_error(
                                    "STALE", "Operation path schema or resources changed during conflict recovery"
                                ) from exc
                            if parse_time(self.clock()) >= parse_time(deadline):
                                raise self._strict_error("STALE", "Strict baseline proof expired") from exc
                            return operation.to_dict()
            message = str(exc)
            code = "STALE" if "stale" in message.lower() or "expired" in message.lower() else "UNTRUSTED"
            raise self._strict_error(code, message) from exc
        except sqlite3.Error as exc:
            raise self._strict_error("UNAVAILABLE", "Strict operation storage is unavailable") from exc
        return operation.to_dict()

    def authorize_scope(self, goal_id: str, scope_id: str, event: SignedEvent) -> Payload:
        self._not_implemented("scope authorization")

    def revoke_authorization(self, grant_id: str, event: SignedEvent) -> Payload:
        self._not_implemented("grant revocation")

    def cancel(self, goal_id: str, event: SignedEvent) -> Payload:
        self._not_implemented("cancellation")

    def reconcile(self, attempt_id: str, event: SignedEvent) -> Payload:
        self._not_implemented("reconciliation")

    def accept_risk(self, goal_id: str, finding_id: str, event: SignedEvent) -> Payload:
        self._not_implemented("risk acceptance")

    def execute(self, operation_id: str, attempt_id: str, executor: Callable[[Operation], Payload]) -> Payload:
        self._not_implemented("execution")

    def resume_transport(
        self,
        attempt_id: str,
        event: SignedEvent,
        proof: SignedEvent,
        executor: Callable[[Operation], Payload],
    ) -> Payload:
        self._not_implemented("transport recovery")

    def observe(self, goal_id: str, event: SignedEvent) -> Payload:
        self._not_implemented("acceptance observation")

    def reuse_observation(self, goal_id: str, event: SignedEvent) -> Payload:
        self._not_implemented("observation reuse")

    def close(self, goal_id: str) -> Payload:
        self._not_implemented("closure")
