"""Shared workflow contracts used by legacy and strict execution paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .core import ContractValidationError
from .host_contract import validate_host_binding
from .review_service import ReviewContext
from .workflow_protocol import SignedEvent, fingerprint, strings

Payload = dict[str, Any]

class TrustedEventProofProvider(Protocol):
    """Verify a source event against host trust.

    A verifier may refresh nonce, proof digest, and expiry on a later verification.
    The immutable claims (actor identity, source event, purpose, subject, and binding)
    must remain exact. A refreshed response never extends an already persisted proof.
    """

    def verify(
        self,
        event: SignedEvent | dict[str, Any],
        *,
        purpose: str,
        binding: dict[str, Any],
    ) -> dict[str, Any] | None: ...

class TrustedRuntimeBindingProvider(Protocol):
    """Trusted composition-root source for the live host identity and exclusive lease."""

    def current_runtime_binding(self) -> dict[str, Any]: ...

    def acquire_lease(self, expected_digest: str) -> Any: ...

class TrustedProofUnavailable(Exception):
    """The trusted proof service could not produce a decision."""

class TrustedProofRejected(Exception):
    """The trusted proof service rejected the event as unauthenticated."""

class TrustedBindingUnavailable(Exception):
    """The trusted host identity or lease service is unavailable."""

class TrustedBindingStale(Exception):
    """The requested runtime binding no longer matches the live host identity."""

class TrustedReviewProofProvider(Protocol):
    """Authenticate host review sources and return goalanchor.review-proof/v1.

    The composition root supplies this service; it is never a model-facing
    validator. It authenticates the embedded host.review.completed sources,
    their actual packet/result transport, independent sessions and live
    principal/account provenance. normalize_review_proof only checks binding
    and cannot substitute for that authentication. Return None or reject when
    provenance is absent. Persisted claims, including expiry, stay immutable;
    subsequent verification must not mint a longer-lived replacement proof.
    """

    def verify(
        self,
        review: dict[str, Any],
        *,
        expected_policy_digest: str,
        expected_context: ReviewContext,
        expected_host_binding: str | None,
    ) -> dict[str, Any] | None: ...

class StrictWorkflowError(ContractValidationError):
    """A fail-closed Strict Workflow error with a stable machine-readable code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code

@dataclass(frozen=True)
class WorkflowPolicy:
    workspace: str
    allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...] = ()
    grant_ttl_seconds: int = 3600
    acceptable_risks: tuple[str, ...] = ()
    host_binding: str | None = None

    def __post_init__(self) -> None:
        if self.host_binding is not None:
            object.__setattr__(self, "host_binding", validate_host_binding(self.host_binding))
        object.__setattr__(self, "workspace", str(Path(self.workspace).resolve()))
        for name in ("allowed_tools", "denied_tools", "acceptable_risks"):
            object.__setattr__(self, name, strings(getattr(self, name), name))
        if isinstance(self.grant_ttl_seconds, bool) or not isinstance(self.grant_ttl_seconds, int):
            raise ContractValidationError("grant TTL must be an integer")
        if not 1 <= self.grant_ttl_seconds <= 86400:
            raise ContractValidationError("grant TTL must be between 1 and 86400 seconds")

    @property
    def fingerprint(self) -> str:
        return fingerprint(dict(self.__dict__))
