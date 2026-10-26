"""P6 before/after experiment: baseline vs compact prompt for fixed review tasks.

Runs MEDIUM/HIGH review tasks twice each (baseline prompt, compact prompt),
compares measured tokens, elapsed time and correctness, and feeds the result
into PromptOptimizer only when correctness holds and tokens do not increase.
The artifact records the decision and every measured value; nothing is
inferred from a single direction without the paired run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_real_efficiency_samples import _codex_argv  # noqa: E402

PAIRS = {
    "medium-two-reviewer": {
        "baseline": (
            "You are reviewer-1 evaluating a candidate. The candidate states: 'All acceptance "
            "criteria are satisfied and the goal is complete.' The frozen evidence list is empty; "
            "the review requires evidence E-1 for the acceptance claim. Do not call tools. Reply "
            'exactly one JSON object {"verdict":"FAIL"} and nothing else.'
        ),
        "compact": (
            'Reviewer-1. Candidate claims full acceptance; evidence E-1 absent. No tools. Reply '
            'exactly {"verdict":"FAIL"}.'
        ),
        "expect_json_verdict": "FAIL",
    },
    "high-dispute-review": {
        "baseline": (
            "You are the arbiter for a disputed review. Reviewer-1 returned PASS accepting the "
            "candidate's completeness claim. Reviewer-2 returned FAIL because the claim cites no "
            "evidence and E-1 is absent from the frozen evidence list. Weigh only the frozen "
            "evidence. Do not call tools. Reply exactly one JSON object "
            '{"verdict":"FAIL","reason":"missing-evidence"} and nothing else.'
        ),
        "compact": (
            'Arbiter. R1 PASS (no evidence), R2 FAIL (E-1 absent). Frozen evidence only. No tools. '
            'Reply exactly {"verdict":"FAIL","reason":"missing-evidence"}.'
        ),
        "expect_json_verdict": "FAIL",
    },
}


def run_prompt(model: str, prompt: str, cwd: str) -> dict[str, object]:
    argv = _codex_argv(["-m", model, "--json", "--skip-git-repo-check", prompt])
    start = time.monotonic()
    completed = __import__("subprocess").run(argv, capture_output=True, encoding="utf-8",
                                             errors="replace", shell=False, timeout=240, cwd=cwd)
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
    return {"exit_code": completed.returncode, "tokens_used": tokens, "elapsed_seconds": round(elapsed, 3),
            "reply": reply}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import tempfile

    from goalanchor.efficiency_governance import PromptOptimizer

    results: dict[str, object] = {}
    accepted: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="ga-p6-exp-") as root:
        for task_id, pair in PAIRS.items():
            run_a = run_prompt(args.model, pair["baseline"], root)
            run_b = run_prompt(args.model, pair["compact"], root)
            correct_a = _correct(run_a, pair)
            correct_b = _correct(run_b, pair)
            token_delta = (run_b["tokens_used"] or 0) - (run_a["tokens_used"] or 0)
            results[task_id] = {
                "baseline": {k: v for k, v in run_a.items() if k != "reply"} | {"correct": correct_a},
                "compact": {k: v for k, v in run_b.items() if k != "reply"} | {"correct": correct_b},
                "token_delta": token_delta,
                "elapsed_delta_seconds": round(run_b["elapsed_seconds"] - run_a["elapsed_seconds"], 3),
            }
            if correct_a and correct_b and run_b["tokens_used"] is not None and token_delta < 0:
                digest = hashlib.sha256(pair["baseline"].encode()).hexdigest()
                optimizer = PromptOptimizer()
                optimizer.remember(digest, pair["compact"], token_saving=-token_delta)
                accepted[task_id] = {"context_fingerprint": digest, "token_saving": -token_delta}
    artifact = {
        "schema": "goalanchor.p6-prompt-optimizer-experiment/v1",
        "date": "2026-10-26",
        "model": args.model,
        "boundary": "single-pair runs per tier; acceptance requires both runs correct and token delta < 0; "
                    "no claim beyond these fixed tasks",
        "results": results,
        "optimizer_accepted": accepted,
        "decision": "PARTIAL/BOUNDED" if accepted else "NOT_ACCEPTED",
    }
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False))
    return 0


def _correct(run: dict[str, object], pair: dict[str, str]) -> bool:
    try:
        return json.loads(str(run["reply"])).get("verdict") == pair["expect_json_verdict"]
    except ValueError:
        return False


if __name__ == "__main__":
    sys.exit(main())
