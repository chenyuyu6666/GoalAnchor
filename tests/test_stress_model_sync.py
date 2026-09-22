"""Fast CI smoke for the model-sync stress scenarios.

The heavy multi-round runs live in ``scripts/stress_model_sync.py`` and can
be executed manually with custom intensity::

    python scripts/stress_model_sync.py --cycles 200

This module imports the same scenario functions and runs a small, fast
subset so CI catches regressions in the model-consistency guarantee.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
STRESS_PATH = ROOT / "scripts" / "stress_model_sync.py"


def _load_stress():
    spec = importlib.util.spec_from_file_location("stress_model_sync", STRESS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_dispute_cycles_hold_model_across_rounds() -> None:
    stress = _load_stress()
    result = stress.scenario_dispute_cycles(cycles=3)
    assert result["failures"] == [], result


def test_model_switch_has_no_cross_run_bleed() -> None:
    stress = _load_stress()
    result = stress.scenario_model_switch(runs=3)
    assert result["failures"] == [], result


def test_concurrent_dispatch_keeps_model_injection_correct() -> None:
    stress = _load_stress()
    result = stress.scenario_concurrency(workers=8, rounds=2)
    assert result["failures"] == [], result


def test_retry_attempts_all_carry_model() -> None:
    stress = _load_stress()
    result = stress.scenario_retry(failed_attempts=2, retries=3, iterations=3)
    assert result["failures"] == [], result


def test_divergence_fuzz_rejects_without_launch() -> None:
    stress = _load_stress()
    result = stress.scenario_divergence_fuzz(cases=12)
    assert result["failures"] == [], result
