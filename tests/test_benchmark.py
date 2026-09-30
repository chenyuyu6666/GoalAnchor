from goalanchor import run_phase6_benchmark


def test_phase6_benchmark_is_fully_aligned() -> None:
    result = run_phase6_benchmark()

    assert result["total"] == 5
    assert result["aligned_count"] == 5
    assert all(item["aligned"] for item in result["scenarios"])
