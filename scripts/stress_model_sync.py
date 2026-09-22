"""Multi-round stress tests for the sub-agent model-consistency guarantee.

Every scenario runs fully offline against fake agents (no platform CLIs, no
tokens), so it is safe for CI and local runs alike. The scenarios hammer the
model pin end to end:

1. dispute cycles   - HIGH-risk cycles that converge on round 2 (every role
                      re-spawns in the dispute round); the model must stay
                      pinned in every binding across every round.
2. model switch     - the main model alternates between runs; each run's
                      audit must show exactly its own model (mid-run switch
                      syncs on the next dispatch, never bleeds across runs).
3. concurrency      - many parallel dispatches through one router; model
                      injection and the usage audit must stay correct.
4. retry            - transiently failing bridges with ``retries``; every
                      attempt must carry the model flag.
5. divergence fuzz  - unconfirmed divergent configs are always rejected
                      before any launch; user-confirmed ones always pass.

Usage::

    python scripts/stress_model_sync.py                     # default intensity
    python scripts/stress_model_sync.py --cycles 200 --concurrency 16

Exit code 0 means every check passed; any failure is listed in the JSON
summary and exits non-zero.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from goalanchor.agent_runtime import (  # noqa: E402
    AgentRequest,
    AgentRole,
    AgentRuntimeError,
    ReviewRisk,
    run_review_cycle_with_adapter,
)
from goalanchor.platforms import (  # noqa: E402
    PlatformAgentRouter,
    PlatformDispatchConfig,
    adapter_from_config,
)
from goalanchor.review_service import ReviewContext  # noqa: E402


REVIEWER_AGENT = r"""
import json, re, os, sys
_log = os.environ.get("STRESS_ARGV_LOG")
if _log:
    with open(f"{_log}.{os.getpid()}", "a", encoding="utf-8") as _fh:
        _fh.write(json.dumps(sys.argv[1:]) + "\n")
prompt = sys.stdin.read() if "-p" not in sys.argv else sys.argv[sys.argv.index("-p") + 1]
agent_id = re.search(r'agent_id="([^"]+)"', prompt).group(1)
fingerprint = re.search(r'context_fingerprint="([0-9a-f]{64})"', prompt).group(1)
round_number = int(re.search(r"round (\d+)", prompt).group(1))
print(json.dumps({
    "reviewer_id": agent_id,
    "round_number": round_number,
    "verdict": "PASS",
    "findings": [],
    "isolated": True,
    "context_fingerprint": fingerprint,
}))
"""

ARBITER_AGENT = r"""
import json, re, os, sys
_log = os.environ.get("STRESS_ARGV_LOG")
if _log:
    with open(f"{_log}.{os.getpid()}", "a", encoding="utf-8") as _fh:
        _fh.write(json.dumps(sys.argv[1:]) + "\n")
