from __future__ import annotations

from goalanchor.efficiency import run_efficiency_baseline


def test_efficiency_baseline_reports_reproducible_observables() -> None:
    report = run_efficiency_baseline(repeat=2)

    assert report["schema"] == "goalanchor.efficiency-baseline/v1"
    assert report["repeat"] == 2
    assert report["environment"]["external_models"] is False
    assert len(report["samples"]) == 8
    for sample in report["samples"]:
        assert sample["duration_ms"] >= 0
        assert sample["stage_duration_ms"] >= 0
        assert sample["tokens_used"] is None
        assert sample["token_status"] == "UNKNOWN"
        assert sample["correct"] is True
        assert sample["repeat_operations"] == 0
        assert sample["rollback_count"] == 0

    assert report["aggregate"]["sample_count"] == 8
    assert report["aggregate"]["correct_rate"] == 1.0
    assert report["aggregate"]["token_status"] == "UNKNOWN"


def test_efficiency_baseline_rejects_invalid_repeat() -> None:
    try:
        run_efficiency_baseline(repeat=0)
    except ValueError as exc:
        assert "repeat" in str(exc)
    else:
        raise AssertionError("repeat=0 must be rejected")
