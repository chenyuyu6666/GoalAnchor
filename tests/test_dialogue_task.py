from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = ROOT / "docs" / "GoalAnchor-对话体验测试任务.md"
START = "<!-- GOALANCHOR_DIALOGUE_TASK_START -->"
END = "<!-- GOALANCHOR_DIALOGUE_TASK_END -->"


def _task() -> dict:
    document = TASK_PATH.read_text(encoding="utf-8")
    section = document.split(START, 1)[1].split(END, 1)[0]
    blocks = re.findall(r"```json\s*(.*?)\s*```", section, re.DOTALL)
    assert len(blocks) == 1
    payload = json.loads(blocks[0])
    assert isinstance(payload, dict)
    return payload


def test_dialogue_task_declares_all_experience_cases() -> None:
    task = _task()

    assert task["schema_version"] == 1
    assert task["task_id"] == "goalanchor-dialogue-experience"
    cases = task["cases"]
    assert len(cases) == 9
    assert len({case["id"] for case in cases}) == len(cases)
    assert task["execution"]["mode"] == "live-dialogue"
    assert task["execution"]["isolated_context_per_case"] is True
    assert task["execution"]["minimum_rounds"] == 3


def test_dialogue_task_scoring_and_report_contract_are_complete() -> None:
    task = _task()
    scoring = task["scoring"]

    assert len(scoring["dimensions"]) == 5
    assert scoring["dimension_min"] == 0
    assert scoring["dimension_max"] == 2
    assert scoring["case_min_score"] == 8
    assert scoring["case_max_score"] == 10
    assert len(scoring["hard_failures"]) >= 5

    report = task["report_schema"]
    assert {"task_id", "tested_revision", "cases", "summary"} <= set(
        report["required_fields"]
    )
    assert {"case_id", "transcript", "scores", "outcome"} <= set(
        report["case_fields"]
    )


def test_dialogue_case_sections_match_machine_readable_ids() -> None:
    document = TASK_PATH.read_text(encoding="utf-8")
    task = _task()
    headings = set(re.findall(r"^### (DIALOGUE-[A-Z0-9-]+)$", document, re.MULTILINE))

    assert headings == {case["id"] for case in task["cases"]}
