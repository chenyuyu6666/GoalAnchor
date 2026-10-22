from __future__ import annotations

import pytest

from goalanchor.efficiency_governance import (
    BudgetGovernor,
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


def test_budget_governor_unknown_usage_fails_closed() -> None:
    governor = BudgetGovernor(token_budget=100, time_budget_seconds=5)
    assert governor.allow(tokens_used=None, elapsed_seconds=1) is False
    assert governor.reason == "UNKNOWN_USAGE"
    assert governor.allow(tokens_used=50, elapsed_seconds=1) is True
    assert governor.allow(tokens_used=101, elapsed_seconds=1) is False
    assert governor.reason == "TOKEN_BUDGET_EXCEEDED"


def test_progress_watchdog_detects_repeated_operations() -> None:
    watchdog = ProgressWatchdog(repeat_threshold=2)
    assert watchdog.observe("read", "a") is False
    assert watchdog.observe("read", "a") is False
    assert watchdog.observe("read", "a") is True
    assert watchdog.reason == "REPEATED_OPERATION"


def test_prompt_optimizer_reuses_only_matching_context() -> None:
    optimizer = PromptOptimizer()
    optimizer.remember("ctx-1", "answer-1", token_saving=10)
    assert optimizer.reuse("ctx-1") == "answer-1"
    assert optimizer.reuse("ctx-2") is None
    with pytest.raises(ValueError):
        optimizer.remember("ctx-1", "answer-2", token_saving=-1)
