"""Small, fail-closed efficiency governance primitives."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .core import ContractValidationError


@dataclass(frozen=True)
class ComplexityResult:
    score: int
    level: str
    signals: dict[str, int]


class TaskComplexityEngine:
    def assess(
        self, *, dependency_count: int, risk: str, blocking_uncertainty: bool, evidence_change_count: int
    ) -> ComplexityResult:
        if type(dependency_count) is not int or dependency_count < 0:
            raise ValueError("dependency_count must be a non-negative integer")
        if type(evidence_change_count) is not int or evidence_change_count < 0:
            raise ValueError("evidence_change_count must be a non-negative integer")
        if type(blocking_uncertainty) is not bool:
            raise ValueError("blocking_uncertainty must be a boolean")
        risk_score = {"LOW": 0, "MEDIUM": 1, "HIGH": 3, "CRITICAL": 4}.get(risk)
        if risk_score is None:
            raise ValueError("risk must be LOW, MEDIUM, HIGH, or CRITICAL")
        signals = {
            "dependencies": min(dependency_count, 3),
            "risk": risk_score,
            "blocking_uncertainty": int(blocking_uncertainty),
            "evidence_changes": min(evidence_change_count, 1),
        }
        score = sum(signals.values())
        level = "LOW" if score <= 2 else "MEDIUM" if score <= 5 else "HIGH"
        return ComplexityResult(score, level, signals)


@dataclass
class BudgetGovernor:
    token_budget: int
    time_budget_seconds: float
    reason: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if type(self.token_budget) is not int or self.token_budget < 1:
            raise ValueError("token_budget must be positive")
        if (
            type(self.time_budget_seconds) not in (int, float)
            or not math.isfinite(float(self.time_budget_seconds))
            or self.time_budget_seconds <= 0
        ):
            raise ValueError("time_budget_seconds must be positive")

    def allow(self, *, tokens_used: int | None, elapsed_seconds: float) -> bool:
        if tokens_used is None:
            self.reason = "UNKNOWN_USAGE"
            return False
        if type(tokens_used) is not int or tokens_used < 0:
            self.reason = "INVALID_USAGE"
            return False
        if (
            type(elapsed_seconds) not in (int, float)
            or not math.isfinite(float(elapsed_seconds))
            or elapsed_seconds < 0
        ):
            self.reason = "INVALID_USAGE"
            return False
        if tokens_used > self.token_budget:
            self.reason = "TOKEN_BUDGET_EXCEEDED"
            return False
        if elapsed_seconds > self.time_budget_seconds:
            self.reason = "TIME_BUDGET_EXCEEDED"
            return False
        self.reason = "WITHIN_BUDGET"
        return True


@dataclass
class ProgressWatchdog:
    repeat_threshold: int = 3
    _counts: dict[tuple[str, str], int] = field(default_factory=dict, init=False)
    reason: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if type(self.repeat_threshold) is not int or self.repeat_threshold < 1:
            raise ValueError("repeat_threshold must be positive")

    def observe(self, operation: str, fingerprint: str) -> bool:
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation must be a non-empty string")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("fingerprint must be a non-empty string")
        key = (operation, fingerprint)
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count > self.repeat_threshold:
            self.reason = "REPEATED_OPERATION"
            return True
        self.reason = "PROGRESS_OBSERVED"
        return False


@dataclass
class PromptOptimizer:
    _cache: dict[str, tuple[str, int]] = field(default_factory=dict, init=False)

    def remember(self, context_fingerprint: str, response: str, *, token_saving: int) -> None:
        if (
            not isinstance(context_fingerprint, str)
            or not context_fingerprint.strip()
            or not isinstance(response, str)
            or not response.strip()
            or type(token_saving) is not int
            or token_saving <= 0
        ):
            raise ValueError("context, response, and positive token_saving are required")
        self._cache[context_fingerprint] = (response, token_saving)

    def reuse(self, context_fingerprint: str) -> str | None:
        entry = self._cache.get(context_fingerprint)
        return None if entry is None or entry[1] <= 0 else entry[0]


@dataclass(frozen=True)
class EfficiencyDecision:
    allowed: bool
    reason: str
    complexity: ComplexityResult
    tokens_used: int | None
    elapsed_seconds: float
    progress_alert: bool
    token_status: str


@dataclass(frozen=True)
class EfficiencyObservation:
    """One measured workflow-boundary observation supplied by a host provider."""

    dependency_count: int
    risk: str
    blocking_uncertainty: bool
    evidence_change_count: int
    operation: str
    operation_fingerprint: str
    tokens_used: int | None
    elapsed_seconds: float


@dataclass(frozen=True)
class EfficiencyObservationContext:
    """Workflow-owned semantic fields for a host-measured observation."""

    dependency_count: int
    risk: str
    blocking_uncertainty: bool
    evidence_change_count: int
    operation: str
    operation_fingerprint: str


class EfficiencyObservationProvider(Protocol):
    """Host-neutral supplier of the latest measured efficiency observation.

    ``latest`` returns None while the host has no measured usage yet; a missing
    token count stays UNKNOWN and blocks budget admission.
    """

    def latest(self) -> EfficiencyObservation | None: ...


class ContextBoundEfficiencyObservationProvider(EfficiencyObservationProvider, Protocol):
    """Provider that binds exact workflow semantics before returning a measurement."""

    def bind_context(self, context: EfficiencyObservationContext) -> None: ...


class EfficiencyAdmissionLedger(Protocol):
    """Durable, cross-process admission record and cumulative budget state."""

    def admit(
        self,
        *,
        idempotency_key: str,
        scope_id: str,
        token_budget: int,
        time_budget_seconds: float,
        operation: str,
        operation_fingerprint: str,
        risk: str,
        complexity: ComplexityResult,
        progress_alert: bool | Callable[[], bool],
        tokens_used: int | None,
        elapsed_seconds: float,
    ) -> Any: ...


class EfficiencyAdmissionError(ContractValidationError):
    """A durable efficiency admission failure with a stable fail-closed code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