prompt = sys.stdin.read() if "-p" not in sys.argv else sys.argv[sys.argv.index("-p") + 1]
agent_id = re.search(r'agent_id="([^"]+)"', prompt).group(1)
fingerprint = re.search(r'context_fingerprint="([0-9a-f]{64})"', prompt).group(1)
round_number = int(re.search(r"round (\d+)", prompt).group(1))
reviewer_ids = sorted(set(re.findall(r'"reviewer_id": "(reviewer-\d+)"', prompt)))
disagree = round_number < 2
payload = {
    "arbiter_id": agent_id,
    "round_number": round_number,
    "verdict": "WARN" if disagree else "PASS",
    "summary": "stress round summary",
    "rationale": "challenged in round 1" if disagree else "reviewer results agree",
    "reviewer_ids": reviewer_ids,
    "context_fingerprint": fingerprint,
    "disagrees_with_reviewers": disagree,
}
print(json.dumps(payload))
"""

META_BRIDGE = r"""
import json, sys
request = json.loads(sys.stdin.read())
arbiter = request["arbiter_result"]
payload = {
    "reviewer_id": request["agent_id"],
    "round_number": request["round_number"],
    "arbiter_id": arbiter["arbiter_id"],
    "verdict": "PASS",
    "summary": "stress meta agrees",
    "findings": [],
    "context_fingerprint": request["context"]["fingerprint"],
    "agrees_with_arbiter": True,
    "isolated": True,
}
print(json.dumps(payload))
"""


def _context() -> ReviewContext:
    return ReviewContext(
        goal="stress: pinned-model review",
        prompt="多轮压力测试",
        constraints=("no-regression",),
        evidence_refs=("E-1",),
        candidate="stress candidate",
    )


def _write(tmp: Path, name: str, source: str) -> str:
    path = tmp / name
    path.write_text(source, encoding="utf-8")
    return str(path)


def _build_config(tmp: Path, model: str | None, *, retries: int = 1) -> dict:
    reviewer = _write(tmp, "reviewer_agent.py", REVIEWER_AGENT)
    arbiter = _write(tmp, "arbiter_agent.py", ARBITER_AGENT)
    meta = _write(tmp, "meta_bridge.py", META_BRIDGE)
    config = {
        "platforms": {
            "fake-rev": {
                "argv": [sys.executable, reviewer, "-p", "{prompt}"],
                "timeout_seconds": 120,
                "retries": retries,
                "env": {"STRESS_ARGV_LOG": str(tmp / "argv.log")},
            },
            "fake-arb": {
                "argv": [sys.executable, arbiter, "-p", "{prompt}"],
                "timeout_seconds": 120,
                "retries": retries,
                "env": {"STRESS_ARGV_LOG": str(tmp / "argv.log")},
            },
        },
        "profiles": {
            "rev-a": {"platform": "fake-rev", "roles": ["reviewer"]},
            "rev-b": {"platform": "fake-rev", "roles": ["reviewer"]},
            "arb": {"platform": "fake-arb", "roles": ["arbiter"]},
            "meta": {
                "roles": ["meta_reviewer"],
                "command": [sys.executable, meta],
            },
        },
        "role_bindings": {"arbiter": "arb", "meta_reviewer": "meta"},
        "capabilities": {"supports_parallel": True, "max_concurrency": 4},
    }
    if model is not None:
        config["model"] = model
    return config


def _run_cycle(tmp: Path, model: str | None, *, risk="HIGH", max_rounds=2):
    config = _build_config(tmp, model)
    request = {"context": _context().to_dict(), "risk": risk, "max_rounds": max_rounds}
    adapter = adapter_from_config(config)
    result = run_review_cycle_with_adapter(
        ReviewContext.from_dict(request["context"]),
        ReviewRisk(request["risk"]),
        adapter,
        max_rounds=max_rounds,
    )
    return result, adapter


def _read_argv_log(tmp: Path) -> list:
    lines: list = []
    for fragment in sorted(tmp.glob("argv.log.*")):
        lines.extend(
            json.loads(line)
            for line in fragment.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return lines


def scenario_dispute_cycles(cycles: int) -> dict:
    """HIGH-risk dispute cycles; the model must hold across both rounds."""

    failures: list[str] = []
    bindings_seen = 0
    for index in range(cycles):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            result, _adapter = _run_cycle(tmp, f"stress-main-{index}")
            if result.outcome.value != "PASS" or result.round_number != 2:
                failures.append(
                    f"cycle {index}: outcome={result.outcome.value} round={result.round_number}"
                )
                continue
            for binding in result.agent_bindings:
                bindings_seen += 1
                if binding.get("platform") == "stdio-bridge":
                    # bridge profiles manage their own model; none is injected
                    if binding.get("model") is not None:
                        failures.append(
                            f"cycle {index}: bridge binding unexpectedly pinned: {binding}"
                        )
                elif binding.get("model") != f"stress-main-{index}":
                    failures.append(
                        f"cycle {index}: binding {binding.get('agent_id')} model="
                        f"{binding.get('model')!r}"
                    )
            for argv in _read_argv_log(tmp):
                if "-m" not in argv or argv[argv.index("-m") + 1] != f"stress-main-{index}":
                    failures.append(f"cycle {index}: unpinned argv {argv}")
    return {
        "scenario": "dispute_cycles",
        "iterations": cycles,
        "checks": bindings_seen + cycles,
        "failures": failures,
    }


def scenario_model_switch(runs: int) -> dict:
    """Alternate the main model between runs; no bleed across runs."""

    models = [f"model-{chr(ord('A') + index % 6)}" for index in range(runs)]
    failures: list[str] = []
    for index, model in enumerate(models):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            result, _adapter = _run_cycle(tmp, model)
            used = sorted(
                {item["model"] for item in result.agent_bindings if item.get("model")}
            )
            if used != [model]:
                failures.append(f"run {index}: models_used={used} expected [{model!r}]")
    return {
        "scenario": "model_switch",
        "iterations": runs,
        "checks": runs,
        "failures": failures,
    }


def scenario_concurrency(workers: int, rounds: int) -> dict:
    """Parallel dispatches through one router; injection stays correct."""

    failures: list[str] = []
    total = 0
    for round_index in range(rounds):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            model = f"conc-{round_index}"
            config = _build_config(tmp, model)
            router = PlatformAgentRouter(PlatformDispatchConfig.from_dict(config))
            context = _context()
            request = {
                "role": AgentRole.REVIEWER,
                "round_number": 1,
                "risk": ReviewRisk.LOW,
                "context": context,
            }

            def dispatch(agent_id: str):
                return router.dispatch(
                    "rev-a",
                    AgentRequest(
                        role=request["role"],
                        agent_id=agent_id,
                        round_number=request["round_number"],
                        risk=request["risk"],
                        context=request["context"],
                    ),
                )

            with ThreadPoolExecutor(max_workers=workers) as pool:
                payloads = list(
                    pool.map(dispatch, [f"conc-reviewer-{i}" for i in range(workers)])
                )
            usage = router.usage_report()
            total += len(usage)
            for payload in payloads:
                if payload.get("verdict") != "PASS":
                    failures.append(f"round {round_index}: bad payload {payload}")
            for item in usage:
                if item.get("model") != model:
                    failures.append(
                        f"round {round_index}: usage model={item.get('model')!r}"
                    )
    return {
        "scenario": "concurrency",
        "iterations": rounds * workers,
        "checks": total * 2,
        "failures": failures,
    }


def scenario_retry(failed_attempts: int, retries: int, iterations: int) -> dict:
    """Every retry attempt must still carry the pinned model flag."""

    from goalanchor import platforms as platforms_module

    failures: list[str] = []
    real_run = platforms_module._run_subprocess
    for iteration in range(iterations):
        captured: list[tuple] = []
        state = {"calls": 0}
        lock = threading.Lock()

        def flaky(argv, *, input_text, timeout_seconds, env):
            with lock:
                state["calls"] += 1
                call = state["calls"]
                captured.append(tuple(argv))
            if call <= failed_attempts:
                raise AgentRuntimeError("stress transient failure")
            prompt = input_text if input_text is not None else argv[-1]
            stripped = prompt.lstrip()
            if stripped.startswith("{"):
                request = json.loads(prompt)
                arbiter = request["arbiter_result"]
                payload = {
                    "reviewer_id": request["agent_id"],
                    "round_number": request["round_number"],
                    "arbiter_id": arbiter["arbiter_id"],
                    "verdict": arbiter["verdict"],
                    "summary": "stress meta agrees",
                    "findings": [],
                    "context_fingerprint": request["context"]["fingerprint"],
                    "agrees_with_arbiter": True,
                    "isolated": True,
                }
            elif "# GoalAnchor arbiter review" in prompt:
                agent_id = re.search(r'agent_id="([^"]+)"', prompt).group(1)
                fingerprint = re.search(
                    r'context_fingerprint="([0-9a-f]{64})"', prompt
                ).group(1)
                reviewer_ids = sorted(
                    set(re.findall(r'"reviewer_id": "(reviewer-\d+)"', prompt))
                )
                payload = {
                    "arbiter_id": agent_id,
                    "round_number": 1,
                    "verdict": "PASS",
                    "summary": "stress arbiter agrees",
                    "rationale": "reviewer results agree",
                    "reviewer_ids": reviewer_ids,
                    "context_fingerprint": fingerprint,
                    "disagrees_with_reviewers": False,
                }
            else:
                agent_id = re.search(r'agent_id="([^"]+)"', prompt).group(1)
                fingerprint = re.search(
                    r'context_fingerprint="([0-9a-f]{64})"', prompt
                ).group(1)
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": 1,
                    "verdict": "PASS",
                    "findings": [],
                    "isolated": True,
                    "context_fingerprint": fingerprint,
                }
            return json.dumps(payload), ""

        platforms_module._run_subprocess = flaky
        try:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = _build_config(tmp, "retry-model", retries=retries)
                adapter = adapter_from_config(config)
                result = run_review_cycle_with_adapter(
                    _context(), ReviewRisk.LOW, adapter, max_rounds=1
                )
                if result.outcome.value != "PASS":
                    failures.append(f"iteration {iteration}: outcome={result.outcome.value}")
                # the transient failures all land inside the first call (the
                # single LOW-risk reviewer); arbiter and meta then succeed on
                # their first attempt
                expected_attempts = failed_attempts + 3
                if len(captured) != expected_attempts:
                    failures.append(
                        f"iteration {iteration}: attempts={len(captured)} "
                        f"expected {expected_attempts}"
                    )
                for argv in captured:
                    if "-p" in argv:
                        # platform-CLI attempt: the model flag is mandatory
                        if "-m" not in argv or argv[argv.index("-m") + 1] != "retry-model":
                            failures.append(
                                f"iteration {iteration}: attempt lost model: {argv}"
                            )
                    else:
                        # bridge attempt: no model flag may be injected
                        if "-m" in argv:
                            failures.append(
                                f"iteration {iteration}: bridge got a model flag: {argv}"
                            )
        finally:
            platforms_module._run_subprocess = real_run
    return {
        "scenario": "retry",
        "iterations": iterations,
        "checks": iterations * (failed_attempts + 3),
        "failures": failures,
    }


def scenario_divergence_fuzz(cases: int, seed: int = 20260921) -> dict:
    """Random divergent configs: rejected without launch, or user-confirmed.

    The seeded RNG is deliberate: fuzz cases must be reproducible run to
    run, so ``random`` (not ``secrets``) is the correct source here — this
    is not a security-sensitive use.
    """

    rng = random.Random(seed)
    from goalanchor import platforms as platforms_module

    failures: list[str] = []
    launch_attempts = {"count": 0}
    real_run = platforms_module._run_subprocess

    def no_launch(*args, **kwargs):
        launch_attempts["count"] += 1
        raise AssertionError("subprocess must not be launched for rejected configs")

    platforms_module._run_subprocess = no_launch
    try:
        for index in range(cases):
            main_model = f"main-{rng.randrange(1000)}"
            divergent = f"other-{rng.randrange(1000)}"
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = _build_config(tmp, main_model)
                config["profiles"]["rev-b"]["model"] = divergent
                if rng.random() < 0.5:
                    # user-confirmed divergence: restore real subprocess runs
                    # so the full cycle can execute against the fake agents
                    platforms_module._run_subprocess = real_run
                    try:
                        config["model_divergence"] = {
                            "confirmed_by": "user",
                            "reason": f"stress case {index}",
                        }
                        adapter = adapter_from_config(config)
                        result = run_review_cycle_with_adapter(
                            _context(), ReviewRisk.HIGH, adapter, max_rounds=2
                        )
                        by_agent = {
                            item["agent_id"]: item["model"] for item in result.agent_bindings
                        }
                        if by_agent.get("reviewer-2") != divergent:
                            failures.append(
                                f"case {index}: confirmed divergence not applied: {by_agent}"
                            )
                    finally:
                        platforms_module._run_subprocess = no_launch
                else:
                    try:
                        adapter_from_config(config)
                    except AgentRuntimeError as exc:
                        if "model_divergence" not in str(exc):
                            failures.append(f"case {index}: unhelpful error: {exc}")
                    else:
                        failures.append(f"case {index}: divergence not rejected")
    finally:
        platforms_module._run_subprocess = real_run
    if launch_attempts["count"]:
        failures.append(
            f"{launch_attempts['count']} subprocess launches happened during rejection cases"
        )
    return {
        "scenario": "divergence_fuzz",
        "iterations": cases,
        "checks": cases,
        "failures": failures,
    }


def scenario_cli_end_to_end(runs: int) -> dict:
    """Full CLI process per run on the offline example; --model alternates.

    Each run also exercises the machine-readable ``warnings`` channel: the
    pinned leg must stay silent, and an unpinned copy of the same config must
    report ``MODEL_NOT_PINNED`` with a null ``main_model``.
    """

    example = ROOT / "examples" / "multi-platform-review"
    request_path = example / "request.json"
    config_path = example / "config.json"
    failures: list[str] = []
    for index in range(runs):
        model = f"cli-model-{index}"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "goalanchor",
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(config_path),
                "--model",
                model,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=ROOT,
            timeout=300,
        )
        if completed.returncode != 0:
            failures.append(
                f"run {index}: exit {completed.returncode}: {completed.stderr[-300:]}"
            )
            continue
        payload = json.loads(completed.stdout)
        if payload.get("outcome") != "PASS":
            failures.append(f"run {index}: outcome={payload.get('outcome')}")
        if payload.get("usage_summary", {}).get("models_used") != [model]:
            failures.append(
                f"run {index}: models_used={payload.get('usage_summary', {}).get('models_used')}"
            )
        if payload.get("warnings") != []:
            failures.append(f"run {index}: pinned warnings={payload.get('warnings')}")
        unpinned_config = json.loads(config_path.read_text(encoding="utf-8"))
        unpinned_config.pop("model", None)
        with tempfile.TemporaryDirectory() as tmp:
            unpinned_path = Path(tmp) / "config-unpinned.json"
            unpinned_path.write_text(json.dumps(unpinned_config), encoding="utf-8")
            unpinned_run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "goalanchor",
                    "review-cycle",
                    "--request",
                    str(request_path),
                    "--config",
                    str(unpinned_path),
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                cwd=ROOT,
                timeout=300,
            )
        if unpinned_run.returncode != 0:
            failures.append(
                f"run {index}: unpinned exit {unpinned_run.returncode}: "
                f"{unpinned_run.stderr[-300:]}"
            )
            continue
        unpinned_payload = json.loads(unpinned_run.stdout)
        codes = [item.get("code") for item in unpinned_payload.get("warnings", [])]
        if codes != ["MODEL_NOT_PINNED"]:
            failures.append(f"run {index}: unpinned warnings={codes}")
        if unpinned_payload.get("usage_summary", {}).get("main_model") is not None:
            failures.append(f"run {index}: unpinned main_model is not null")
    return {
        "scenario": "cli_end_to_end",
        "iterations": runs,
        "checks": runs * 5,
        "failures": failures,
    }


def run_stress(
    *,
    cycles: int,
    switch_runs: int,
    concurrency: int,
    concurrency_rounds: int,
    retry_iterations: int,
    fuzz_cases: int,
    cli_runs: int,
) -> list[dict]:
    with contextlib.redirect_stdout(io.StringIO()):
        results = [
            scenario_dispute_cycles(cycles),
            scenario_model_switch(switch_runs),
            scenario_concurrency(concurrency, concurrency_rounds),
            scenario_retry(2, 3, retry_iterations),
            scenario_divergence_fuzz(fuzz_cases),
            scenario_cli_end_to_end(cli_runs),
        ]
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=40, help="dispute-cycle rounds")
    parser.add_argument("--switch-runs", type=int, default=24, help="model-switch runs")
    parser.add_argument("--concurrency", type=int, default=12, help="parallel dispatches")
    parser.add_argument("--concurrency-rounds", type=int, default=5)
    parser.add_argument("--retry-iterations", type=int, default=25)
    parser.add_argument("--fuzz-cases", type=int, default=60)
    parser.add_argument("--cli-runs", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="machine-readable output only")
    args = parser.parse_args(argv)

    if not args.json:
        print(
            f"stress: cycles={args.cycles} switch={args.switch_runs} "
            f"concurrency={args.concurrency}x{args.concurrency_rounds} "
            f"retry={args.retry_iterations} fuzz={args.fuzz_cases} cli={args.cli_runs}",
            file=sys.stderr,
        )
    results = run_stress(
        cycles=args.cycles,
        switch_runs=args.switch_runs,
        concurrency=args.concurrency,
        concurrency_rounds=args.concurrency_rounds,
        retry_iterations=args.retry_iterations,
        fuzz_cases=args.fuzz_cases,
        cli_runs=args.cli_runs,
    )
    total_failures = sum(len(item["failures"]) for item in results)
    summary = {
        "stress": "model-sync",
        "scenarios": results,
        "total_failures": total_failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if total_failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
