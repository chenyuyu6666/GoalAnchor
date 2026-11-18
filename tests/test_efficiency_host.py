from __future__ import annotations

import threading

import pytest

from goalanchor.efficiency_governance import (
    BudgetGovernor,
    EfficiencyObservationContext,
    EfficiencyRuntime,
)
from goalanchor.efficiency_host import (
    CodexTurnUsageTracker,
    RecordedUsageEfficiencyObservationProvider,
)


def _context(**overrides):
    values = {
        "dependency_count": 2,
        "risk": "MEDIUM",
        "blocking_uncertainty": False,
        "evidence_change_count": 1,
        "operation": "write_text",
        "operation_fingerprint": "a" * 64,
    }
    values.update(overrides)
    return EfficiencyObservationContext(**values)


def test_recorded_usage_provider_binds_exact_workflow_semantics() -> None:
    now = [10.0]
    provider = RecordedUsageEfficiencyObservationProvider(clock=lambda: now[0])
    provider.bind_context(_context())
    provider.record(tokens_used=42, elapsed_seconds=1.5, source="host-test")
    observation = provider.latest()
    assert observation is not None
    assert observation.tokens_used == 42
    assert observation.elapsed_seconds == 1.5
    assert observation.operation == "write_text"
    assert observation.operation_fingerprint == "a" * 64
    assert observation.risk == "MEDIUM"


def test_recorded_usage_provider_expires_measurements() -> None:
    now = [10.0]
    provider = RecordedUsageEfficiencyObservationProvider(
        clock=lambda: now[0], max_age_seconds=5
    )
    provider.bind_context(_context())
    provider.record(tokens_used=42, elapsed_seconds=1.5)
    now[0] = 15.1
    assert provider.latest() is None


def test_recorded_usage_cannot_be_relabelled_to_another_operation() -> None:
    provider = RecordedUsageEfficiencyObservationProvider(clock=lambda: 10.0)
    provider.bind_context(_context())
    provider.record(tokens_used=42, elapsed_seconds=1.5)
    provider.bind_context(_context(operation="delete_file", operation_fingerprint="b" * 64))
    assert provider.latest() is None


def test_recorded_usage_provider_requires_context_and_rejects_invalid_measurements() -> None:
    provider = RecordedUsageEfficiencyObservationProvider(clock=lambda: 10.0)
    provider.record(tokens_used=42, elapsed_seconds=1.5)
    assert provider.latest() is None
    provider.bind_context(_context())
    with pytest.raises(ValueError, match="tokens_used"):
        provider.record(tokens_used=-1, elapsed_seconds=1)
    with pytest.raises(ValueError, match="elapsed_seconds"):
        provider.record(tokens_used=1, elapsed_seconds=float("nan"))


def test_codex_turn_usage_tracker_uses_host_reported_total_tokens_and_elapsed() -> None:
    now = [100.0]
    tracker = CodexTurnUsageTracker(clock=lambda: now[0])
    tracker.start_turn("turn-1")
    tracker.bind_context(_context())
    now[0] = 102.25
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-1",
                "tokenUsage": {
                    "last": {"totalTokens": 36040, "inputTokens": 36013, "outputTokens": 27},
                    "total": {"totalTokens": 99999},
                },
            },
        }
    )
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 99999
    assert observation.elapsed_seconds == 2.25


def test_codex_turn_usage_tracker_ignores_other_turn_and_malformed_usage() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-1")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {"turnId": "turn-2", "tokenUsage": {"last": {"totalTokens": 1}}},
        }
    )
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {"turnId": "turn-1", "tokenUsage": {"last": {"totalTokens": True}}},
        }
    )
    assert tracker.latest() is None


def test_codex_turn_usage_tracker_buffers_usage_that_arrives_before_start_binding() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn("thread-1")
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 7}},
            },
        }
    )
    tracker.start_turn("turn-1", "thread-1")
    assert tracker.latest() is None
    tracker.bind_context(_context())
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 7


def test_buffered_usage_cannot_override_a_newer_cumulative_report() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn("thread-1")
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 80}},
            },
        }
    )
    tracker.start_turn("turn-1", "thread-1")
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 110}},
            },
        }
    )
    tracker.bind_context(_context())
    observation = tracker.latest()
    assert observation is not None and observation.tokens_used == 110
    decision = EfficiencyRuntime(
        BudgetGovernor(token_budget=100, time_budget_seconds=10)
    ).admit_observation(
        tracker,
        expected_operation="write_text",
        expected_operation_fingerprint="a" * 64,
        expected_risk="MEDIUM",
    )
    assert decision is not None and decision.allowed is False


