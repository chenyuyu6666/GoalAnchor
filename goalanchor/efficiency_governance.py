"""Small, fail-closed efficiency governance primitives."""

from __future__ import annotations

import math
from dataclasses import dataclass, field


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
    ) -> EfficiencyDecision:
        complexity = self.complexity_engine.assess(
            dependency_count=dependency_count,
            risk=risk,
            blocking_uncertainty=blocking_uncertainty,
            evidence_change_count=evidence_change_count,
        )
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

    def reuse_prompt(self, context_fingerprint: str) -> str | None:
        return self.optimizer.reuse(context_fingerprint)


__all__ = [
    "BudgetGovernor",
    "ComplexityResult",
    "EfficiencyDecision",
    "EfficiencyRuntime",
    "ProgressWatchdog",
    "PromptOptimizer",
    "TaskComplexityEngine",
]
