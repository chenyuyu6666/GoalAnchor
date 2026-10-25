from __future__ import annotations

import math

import pytest

from goalanchor.efficiency_governance import (
    BudgetGovernor,
    EfficiencyRuntime,
    ProgressWatchdog,
    PromptOptimizer,
    TaskComplexityEngine,
)


def test_complexity_engine_is_explainable_and_bounded() -> None:
    result = TaskComplexityEngine().assess(
        dependency_count=3, risk="HIGH", blocking_uncertainty=True, evidence_change_count=2
    )
    assert result.score == 8
    assert result.level == "HIGH"
    assert result.signals == {
        "dependencies": 3,
        "risk": 3,
        "blocking_uncertainty": 1,
        "evidence_changes": 1,
    }


@pytest.mark.parametrize("value", [0, 1, "false", None, []])
def test_complexity_engine_rejects_non_boolean_uncertainty(value: object) -> None:
    with pytest.raises(ValueError, match="blocking_uncertainty"):
        TaskComplexityEngine().assess(
            dependency_count=0, risk="LOW", blocking_uncertainty=value, evidence_change_count=0
        )


def test_budget_governor_unknown_usage_fails_closed() -> None:
    governor = BudgetGovernor(token_budget=100, time_budget_seconds=5)
    assert governor.allow(tokens_used=None, elapsed_seconds=1) is False
    assert governor.reason == "UNKNOWN_USAGE"
    assert governor.allow(tokens_used=50, elapsed_seconds=1) is True
    assert governor.allow(tokens_used=101, elapsed_seconds=1) is False
    assert governor.reason == "TOKEN_BUDGET_EXCEEDED"


@pytest.mark.parametrize(
    ("tokens_used", "elapsed_seconds"),
    [(-1, 1), (True, 1), (0, -1), (0, "1"), (0, math.nan)],
)
def test_budget_governor_rejects_invalid_usage(tokens_used: object, elapsed_seconds: object) -> None:
    governor = BudgetGovernor(token_budget=100, time_budget_seconds=5)
    assert governor.allow(tokens_used=tokens_used, elapsed_seconds=elapsed_seconds) is False  # type: ignore[arg-type]
    assert governor.reason == "INVALID_USAGE"


def test_progress_watchdog_detects_repeated_operations() -> None:
    watchdog = ProgressWatchdog(repeat_threshold=2)
    assert watchdog.observe("read", "a") is False
    assert watchdog.observe("read", "a") is False
    assert watchdog.observe("read", "a") is True
    assert watchdog.reason == "REPEATED_OPERATION"


@pytest.mark.parametrize("operation,fingerprint", [("", "a"), (" ", "a"), ("read", ""), ("read", " "), ([], {})])
def test_progress_watchdog_rejects_invalid_observations(operation: object, fingerprint: object) -> None:
    with pytest.raises(ValueError):
        ProgressWatchdog().observe(operation, fingerprint)  # type: ignore[arg-type]


def test_prompt_optimizer_reuses_only_matching_context() -> None:
    optimizer = PromptOptimizer()
    optimizer.remember("ctx-1", "answer-1", token_saving=10)
    assert optimizer.reuse("ctx-1") == "answer-1"
    assert optimizer.reuse("ctx-2") is None
    with pytest.raises(ValueError):
        optimizer.remember("ctx-1", "answer-2", token_saving=-1)
    with pytest.raises(ValueError):
        optimizer.remember("ctx-1", "answer-2", token_saving=0)


def test_efficiency_runtime_fails_closed_on_unknown_usage_and_repeats() -> None:
    runtime = EfficiencyRuntime(BudgetGovernor(token_budget=100, time_budget_seconds=5))
    unknown = runtime.admit(
        dependency_count=1,
        risk="MEDIUM",
        blocking_uncertainty=False,
        evidence_change_count=0,
        operation="review",
        operation_fingerprint="ctx-1",
        tokens_used=None,
        elapsed_seconds=1,
    )
    assert unknown.allowed is False
    assert unknown.reason == "UNKNOWN_USAGE"
    measured = runtime.admit(
        dependency_count=1,
        risk="MEDIUM",
        blocking_uncertainty=False,
        evidence_change_count=0,
        operation="review",
        operation_fingerprint="ctx-2",
        tokens_used=10,
        elapsed_seconds=1,
    )
    assert measured.allowed is True and measured.token_status == "MEASURED"
    runtime.watchdog.repeat_threshold = 1
    runtime.admit(
        dependency_count=0,
        risk="LOW",
        blocking_uncertainty=False,
        evidence_change_count=0,
        operation="review",
        operation_fingerprint="same",
        tokens_used=1,
        elapsed_seconds=0,
    )
    repeated = runtime.admit(
        dependency_count=0,
        risk="LOW",
        blocking_uncertainty=False,
        evidence_change_count=0,
        operation="review",
        operation_fingerprint="same",
        tokens_used=1,
        elapsed_seconds=0,
    )
    assert repeated.allowed is False and repeated.reason == "REPEATED_OPERATION"