def test_buffered_usage_for_an_old_turn_cannot_bind_to_the_new_turn() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn("thread-1")
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-old",
                "tokenUsage": {"total": {"totalTokens": 7}},
            },
        }
    )
    tracker.start_turn("turn-new", "thread-1")
    tracker.bind_context(_context(operation="delete_file", operation_fingerprint="b" * 64))
    assert tracker.latest() is None


def test_codex_turn_usage_tracker_rejects_other_thread_and_invalidates_bad_current_report() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-1", "thread-1")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-2",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 7}},
            },
        }
    )
    assert tracker.latest() is None
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 7}},
            },
        }
    )
    assert tracker.latest() is not None
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"last": {"totalTokens": True}},
            },
        }
    )
    assert tracker.latest() is None


def test_codex_turn_usage_uses_cross_turn_baseline() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-1")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 100}},
            },
        }
    )
    tracker.start_turn("turn-2")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-2",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 50


def test_codex_turn_usage_cannot_fall_back_or_move_cumulative_total_backwards() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.bind_context(_context())
    tracker.start_turn("turn-1", "thread-1")
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 120}},
            },
        }
    )
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"last": {"totalTokens": 7}},
            },
        }
    )
    assert tracker.latest() is None
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 60}},
            },
        }
    )
    assert tracker.latest() is None


def test_codex_turn_usage_keeps_cumulative_baseline_per_thread() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.bind_context(_context())
    tracker.start_turn("turn-a", "thread-a")
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-a",
                "turnId": "turn-a",
                "tokenUsage": {"total": {"totalTokens": 100}},
            },
        }
    )
    tracker.start_turn("turn-b", "thread-b")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 150
    decision = EfficiencyRuntime(
        BudgetGovernor(token_budget=100, time_budget_seconds=10)
    ).admit_observation(
        tracker,
        expected_operation="write_text",
        expected_operation_fingerprint="a" * 64,
        expected_risk="MEDIUM",
    )
    assert decision is not None and decision.allowed is False


def test_codex_usage_elapsed_advances_to_the_admission_time() -> None:
    now = [0.0]
    tracker = CodexTurnUsageTracker(clock=lambda: now[0], max_age_seconds=300)
    tracker.start_turn("turn-1", "thread-1")
    tracker.bind_context(_context())
    now[0] = 1.0
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 10}},
            },
        }
    )
    now[0] = 100.0
    observation = tracker.latest()
    assert observation is not None
    assert observation.elapsed_seconds == 100.0
    decision = EfficiencyRuntime(
        BudgetGovernor(token_budget=100, time_budget_seconds=10)
    ).admit_observation(
        tracker,
        expected_operation="write_text",
        expected_operation_fingerprint="a" * 64,
        expected_risk="MEDIUM",
    )
    assert decision is not None and decision.allowed is False
    assert decision.reason == "TIME_BUDGET_EXCEEDED"


def test_unbound_threads_are_inferred_and_never_share_a_cumulative_baseline() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-a")
    tracker.bind_context(_context(operation="operation-a", operation_fingerprint="a" * 64))
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-a",
                "turnId": "turn-a",
                "tokenUsage": {"total": {"totalTokens": 120}},
            },
        }
    )
    tracker.start_turn("turn-b")
    tracker.bind_context(_context(operation="operation-b", operation_fingerprint="b" * 64))
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    observation = tracker.latest()
    assert observation is not None
    assert observation.operation == "operation-b"
    assert observation.tokens_used == 150
    decision = EfficiencyRuntime(
        BudgetGovernor(token_budget=100, time_budget_seconds=10)
    ).admit_observation(
        tracker,
        expected_operation="operation-b",
        expected_operation_fingerprint="b" * 64,
        expected_risk="MEDIUM",
    )
    assert decision is not None and decision.allowed is False
    assert decision.reason == "TOKEN_BUDGET_EXCEEDED"


def test_unbound_measurement_cannot_switch_thread_after_usage_was_recorded() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-1")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 120}},
            },
        }
    )
    assert tracker.latest() is not None
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-b",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 120


def test_old_turn_usage_cannot_claim_a_thread_before_turn_validation() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("current-turn")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "foreign-thread",
                "turnId": "old-turn",
                "tokenUsage": {"total": {"totalTokens": 120}},
            },
        }
    )
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "current-thread",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 150


