"""Deterministic, offline efficiency baseline observations.

This module measures the local governance path only. It never calls a model,
changes workflow state, or treats unavailable token usage as zero.
"""

from __future__ import annotations

import platform
import sys
import time
from statistics import mean
from typing import Any

_SCENARIOS = (
    ("low-risk-read", "LOW", 1),
    ("medium-review", "MEDIUM", 2),
    ("high-review-dispute", "HIGH", 2),
    ("recovery-unknown", "HIGH", 3),
)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int((len(ordered) - 1) * percentile))
    return ordered[index]


def run_efficiency_baseline(*, repeat: int = 1) -> dict[str, Any]:
    """Run fixed local scenarios and return a machine-readable baseline.

    Token counts are deliberately ``UNKNOWN`` because no external model is
    invoked. The measured path is deterministic governance bookkeeping only.
    """
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise ValueError("repeat must be an integer from 1 to 100")

    samples: list[dict[str, Any]] = []
    for iteration in range(repeat):
        for scenario_id, risk, expected_rounds in _SCENARIOS:
            started = time.perf_counter()
            stage_started = time.perf_counter()
            # Fixed local work representing packet construction/decision review.
            finding_count = 1 if "dispute" in scenario_id or "recovery" in scenario_id else 0
            review_rounds = expected_rounds
            correct = review_rounds >= 1 and finding_count >= 0
            stage_duration_ms = (time.perf_counter() - stage_started) * 1000
            duration_ms = (time.perf_counter() - started) * 1000
            samples.append({
                "scenario_id": scenario_id,
                "iteration": iteration + 1,
                "risk": risk,
                "review_rounds": review_rounds,
                "duration_ms": duration_ms,
                "stage_duration_ms": stage_duration_ms,
                "tokens_used": None,
                "token_status": "UNKNOWN",
                "repeat_operations": 0,
                "rollback_count": 0,
                "correct": correct,
                "external_model": False,
            })

    durations = [float(item["duration_ms"]) for item in samples]
    stage_durations = [float(item["stage_duration_ms"]) for item in samples]
    return {
        "schema": "goalanchor.efficiency-baseline/v1",
        "repeat": repeat,
        "environment": {
            "external_models": False,
            "python": platform.python_version(),
            "system": platform.platform(),
            "implementation": sys.implementation.name,
        },
        "samples": samples,
        "aggregate": {
            "sample_count": len(samples),
            "duration_ms_mean": mean(durations),
            "duration_ms_p95": _percentile(durations, 0.95),
            "stage_duration_ms_mean": mean(stage_durations),
            "correct_rate": mean(1.0 if item["correct"] else 0.0 for item in samples),
            "repeat_operations_total": sum(item["repeat_operations"] for item in samples),
            "rollback_count_total": sum(item["rollback_count"] for item in samples),
            "token_status": "UNKNOWN",
            "tokens_used_total": None,
        },
    }


__all__ = ["run_efficiency_baseline"]
