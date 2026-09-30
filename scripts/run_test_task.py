"""Run a controlled, machine-readable GoalAnchor test task from Markdown."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TASK_START = "<!-- GOALANCHOR_TEST_TASK_START -->"
TASK_END = "<!-- GOALANCHOR_TEST_TASK_END -->"
JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
TASK_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CHECK_TYPES = {"pytest", "fixtures", "cli", "file"}
DEFAULT_TIMEOUT_SECONDS = 300
MAX_CAPTURED_OUTPUT = 8000


class TaskError(ValueError):
    """Raised when a test task is invalid or cannot be prepared."""


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TaskError(f"{field_name} must be an object")
    return value


def _as_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskError(f"{field_name} must be a non-empty string")
    return value


def _as_string_list(value: Any, field_name: str, *, required: bool = True) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TaskError(f"{field_name} must be an array of strings")
    return list(value)


def _as_positive_timeout(value: Any, field_name: str) -> float:
    if value is None:
        return float(DEFAULT_TIMEOUT_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise TaskError(f"{field_name} must be greater than zero")
    return float(value)


def _load_task(task_path: Path) -> dict[str, Any]:
    try:
        document = task_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskError(f"unable to read task document: {task_path}") from exc

    start = document.find(TASK_START)
    end = document.find(TASK_END)
    if start < 0 or end < 0 or end <= start:
        raise TaskError(
            f"task document must contain {TASK_START} and {TASK_END} markers"
        )
    section = document[start + len(TASK_START) : end]
    blocks = JSON_BLOCK_RE.findall(section)
    if len(blocks) != 1:
        raise TaskError("task document must contain exactly one JSON block between markers")
    try:
        payload = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise TaskError(f"task JSON is invalid: {exc}") from exc
    return _validate_task(payload)


def _validate_task(payload: Any) -> dict[str, Any]:
    task = dict(_as_mapping(payload, "task"))
    if task.get("schema_version") != 1:
        raise TaskError("schema_version must be 1")
    task_id = _as_string(task.get("task_id"), "task_id")
    if not TASK_ID_RE.fullmatch(task_id):
        raise TaskError("task_id must use lowercase hyphen-case")
    _as_string(task.get("description"), "description")
    checks = task.get("checks")
    if not isinstance(checks, list) or not checks:
        raise TaskError("checks must be a non-empty array")

    normalized_checks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_check in enumerate(checks):
        check = dict(_as_mapping(raw_check, f"checks[{index}]"))
        check_id = _as_string(check.get("id"), f"checks[{index}].id")
        if not TASK_ID_RE.fullmatch(check_id):
            raise TaskError(f"checks[{index}].id must use lowercase hyphen-case")
        if check_id in seen_ids:
            raise TaskError(f"duplicate check id: {check_id}")
        seen_ids.add(check_id)
        check_type = _as_string(check.get("type"), f"checks[{index}].type")
        if check_type not in CHECK_TYPES:
            allowed = ", ".join(sorted(CHECK_TYPES))
            raise TaskError(f"unsupported check type {check_type!r}; use {allowed}")
        check["id"] = check_id
        check["type"] = check_type
        check["timeout_seconds"] = _as_positive_timeout(
            check.get("timeout_seconds"), f"checks[{index}].timeout_seconds"
        )

        if check_type in {"pytest", "cli"}:
            check["args"] = _as_string_list(
                check.get("args", []), f"checks[{index}].args", required=False
            )
        if check_type == "fixtures":
            check["fixture_dir"] = _as_string(
                check.get("fixture_dir"), f"checks[{index}].fixture_dir"
            )
            if not isinstance(check.get("require_all_aligned", False), bool):
                raise TaskError(f"checks[{index}].require_all_aligned must be boolean")
        if check_type == "cli":
            expected_exit = check.get("expected_exit", 0)
            if isinstance(expected_exit, bool) or not isinstance(expected_exit, int):
                raise TaskError(f"checks[{index}].expected_exit must be an integer")
            check["expected_exit"] = expected_exit
            assertions = _as_mapping(
                check.get("json_assertions", {}), f"checks[{index}].json_assertions"
            )
            if any(not isinstance(key, str) or not key for key in assertions):
                raise TaskError(f"checks[{index}].json_assertions keys must be strings")
            check["json_assertions"] = dict(assertions)
        if check_type == "file":
            paths = _as_string_list(check.get("paths"), f"checks[{index}].paths")
            if not paths:
                raise TaskError(f"checks[{index}].paths must not be empty")
            check["paths"] = paths
        normalized_checks.append(check)

    task["task_id"] = task_id
    task["checks"] = normalized_checks
    return task


def _safe_project_path(root: Path, raw_path: str, field_name: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise TaskError(f"{field_name} must be relative to the project root")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        inside = resolved.is_relative_to(resolved_root)
    except AttributeError:
        inside = os.path.commonpath((str(resolved_root), str(resolved))) == str(resolved_root)
    if not inside:
        raise TaskError(f"{field_name} must stay inside the project root")
    return resolved


def _git_revision(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    return "unknown"


def _capture(value: str) -> str:
    if len(value) <= MAX_CAPTURED_OUTPUT:
        return value
    return value[:MAX_CAPTURED_OUTPUT] + "\n...[output truncated]"


def _run_subprocess(
    command: Sequence[str], root: Path, timeout_seconds: float
) -> tuple[int | None, str, str, bool]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return None, stdout, stderr, True
    return completed.returncode, completed.stdout, completed.stderr, False


def _json_path(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def _base_result(check: Mapping[str, Any], started: float) -> dict[str, Any]:
    return {
        "id": check["id"],
        "type": check["type"],
        "passed": False,
        "duration_ms": 0,
        "exit_code": None,
        "command": [],
        "stdout": "",
        "stderr": "",
        "details": {},
        "error": None,
        "_started": started,
    }


def _finish_result(result: dict[str, Any]) -> dict[str, Any]:
    started = result.pop("_started")
    result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    result["stdout"] = _capture(result["stdout"])
    result["stderr"] = _capture(result["stderr"])
    return result


def _run_check(check: Mapping[str, Any], root: Path, revision: str) -> dict[str, Any]:
    result = _base_result(check, time.monotonic())
    check_type = check["type"]
    timeout_seconds = float(check["timeout_seconds"])

    if check_type == "file":
        missing = []
        for raw_path in check["paths"]:
            path = _safe_project_path(root, raw_path, f"{check['id']}.paths")
            if not path.is_file():
                missing.append(raw_path)
        result["details"] = {"checked_paths": check["paths"], "missing_paths": missing}
        result["passed"] = not missing
        if missing:
            result["error"] = "required file(s) missing"
        return _finish_result(result)

    if check_type == "pytest":
        command = [sys.executable, "-m", "pytest", *check["args"]]
    elif check_type == "fixtures":
        fixture_dir = _safe_project_path(
            root, check["fixture_dir"], f"{check['id']}.fixture_dir"
        )
        command = [
            sys.executable,
            "scripts/evaluate_fixtures.py",
            "--fixtures",
            str(fixture_dir),
            "--package-root",
            str(root),
            "--revision",
            revision,
        ]
    elif check_type == "cli":
        command = [sys.executable, "-m", "goalanchor", *check["args"]]
    else:
        raise TaskError(f"unsupported check type: {check_type}")

    result["command"] = command
    exit_code, stdout, stderr, timed_out = _run_subprocess(command, root, timeout_seconds)
    result["exit_code"] = exit_code
    result["stdout"] = stdout
    result["stderr"] = stderr
    if timed_out:
        result["error"] = f"timed out after {timeout_seconds:g} seconds"
        return _finish_result(result)

    if check_type == "fixtures":
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            result["error"] = f"fixture evaluator did not return JSON: {exc}"
            return _finish_result(result)
        result["details"] = {
            "total": payload.get("total"),
            "aligned_count": payload.get("aligned_count"),
            "accepted_count": payload.get("accepted_count"),
            "rejected_count": payload.get("rejected_count"),
        }
        aligned = payload.get("aligned_count") == payload.get("total")
        if check.get("require_all_aligned", False) and not aligned:
            result["error"] = "not all regression fixtures aligned"
            return _finish_result(result)
        result["passed"] = exit_code == 0 and (
            aligned or not check.get("require_all_aligned", False)
        )
        if not result["passed"] and not result["error"]:
            result["error"] = f"expected exit code 0, received {exit_code}"
        return _finish_result(result)

    if check_type == "cli":
        assertions = check["json_assertions"]
        if assertions:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError as exc:
                result["error"] = f"CLI output is not JSON: {exc}"
                return _finish_result(result)
            failures = []
            for path, expected in assertions.items():
                try:
                    actual = _json_path(parsed, path)
                except KeyError:
                    failures.append({"path": path, "expected": expected, "error": "missing"})
                    continue
                if actual != expected:
                    failures.append(
                        {"path": path, "expected": expected, "actual": actual}
                    )
            result["details"] = {
                "json_assertions": assertions,
                "assertion_failures": failures,
            }
            if failures:
                result["error"] = "CLI JSON assertions failed"
                return _finish_result(result)
        result["passed"] = exit_code == check["expected_exit"]
        if not result["passed"]:
            result["error"] = (
                f"expected exit code {check['expected_exit']}, received {exit_code}"
            )
        return _finish_result(result)

    result["passed"] = exit_code == 0
    if not result["passed"]:
        result["error"] = f"expected exit code 0, received {exit_code}"
    return _finish_result(result)


def run_task(
    task_path: Path, *, root: Path = ROOT, report_path: Path | None = None
) -> dict[str, Any]:
    task = _load_task(task_path.resolve())
    root = root.resolve()
    revision = _git_revision(root)
    started_at = datetime.now(UTC)
    results = [_run_check(check, root, revision) for check in task["checks"]]
    passed = all(result["passed"] for result in results)
    finished_at = datetime.now(UTC)
    report = {
        "schema_version": 1,
        "task_id": task["task_id"],
        "description": task["description"],
        "task_path": str(task_path.resolve()),
        "project_root": str(root),
        "revision": revision,
        "task_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "passed": passed,
        "summary": {
            "total": len(results),
            "passed": sum(result["passed"] for result in results),
            "failed": sum(not result["passed"] for result in results),
        },
        "checks": results,
    }
    if report_path is not None:
        report_path = report_path.resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a controlled GoalAnchor test task")
    parser.add_argument(
        "--task",
        type=Path,
        default=ROOT / "docs" / "GoalAnchor-自动测试任务.md",
        help="Markdown test task document",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=ROOT,
        help="Project root used to resolve test paths",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "test-results" / "goalanchor-baseline.json",
        help="JSON report path",
    )
    args = parser.parse_args(argv)
    try:
        report = run_task(args.task, root=args.project_root, report_path=args.report)
    except (OSError, TaskError, subprocess.SubprocessError) as exc:
        print(f"test task failed to start: {exc}", file=sys.stderr)
        return 2
    print(
        f"{report['task_id']}: "
        f"{report['summary']['passed']}/{report['summary']['total']} checks passed "
        f"at {report['revision']}"
    )
    print(f"Report: {args.report.resolve()}")
    for check in report["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        suffix = f" - {check['error']}" if check["error"] else ""
        print(f"[{status}] {check['id']} ({check['duration_ms']} ms){suffix}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