def test_old_buffered_turn_usage_cannot_claim_the_next_turn_thread() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn()
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "foreign-thread",
                "turnId": "old-turn",
                "tokenUsage": {"total": {"totalTokens": 120}},
            },
        }
    )
    tracker.start_turn("current-turn")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "current-thread",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 150


def test_stale_buffer_is_cleared_before_current_usage_and_context_arrive() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn()
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "foreign-thread",
                "turnId": "old-turn",
                "tokenUsage": {"total": {"totalTokens": 120}},
            },
        }
    )
    tracker.start_turn("current-turn")
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "current-thread",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    tracker.bind_context(_context())
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 150


def test_buffered_usage_cannot_regress_and_lower_the_budget_measurement() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn()
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "current-thread",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    tracker.start_turn("current-turn")
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "current-thread",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 80}},
            },
        }
    )
    tracker.bind_context(_context())
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 150
    decision = EfficiencyRuntime(
        BudgetGovernor(token_budget=100, time_budget_seconds=10)
    ).admit_observation(
        tracker,
        expected_operation="write_text",
        expected_operation_fingerprint="a" * 64,
        expected_risk="MEDIUM",
    )
    assert decision is not None and decision.allowed is False
    assert decision.reason == "TOKEN_BUDGET_EXCEEDED"


def test_buffered_high_water_is_scoped_to_the_same_turn_and_thread() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn()
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "old-thread",
                "turnId": "old-turn",
                "tokenUsage": {"total": {"totalTokens": 999}},
            },
        }
    )
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "current-thread",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    tracker.start_turn("current-turn")
    tracker.bind_context(_context())
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 150


def test_buffered_usage_cannot_drop_its_thread_identity() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn()
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-a",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 80}},
            },
        }
    )
    tracker.start_turn("current-turn")
    tracker.bind_context(_context())
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 150


def test_buffered_usage_for_one_turn_cannot_switch_threads() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn()
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-a",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-b",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 170}},
            },
        }
    )
    tracker.start_turn("current-turn")
    tracker.bind_context(_context())
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 150


def test_delayed_old_turn_usage_cannot_displace_the_next_turn_buffer() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn()
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-a",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-a",
                "turnId": "old-turn",
                "tokenUsage": {"total": {"totalTokens": 999}},
            },
        }
    )
    tracker.start_turn("current-turn")
    tracker.bind_context(_context())
    observation = tracker.latest()
    assert observation is not None
    assert observation.tokens_used == 150


def test_inferred_thread_cannot_be_used_to_reactivate_the_same_turn() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("current-turn")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-a",
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    tracker.start_turn("other-turn")
    with pytest.raises(ValueError, match="reactivated"):
        tracker.start_turn("current-turn", "thread-a")


def test_threadless_turn_cannot_be_reactivated_under_a_later_thread() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("current-turn")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "current-turn",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    tracker.start_turn("other-turn")
    with pytest.raises(ValueError, match="reactivated"):
        tracker.start_turn("current-turn", "thread-a")


def test_candidate_overflow_fails_closed_for_the_prepared_turn() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn()

    def report(turn_id: str, tokens: int) -> None:
        tracker.observe(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "shared-thread",
                    "turnId": turn_id,
                    "tokenUsage": {"total": {"totalTokens": tokens}},
                },
            }
        )

    report("current-turn", 150)
    for index in range(8):
        report(f"other-turn-{index}", index + 1)
    report("current-turn", 80)
    tracker.start_turn("current-turn")
    tracker.bind_context(_context())
    report("current-turn", 80)
    assert tracker.latest() is None


def test_candidate_overflow_blocks_a_target_first_seen_after_the_limit() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn("thread-a")

    def report(turn_id: str, tokens: int) -> None:
        tracker.observe(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-a",
                    "turnId": turn_id,
                    "tokenUsage": {"total": {"totalTokens": tokens}},
                },
            }
        )

    for index in range(9):
        report(f"other-turn-{index}", index + 1)
    report("target-turn", 150)
    tracker.start_turn("target-turn", "thread-a")
    tracker.bind_context(_context())
    report("target-turn", 80)
    assert tracker.latest() is None


def test_prepared_thread_must_match_start_thread() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.prepare_turn("thread-a")
    with pytest.raises(ValueError, match="differs"):
        tracker.start_turn("turn-b", "thread-b")


