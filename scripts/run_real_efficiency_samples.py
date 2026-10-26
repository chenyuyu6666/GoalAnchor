"""Bounded real-model efficiency samples for the GoalAnchor v1.0 baseline.

Runs three fixed single-turn tasks (LOW/MEDIUM/HIGH) through `codex exec`
with a pinned model, records measured tokens/elapsed/correct, and emits an
audit artifact. The UNKNOWN/reconcile tier requires a reconcile harness and
is intentionally NOT_RUN here; missing values stay UNKNOWN, never zero.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

TASKS = {
    "low-read-only": {
        "prompt": "Do not call tools. Reply exactly GOALANCHOR_EFF_LOW_OK.",
        "expect": "GOALANCHOR_EFF_LOW_OK",
    },
    "medium-two-reviewer": {
        "prompt": (
            "You are reviewer-1. Candidate claims full acceptance but cites no evidence E-1. "
            'Reply exactly one JSON object {"verdict":"FAIL"} and nothing else.'
        ),
        "expect_json_verdict": "FAIL",
    },
    "high-dispute-review": {
        "prompt": (
            "Reviewer-1 says PASS without evidence; reviewer-2 says FAIL citing missing E-1. "
            "As arbiter, weigh only frozen evidence. "
            'Reply exactly one JSON object {"verdict":"FAIL","reason":"missing-evidence"} and nothing else.'
        ),
        "expect_json_verdict": "FAIL",
    },
}


def _codex_argv(extra: list[str]) -> list[str]:
    import shutil
    executable = shutil.which("codex")
    if executable is None:
        raise SystemExit("codex executable not found")
    path = Path(executable)
    if path.suffix.lower() in {".cmd", ".ps1", ".bat"} or path.suffix == "":
        node = shutil.which("node")
        script = path.parent / "node_modules/@openai/codex/bin/codex.js"
        if node and script.is_file():
            return [node, str(script), "exec", *extra]
    return [executable, "exec", *extra]


def run_task(model: str, task_id: str, task: dict[str, str]) -> dict[str, object]:
    argv = _codex_argv(["-m", model, "--json", "--skip-git-repo-check", task["prompt"]])
    start = time.monotonic()
    completed = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace",
                               shell=False, timeout=240)
    elapsed = time.monotonic() - start
    tokens = None
    reply = ""
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        usage = event.get("usage")
        if isinstance(usage, dict) and all(isinstance(usage.get(k), int) for k in ("input_tokens", "output_tokens")):
            tokens = usage["input_tokens"] + usage["output_tokens"]
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                reply = item["text"].strip()
    expected = task.get("expect")
    if expected is not None:
        correct = reply == expected
    else:
        try:
            correct = json.loads(reply).get("verdict") == task["expect_json_verdict"]
        except ValueError:
            correct = False
    return {"scenario_id": task_id, "model": model, "exit_code": completed.returncode,
            "tokens_used": tokens, "token_status": "MEASURED" if tokens is not None else "UNKNOWN",
            "elapsed_seconds": round(elapsed, 3), "correct": correct, "host": "codex"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    samples = [run_task(args.model, task_id, task) for task_id, task in TASKS.items()]
    artifact = {"schema": "goalanchor.real-efficiency-samples/v1", "date": "2026-10-26",
                "boundary": "bounded single-turn fixed tasks; no tools; UNKNOWN/reconcile tier NOT_RUN "
                            "(requires reconcile harness); token totals as reported by codex exec JSON",
                "samples": samples,
                "unknown_reconcile_tier": "NOT_RUN"}
    args.output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False))
    return 0 if all(s["correct"] and s["tokens_used"] for s in samples) else 1


if __name__ == "__main__":
    sys.exit(main())
