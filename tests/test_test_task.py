from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "run_test_task.py"
TASK_PATH = ROOT / "docs" / "GoalAnchor-自动测试任务.md"


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_test_task_under_test", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_task_document_has_controlled_checks() -> None:
    runner = _load_runner()

    task = runner._load_task(TASK_PATH)

    assert task["task_id"] == "goalanchor-baseline"
    assert [check["type"] for check in task["checks"]] == [
        "pytest",
        "fixtures",
        "cli",
        "file",
    ]


def test_runner_executes_fixture_cli_and_file_checks(tmp_path: Path) -> None:
    runner = _load_runner()
    task_path = tmp_path / "task.md"
    task_path.write_text(
        """# Test task\n\n<!-- GOALANCHOR_TEST_TASK_START -->\n\n```json\n{
  \"schema_version\": 1,
  \"task_id\": \"runner-smoke\",
  \"description\": \"Runner smoke checks\",
  \"checks\": [
     {\"id\": \"fixtures\", \"type\": \"fixtures\", \"fixture_dir\": \"tests/fixtures/regression\",\
\n      \"require_all_aligned\": true, \"timeout_seconds\": 60},
    {\"id\": \"cli\", \"type\": \"cli\", \"args\": [\"--help\"], \"expected_exit\": 0},
    {\"id\": \"file\", \"type\": \"file\", \"paths\": [\"README.md\"]}
  ]
}\n```\n\n<!-- GOALANCHOR_TEST_TASK_END -->\n""",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    report = runner.run_task(task_path, root=ROOT, report_path=report_path)

    assert report["passed"] is True
    assert report["summary"] == {"total": 3, "passed": 3, "failed": 0}
    assert report_path.is_file()
    assert all(check["passed"] for check in report["checks"])


def test_task_rejects_duplicate_check_ids(tmp_path: Path) -> None:
    runner = _load_runner()
    task_path = tmp_path / "invalid.md"
    task_path.write_text(
        """<!-- GOALANCHOR_TEST_TASK_START -->
```json
{
  \"schema_version\": 1,
  \"task_id\": \"invalid-task\",
  \"description\": \"invalid\",
  \"checks\": [
    {\"id\": \"same\", \"type\": \"file\", \"paths\": [\"README.md\"]},
    {\"id\": \"same\", \"type\": \"file\", \"paths\": [\"README.md\"]}
  ]
}
```
<!-- GOALANCHOR_TEST_TASK_END -->
""",
        encoding="utf-8",
    )

    with pytest.raises(runner.TaskError, match="duplicate check id"):
        runner._load_task(task_path)


def test_task_rejects_paths_outside_project_root() -> None:
    runner = _load_runner()

    with pytest.raises(runner.TaskError, match="inside the project root"):
        runner._safe_project_path(ROOT, "../outside.txt", "path")
