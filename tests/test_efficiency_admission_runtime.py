from __future__ import annotations

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.efficiency_admission import EfficiencyAdmissionStore
from goalanchor.efficiency_governance import (
    BudgetGovernor,
    EfficiencyAdmissionError,
    EfficiencyRuntime,
    ProgressWatchdog,
)


def _admit(runtime, *, key: str, tokens: int, elapsed: float = 1.0, admission_scope: str | None = "scope-1"):
    return runtime.admit(
        dependency_count=0,
        risk="LOW",
        blocking_uncertainty=False,
        evidence_change_count=0,
        operation="write_text",
        operation_fingerprint="a" * 64,
        tokens_used=tokens,
        elapsed_seconds=elapsed,
        admission_scope=admission_scope,
        idempotency_key=key,
    )


def test_runtime_uses_durable_ledger_for_admissions(tmp_path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: "2026-11-18T00:00:00+00:00")
    runtime = EfficiencyRuntime(budget=BudgetGovernor(token_budget=100, time_budget_seconds=10), ledger=ledger)
    first = _admit(runtime, key="a", tokens=60)
    assert first.allowed is True
    assert first.reason == "WITHIN_BUDGET"

    replayed = _admit(runtime, key="a", tokens=60)
    assert first == replayed
    assert ledger.budget_state("scope-1").tokens_consumed == 60

    denied = _admit(runtime, key="b", tokens=50)
    assert denied.allowed is False
    assert denied.reason == "TOKEN_BUDGET_EXCEEDED"
    assert ledger.budget_state("scope-1").tokens_consumed == 60


def test_runtime_conflicting_admission_maps_to_conflict(tmp_path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: "2026-11-18T00:00:00+00:00")
    runtime = EfficiencyRuntime(budget=BudgetGovernor(token_budget=100, time_budget_seconds=10), ledger=ledger)
    _admit(runtime, key="a", tokens=60)
    with pytest.raises(EfficiencyAdmissionError) as caught:
        _admit(runtime, key="a", tokens=70)
    assert caught.value.code == "CONFLICT"
    assert isinstance(caught.value, ContractValidationError)


def test_runtime_ledger_requires_admission_binding(tmp_path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: "2026-11-18T00:00:00+00:00")
    runtime = EfficiencyRuntime(budget=BudgetGovernor(token_budget=100, time_budget_seconds=10), ledger=ledger)
    with pytest.raises(EfficiencyAdmissionError) as caught:
        _admit(runtime, key="a", tokens=60, admission_scope=None)
    assert caught.value.code == "UNAVAILABLE"


def test_repeated_admission_does_not_advance_watchdog(tmp_path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: "2026-11-18T00:00:00+00:00")
    runtime = EfficiencyRuntime(budget=BudgetGovernor(token_budget=100, time_budget_seconds=10), ledger=ledger)
    first = _admit(runtime, key="a", tokens=60)
    for _ in range(3):
        assert _admit(runtime, key="a", tokens=60) == first
    assert _admit(runtime, key="b", tokens=1).allowed is True
    assert ledger.budget_state("scope-1").tokens_consumed == 61


def test_progress_denial_is_persisted_without_advancing_the_budget_head(tmp_path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: "2026-11-18T00:00:00+00:00")
    runtime = EfficiencyRuntime(
        budget=BudgetGovernor(token_budget=100, time_budget_seconds=10),
        watchdog=ProgressWatchdog(repeat_threshold=1),
        ledger=ledger,
    )

    assert _admit(runtime, key="first", tokens=10).allowed is True
    denied = _admit(runtime, key="second", tokens=10)
    assert denied.allowed is False
    assert denied.reason == "REPEATED_OPERATION"
    assert denied.progress_alert is True
    history = ledger.history("scope-1")
    assert [item.reason for item in history] == ["WITHIN_BUDGET", "REPEATED_OPERATION"]
    state = ledger.budget_state("scope-1")
    assert state.generation == 1
    assert state.tokens_consumed == 10
