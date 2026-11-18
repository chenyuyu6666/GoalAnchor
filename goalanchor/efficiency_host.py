"""Host-measured efficiency observations for workflow admission."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any

from .efficiency_governance import EfficiencyObservation, EfficiencyObservationContext


@dataclass(frozen=True)
class MeasuredUsage:
    tokens_used: int | None
    elapsed_seconds: float
    source: str


class RecordedUsageEfficiencyObservationProvider:
    """Thread-safe bridge from measured host usage to workflow efficiency admission."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_age_seconds: float = 300,
    ) -> None:
        if (
            type(max_age_seconds) not in (int, float)
            or not math.isfinite(float(max_age_seconds))
            or max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be finite and positive")
        self._clock = clock
        self._max_age_seconds = float(max_age_seconds)
        self._lock = RLock()
        self._usage: MeasuredUsage | None = None
        self._measured_at: float | None = None
        self._context: EfficiencyObservationContext | None = None
        self._context_at_record: EfficiencyObservationContext | None = None

    def bind_context(self, context: EfficiencyObservationContext) -> None:
        if not isinstance(context, EfficiencyObservationContext):
            raise ValueError("context must be an EfficiencyObservationContext")
        with self._lock:
            self._context = context

    def record(
        self,
        *,
        tokens_used: int | None,
        elapsed_seconds: float,
        source: str = "host",
    ) -> None:
        if tokens_used is not None and (type(tokens_used) is not int or tokens_used < 0):
            raise ValueError("tokens_used must be a non-negative integer or None")
        if (
            type(elapsed_seconds) not in (int, float)
            or not math.isfinite(float(elapsed_seconds))
            or elapsed_seconds < 0
        ):
            raise ValueError("elapsed_seconds must be finite and non-negative")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be non-empty text")
        with self._lock:
            self._record_locked(
                tokens_used=tokens_used,
                elapsed_seconds=elapsed_seconds,
                source=source,
            )

    def _record_locked(
        self,
        *,
        tokens_used: int | None,
        elapsed_seconds: float,
        source: str,
    ) -> None:
        """Commit one measurement while the caller holds ``_lock``."""

        self._usage = MeasuredUsage(tokens_used, float(elapsed_seconds), source.strip())
        self._measured_at = self._clock()
        self._context_at_record = self._context

    def latest(self) -> EfficiencyObservation | None:
        with self._lock:
            return self._latest_locked(self._clock())

    def _latest_locked(
        self,
        now: float,
        *,
        elapsed_seconds: float | None = None,
    ) -> EfficiencyObservation | None:
        context = self._context
        usage = self._usage
        measured_at = self._measured_at
        context_at_record = self._context_at_record
        if context is None or usage is None or measured_at is None:
            return None
        if context != context_at_record:
            return None
        if now - measured_at > self._max_age_seconds:
            return None
        return EfficiencyObservation(
            dependency_count=context.dependency_count,
            risk=context.risk,
            blocking_uncertainty=context.blocking_uncertainty,
            evidence_change_count=context.evidence_change_count,
            operation=context.operation,
            operation_fingerprint=context.operation_fingerprint,
            tokens_used=usage.tokens_used,
            elapsed_seconds=(
                usage.elapsed_seconds
                if elapsed_seconds is None
                else max(0.0, elapsed_seconds)
            ),
        )


class CodexTurnUsageTracker(RecordedUsageEfficiencyObservationProvider):
    """Consume Codex App Server token-usage events without model self-reporting."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_age_seconds: float = 300,
    ) -> None:
        super().__init__(clock=clock, max_age_seconds=max_age_seconds)
        self._turn_id: str | None = None
        self._thread_id: str | None = None
        self._turn_started_at: float | None = None
        self._turn_baseline_tokens: int | None = None
        self._thread_totals: dict[str | None, int] = {}
        self._turn_last_total: int | None = None
        self._turn_thread_id: str | None = None
        self._seen_turns: set[tuple[str | None, str]] = set()
        self._awaiting_turn = False
        self._buffered_usage: Mapping[str, Any] | None = None
        self._buffered_candidates: dict[str, Mapping[str, Any]] = {}
        self._buffered_candidates_failed = False
        self._turn_measurement_blocked = False

    def prepare_turn(self, thread_id: str | None = None) -> None:
        """Arm the next turn before ``turn/start`` so adjacent usage is not lost."""

        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip()):
            raise ValueError("thread_id must be non-empty text")
        with self._lock:
            self._thread_id = thread_id.strip() if isinstance(thread_id, str) else None
            self._turn_id = None
            self._turn_started_at = self._clock()
            self._turn_baseline_tokens = self._thread_totals.get(self._thread_id)
            self._turn_last_total = None
            self._turn_thread_id = self._thread_id
            self._usage = None
            self._measured_at = None
            self._context = None
            self._context_at_record = None
            self._awaiting_turn = True
            self._buffered_usage = None
            self._buffered_candidates = {}
            self._buffered_candidates_failed = False
            self._turn_measurement_blocked = False

    def start_turn(self, turn_id: str, thread_id: str | None = None) -> None:
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("turn_id must be non-empty text")
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip()):
            raise ValueError("thread_id must be non-empty text")
        turn_id = turn_id.strip()
        normalized_thread = thread_id.strip() if isinstance(thread_id, str) else None
        with self._lock:
            effective_thread = (
                normalized_thread if normalized_thread is not None else self._thread_id
            )
            turn_key = (effective_thread, turn_id)
            if (
                not self._awaiting_turn
                and self._turn_id == turn_id
                and self._thread_id == effective_thread
            ):
                return
            if any(seen_turn == turn_id for _, seen_turn in self._seen_turns):
                raise ValueError("turn lifecycle cannot be reactivated")
            if (
                self._awaiting_turn
                and self._thread_id is not None
                and normalized_thread is not None
                and normalized_thread != self._thread_id
            ):
                raise ValueError("turn/start thread_id differs from prepared thread")
            self._turn_id = turn_id
            if normalized_thread is not None:
                if self._thread_id != normalized_thread:
                    self._thread_id = normalized_thread
                    self._turn_baseline_tokens = self._thread_totals.get(normalized_thread)
            if not self._awaiting_turn or self._turn_started_at is None:
                self._turn_started_at = self._clock()
                self._turn_baseline_tokens = self._thread_totals.get(self._thread_id)
                self._usage = None
                self._measured_at = None
                self._context = None
                self._context_at_record = None
                self._turn_last_total = None
                self._turn_thread_id = effective_thread
                self._buffered_usage = None
                self._buffered_candidates = {}
                self._buffered_candidates_failed = False
                self._turn_measurement_blocked = False
            else:
                self._turn_measurement_blocked = self._buffered_candidates_failed
                candidate = self._buffered_candidates.get(turn_id)
                self._buffered_candidates = {}
                if candidate is not None and (
                    self._thread_id is None
                    or candidate.get("threadId") == self._thread_id
                ):
                    self._buffered_usage = dict(candidate)
                self._buffered_candidates_failed = False
            self._awaiting_turn = False
            self._seen_turns.add(turn_key)
            self._maybe_commit_buffered_locked()

    def bind_context(self, context: EfficiencyObservationContext) -> None:
        if not isinstance(context, EfficiencyObservationContext):
            raise ValueError("context must be an EfficiencyObservationContext")
        with self._lock:
            self._context = context
            self._maybe_commit_buffered_locked()

    def observe(self, event: Mapping[str, Any]) -> None:
        if event.get("method") != "thread/tokenUsage/updated":
            return
        params = event.get("params")
        if not isinstance(params, Mapping):
            return
        self._observe_usage(params)

    def latest(self) -> EfficiencyObservation | None:
        with self._lock:
            now = self._clock()
            turn_started_at = self._turn_started_at
            if turn_started_at is None:
                return None
            return self._latest_locked(now, elapsed_seconds=now - turn_started_at)

    def _bind_observed_thread_locked(self, params: Mapping[str, Any]) -> bool:
        observed = params.get("threadId")
        if observed is not None and (
            not isinstance(observed, str) or not observed.strip()
        ):
            return False
        if observed is None:
            return self._turn_thread_id is None and self._thread_id is None
        observed = observed.strip()
        if self._thread_id is not None and observed != self._thread_id:
            return False
        if self._turn_thread_id is not None:
            return observed == self._turn_thread_id
        if (
            self._turn_last_total is not None
            or self._usage is not None
            or self._buffered_usage is not None
        ):
            return False
        self._turn_thread_id = observed
        self._turn_baseline_tokens = self._thread_totals.get(observed)
        if self._turn_id is not None:
            self._seen_turns.add((observed, self._turn_id))
        return True

    def _next_buffered_usage(
        self,
        existing: Mapping[str, Any] | None,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        token_usage = params.get("tokenUsage")
        measured_total = None
        if isinstance(token_usage, Mapping):
            measured_total = self._token_total(token_usage.get("total"))
        if measured_total is None:
            return existing
        if existing is not None:
            buffered_usage = existing.get("tokenUsage")
            buffered_total = (
                self._token_total(buffered_usage.get("total"))
                if isinstance(buffered_usage, Mapping)
                else None
            )
            if existing.get("threadId") != params.get("threadId"):
                return existing
            if buffered_total is not None and measured_total < buffered_total:
                return existing
        return dict(params)

    def _buffer_usage_locked(self, params: Mapping[str, Any]) -> None:
        self._buffered_usage = self._next_buffered_usage(
            self._buffered_usage,
            params,
        )

    def _buffer_candidate_locked(self, params: Mapping[str, Any]) -> None:
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str) or not turn_id.strip():
            return
        turn_id = turn_id.strip()
        if turn_id not in self._buffered_candidates and len(self._buffered_candidates) >= 8:
            self._buffered_candidates.clear()
            self._buffered_candidates_failed = True
            return
        candidate = self._next_buffered_usage(
            self._buffered_candidates.get(turn_id),
            params,
        )
        if candidate is not None:
            self._buffered_candidates[turn_id] = candidate

    def _observe_usage(self, params: Mapping[str, Any]) -> None:
        with self._lock:
            if self._awaiting_turn:
                observed = params.get("threadId")
                if observed is not None and (
                    not isinstance(observed, str) or not observed.strip()
                ):
                    return
                if self._thread_id is not None and observed != self._thread_id:
                    return
                if self._buffered_candidates_failed:
                    return
                self._buffer_candidate_locked(params)
                return
            if (
                self._turn_id is None
                or self._turn_started_at is None
                or params.get("turnId") != self._turn_id
            ):
                return
            if self._turn_measurement_blocked:
                return
            if not self._bind_observed_thread_locked(params):
                return
            if self._context is None:
                self._buffer_usage_locked(params)
                return
            self._commit_usage_locked(params)

    def _maybe_commit_buffered_locked(self) -> None:
        if (
            self._awaiting_turn
            or self._turn_id is None
            or self._turn_started_at is None
            or self._buffered_usage is None
        ):
            return
        buffered = self._buffered_usage
        self._buffered_usage = None
        if buffered.get("turnId") != self._turn_id:
            return
        if not self._bind_observed_thread_locked(buffered):
            return
        if self._context is None:
            self._buffered_usage = buffered
            return
        self._commit_usage_locked(buffered)

    def _commit_usage_locked(self, params: Mapping[str, Any]) -> None:
        token_usage = params.get("tokenUsage")
        measured_total = None
        if isinstance(token_usage, Mapping):
            measured_total = self._token_total(token_usage.get("total"))
        usage_thread = self._turn_thread_id
        previous_total = self._thread_totals.get(usage_thread)
        previous_turn_total = self._turn_last_total
        if measured_total is not None and measured_total == previous_turn_total:
            return
        if (
            measured_total is None
            or (self._turn_baseline_tokens is not None and measured_total < self._turn_baseline_tokens)
            or (
                previous_turn_total is not None
                and measured_total < previous_turn_total
            )
            or (
                previous_turn_total is None
                and previous_total is not None
                and measured_total < previous_total
            )
        ):
            self._usage = None
            self._measured_at = None
            self._context_at_record = None
            return
        baseline = self._turn_baseline_tokens
        tokens_used = measured_total if baseline is None else measured_total - baseline
        self._thread_totals[usage_thread] = measured_total
        self._turn_last_total = measured_total
        assert self._turn_started_at is not None
        self._record_locked(
            tokens_used=tokens_used,
            elapsed_seconds=max(0.0, self._clock() - self._turn_started_at),
            source="codex-app-server/tokenUsage.total",
        )

    @staticmethod
    def _token_total(value: Any) -> int | None:
        if not isinstance(value, Mapping):
            return None
        total = value.get("totalTokens")
        return total if type(total) is int and total >= 0 else None


__all__ = [
    "CodexTurnUsageTracker",
    "MeasuredUsage",
    "RecordedUsageEfficiencyObservationProvider",
]