def test_turn_start_is_idempotent_and_old_turn_cannot_reactivate() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-1", "thread-1")
    tracker.bind_context(_context())
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 120}},
            },
        }
    )
    tracker.start_turn("turn-1", "thread-1")
    observation = tracker.latest()
    assert observation is not None and observation.tokens_used == 120
    tracker.prepare_turn("thread-1")
    with pytest.raises(ValueError, match="reactivated"):
        tracker.start_turn("turn-1", "thread-1")


def test_duplicate_usage_does_not_refresh_measurement_ttl() -> None:
    now = [0.0]
    tracker = CodexTurnUsageTracker(clock=lambda: now[0], max_age_seconds=1)
    tracker.start_turn("turn-1")
    tracker.bind_context(_context())
    event = {
        "method": "thread/tokenUsage/updated",
        "params": {
            "turnId": "turn-1",
            "tokenUsage": {"total": {"totalTokens": 10}},
        },
    }
    tracker.observe(event)
    now[0] = 2.0
    assert tracker.latest() is None
    tracker.observe(event)
    assert tracker.latest() is None


def test_new_turn_drops_previous_context_and_commits_after_rebinding() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-1")
    tracker.bind_context(_context(operation="operation-a", operation_fingerprint="a" * 64))
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 120}},
            },
        }
    )
    tracker.prepare_turn()
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-2",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    tracker.start_turn("turn-2")
    assert tracker.latest() is None
    tracker.bind_context(_context(operation="operation-b", operation_fingerprint="b" * 64))
    observation = tracker.latest()
    assert observation is not None
    assert observation.operation == "operation-b"
    assert observation.tokens_used == 30


def test_direct_start_of_a_new_turn_also_waits_for_new_context() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-1", "thread-1")
    tracker.bind_context(_context(operation="operation-a", operation_fingerprint="a" * 64))
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"total": {"totalTokens": 120}},
            },
        }
    )
    tracker.start_turn("turn-2", "thread-1")
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-2",
                "tokenUsage": {"total": {"totalTokens": 150}},
            },
        }
    )
    assert tracker.latest() is None
    tracker.bind_context(_context(operation="operation-b", operation_fingerprint="b" * 64))
    observation = tracker.latest()
    assert observation is not None
    assert observation.operation == "operation-b"
    assert observation.tokens_used == 30


def test_codex_turn_usage_uses_cumulative_delta_and_cannot_undercount_budget() -> None:
    tracker = CodexTurnUsageTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-1")
    tracker.bind_context(_context())
    first = {
        "method": "thread/tokenUsage/updated",
        "params": {
            "turnId": "turn-1",
            "tokenUsage": {
                "last": {"totalTokens": 60},
                "total": {"totalTokens": 60},
            },
        },
    }
    tracker.observe(first)
    tracker.observe(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-1",
                "tokenUsage": {
                    "last": {"totalTokens": 60},
                    "total": {"totalTokens": 120},
                },
            },
        }
    )
    observation = tracker.latest()
    assert observation is not None and observation.tokens_used == 120
    decision = EfficiencyRuntime(
        BudgetGovernor(token_budget=100, time_budget_seconds=10)
    ).admit_observation(
        tracker,
        expected_operation="write_text",
        expected_operation_fingerprint="a" * 64,
        expected_risk="MEDIUM",
    )
    assert decision is not None
    assert decision.allowed is False
    assert decision.reason == "TOKEN_BUDGET_EXCEEDED"


def test_codex_turn_boundary_is_atomic_with_measurement_commit() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingTracker(CodexTurnUsageTracker):
        def _record_locked(self, *, tokens_used, elapsed_seconds, source):
            entered.set()
            assert release.wait(2)
            super()._record_locked(
                tokens_used=tokens_used,
                elapsed_seconds=elapsed_seconds,
                source=source,
            )

    tracker = BlockingTracker(clock=lambda: 10.0)
    tracker.start_turn("turn-old")
    tracker.bind_context(_context())
    observer = threading.Thread(
        target=tracker.observe,
        args=(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "turnId": "turn-old",
                    "tokenUsage": {"total": {"totalTokens": 7}},
                },
            },
        ),
    )
    observer.start()
    assert entered.wait(2)
    switched = threading.Event()

    def switch_turn():
        tracker.start_turn("turn-new")
        tracker.bind_context(_context(operation="delete_file", operation_fingerprint="b" * 64))
        switched.set()

    switcher = threading.Thread(target=switch_turn)
    switcher.start()
    assert switched.wait(0.05) is False
    release.set()
    observer.join(2)
    switcher.join(2)
    assert switched.is_set()
    assert tracker.latest() is None
