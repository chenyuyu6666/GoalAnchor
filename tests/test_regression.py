from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from goalanchor import RegressionOutcome, VersionRun, compare_version_runs
from goalanchor.core import ContractValidationError

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "regression"
EVALUATOR = ROOT / "scripts" / "evaluate_fixtures.py"


def outcome(
    fixture_id: str,
    expected_passed: bool,
    actual_passed: bool,
    finding_codes=(),
    expected_finding_codes=(),
) -> RegressionOutcome:
    return RegressionOutcome(
        fixture_id=fixture_id,
        failure_class="test",
        expected_passed=expected_passed,
        actual_passed=actual_passed,
        finding_codes=finding_codes,
        expected_finding_codes=expected_finding_codes,
    )


def run(revision: str, outcomes) -> VersionRun:
    return VersionRun(
        revision=revision,
        fixtures_sha256="fixtures",
        acceptance_sha256="acceptance",
        outcomes=tuple(outcomes),
    )


def test_outcome_alignment_includes_expected_failure_codes() -> None:
    assert outcome("F1", False, False, ("EXPECTED",), ("EXPECTED",)).aligned
    assert not outcome("F1", False, False, ("OTHER",), ("EXPECTED",)).aligned
    assert not outcome("F1", True, False).aligned


def test_version_comparison_reports_deltas_improvements_and_regressions() -> None:
    before = run(
        "before",
        (
            outcome("F1", False, False),
            outcome("F2", False, True),
            outcome("F3", True, True),
        ),
    )
    after = run(
        "after",
        (
            outcome("F1", False, False),
            outcome("F2", False, False),
            outcome("F3", True, False),
        ),
    )
    comparison = compare_version_runs(before, after)
    assert comparison.comparable
    assert comparison.metric_delta["aligned_count"] == 0
    assert comparison.regressions == ("F3",)
    assert comparison.improvements == ("F2",)


def test_version_comparison_refuses_different_inputs() -> None:
    before = run("before", (outcome("F1", False, False),))
    changed_fixtures = VersionRun(
        revision="after",
        fixtures_sha256="different",
        acceptance_sha256="acceptance",
        outcomes=(outcome("F1", False, False),),
    )
    result = compare_version_runs(before, changed_fixtures)
    assert not result.comparable
    assert result.not_comparable_reason == "fixture inputs differ"
    assert result.metric_delta == {}

    changed_acceptance = VersionRun(
        revision="after",
        fixtures_sha256="fixtures",
        acceptance_sha256="different",
        outcomes=(outcome("F1", False, False),),
    )
    result = compare_version_runs(before, changed_acceptance)
    assert not result.comparable
    assert result.not_comparable_reason == "acceptance criteria differ"


def test_version_run_rejects_duplicate_fixture_ids() -> None:
    with pytest.raises(ContractValidationError, match="unique"):
        run("bad", (outcome("F1", False, False), outcome("F1", False, False)))


def test_fixture_runner_is_deterministic_and_all_cases_align() -> None:
    command = [
        sys.executable,
        str(EVALUATOR),
        "--fixtures",
        str(FIXTURE_DIR),
        "--revision",
        "test",
    ]
    first = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    second = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    assert first.returncode == 0
    assert second.returncode == 0
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["aligned_count"] == first_payload["total"] == 12
    assert first_payload["fixtures_sha256"] == second_payload["fixtures_sha256"]
    assert first_payload["acceptance_sha256"] == second_payload["acceptance_sha256"]
    assert first_payload["outcomes"] == second_payload["outcomes"]