@dataclass
class EfficiencyRuntime:
    """Fail-closed runtime facade for measured efficiency governance.

    This class observes a workflow boundary without changing the underlying
    GoalAnchor decision. Missing model usage remains UNKNOWN and blocks budget
    admission until the caller supplies a measured token count.
    """

    budget: BudgetGovernor
    complexity_engine: TaskComplexityEngine = field(default_factory=TaskComplexityEngine)
    watchdog: ProgressWatchdog = field(default_factory=ProgressWatchdog)
    optimizer: PromptOptimizer = field(default_factory=PromptOptimizer)
    ledger: EfficiencyAdmissionLedger | None = None

    def admit(
        self,
        *,
        dependency_count: int,
        risk: str,
        blocking_uncertainty: bool,
        evidence_change_count: int,
        operation: str,
        operation_fingerprint: str,
        tokens_used: int | None,
        elapsed_seconds: float,
        admission_scope: str | None = None,
        idempotency_key: str | None = None,
    ) -> EfficiencyDecision:
        complexity = self.complexity_engine.assess(
            dependency_count=dependency_count,
            risk=risk,
            blocking_uncertainty=blocking_uncertainty,
            evidence_change_count=evidence_change_count,
        )
        if self.ledger is not None:
            if admission_scope is None or not isinstance(admission_scope, str) or not admission_scope.strip():
                raise EfficiencyAdmissionError("efficiency admission scope is required", "UNAVAILABLE")
            if idempotency_key is None or not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise EfficiencyAdmissionError(
                    "efficiency admission idempotency key is required", "UNAVAILABLE"
                )
            try:
                admission = self.ledger.admit(
                    idempotency_key=idempotency_key,
                    scope_id=admission_scope,
                    token_budget=self.budget.token_budget,
                    time_budget_seconds=self.budget.time_budget_seconds,
                    operation=operation,
                    operation_fingerprint=operation_fingerprint,
                    risk=risk,
                    complexity=complexity,
                    progress_alert=lambda: self.watchdog.observe(operation, operation_fingerprint),
                    tokens_used=tokens_used,
                    elapsed_seconds=elapsed_seconds,
                )
            except EfficiencyAdmissionError:
                raise
            except ContractValidationError as exc:
                raise EfficiencyAdmissionError(
                    "efficiency ledger storage is unavailable",
                    "UNAVAILABLE",
                ) from exc
            decision = getattr(admission, "decision", None)
            if not isinstance(decision, EfficiencyDecision):
                raise EfficiencyAdmissionError("efficiency ledger returned no decision", "UNAVAILABLE")
            return decision
        progress_alert = self.watchdog.observe(operation, operation_fingerprint)
        budget_allowed = self.budget.allow(tokens_used=tokens_used, elapsed_seconds=elapsed_seconds)
        token_status = "MEASURED" if tokens_used is not None else "UNKNOWN"
        if progress_alert:
            return EfficiencyDecision(
                False, "REPEATED_OPERATION", complexity, tokens_used, elapsed_seconds, True, token_status
            )
        if not budget_allowed:
            return EfficiencyDecision(
                False, self.budget.reason, complexity, tokens_used, elapsed_seconds, False, token_status
            )
        return EfficiencyDecision(
            True, "WITHIN_BUDGET", complexity, tokens_used, elapsed_seconds, False, token_status
        )

    def admit_observation(
        self,
        provider: EfficiencyObservationProvider,
        *,
        expected_operation: str | None = None,
        expected_operation_fingerprint: str | None = None,
        expected_risk: str | None = None,
        admission_scope: str | None = None,
        idempotency_key: str | None = None,
    ) -> EfficiencyDecision | None:
        observation = provider.latest()
        if observation is None:
            return None
        if not isinstance(observation, EfficiencyObservation):
            raise ValueError("observation must be an EfficiencyObservation")
        if expected_operation is not None and observation.operation != expected_operation:
            raise ValueError("observation operation does not match the workflow operation")
        if (
            expected_operation_fingerprint is not None
            and observation.operation_fingerprint != expected_operation_fingerprint
        ):
            raise ValueError("observation fingerprint is stale")
        if expected_risk is not None and observation.risk != expected_risk:
            raise ValueError("observation risk does not match the active review")
        return self.admit(
            dependency_count=observation.dependency_count,
            risk=observation.risk,
            blocking_uncertainty=observation.blocking_uncertainty,
            evidence_change_count=observation.evidence_change_count,
            operation=observation.operation,
            operation_fingerprint=observation.operation_fingerprint,
            tokens_used=observation.tokens_used,
            elapsed_seconds=observation.elapsed_seconds,
            admission_scope=admission_scope,
            idempotency_key=idempotency_key,
        )

    def reuse_prompt(self, context_fingerprint: str) -> str | None:
        return self.optimizer.reuse(context_fingerprint)


__all__ = [
    "BudgetGovernor",
    "ComplexityResult",
    "EfficiencyAdmissionError",
    "EfficiencyAdmissionLedger",
    "EfficiencyDecision",
    "EfficiencyObservation",
    "EfficiencyObservationContext",
    "EfficiencyObservationProvider",
    "EfficiencyRuntime",
    "ProgressWatchdog",
    "PromptOptimizer",
    "TaskComplexityEngine",
]
