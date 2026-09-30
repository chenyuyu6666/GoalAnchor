"""Compare fixed fixture outcomes from two GoalAnchor package revisions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_ROOT))

from goalanchor import RegressionOutcome, VersionRun, compare_version_runs


def _evaluate(
    evaluator: Path, package_root: Path, fixtures: Path, revision: str
) -> VersionRun:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [
            sys.executable,
            str(evaluator),
            "--package-root",
            str(package_root),
            "--fixtures",
            str(fixtures),
            "--revision",
            revision,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError(completed.stderr.strip() or "fixture evaluator failed")
    payload = json.loads(completed.stdout)
    return VersionRun(
        revision=payload["revision"],
        fixtures_sha256=payload["fixtures_sha256"],
        acceptance_sha256=payload["acceptance_sha256"],
        outcomes=tuple(RegressionOutcome.from_dict(item) for item in payload["outcomes"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare GoalAnchor fixture outcomes")
    parser.add_argument("--before-root", required=True, type=Path)
    parser.add_argument("--after-root", required=True, type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--before-revision", required=True)
    parser.add_argument("--after-revision", required=True)
    parser.add_argument("--evaluator", type=Path)
    args = parser.parse_args(argv)
    evaluator = (
        args.evaluator.resolve()
        if args.evaluator
        else Path(__file__).with_name("evaluate_fixtures.py").resolve()
    )
    try:
        before = _evaluate(
            evaluator,
            args.before_root.resolve(),
            args.fixtures.resolve(),
            args.before_revision,
        )
        after = _evaluate(
            evaluator,
            args.after_root.resolve(),
            args.fixtures.resolve(),
            args.after_revision,
        )
        comparison = compare_version_runs(before, after)
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"version comparison failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(comparison.to_dict(), ensure_ascii=True, sort_keys=True, indent=2))
    return 0 if comparison.comparable and not comparison.regressions else 1


if __name__ == "__main__":
    raise SystemExit(main())
