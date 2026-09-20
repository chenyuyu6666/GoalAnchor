"""Tests for the built-in multi-platform Agent dispatch adapters."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from goalanchor.agent_runtime import (
    AgentRequest,
    AgentRole,
    AgentRuntimeError,
    AgentRuntimeCapabilities,
    DisputePacket,
    JsonStdioAgentAdapter,
    ReviewRisk,
    run_review_cycle_with_adapter,
)
from goalanchor.integration import ReviewFinding
from goalanchor.platforms import (
    PlatformDispatchConfig,
    ProfileBinding,
    adapter_from_config,
    extract_result_payload,
    render_agent_prompt,
)
from goalanchor.review_service import ArbiterResult, ReviewContext, ReviewerResult

FAKE_AGENT = r"""
import json, re, sys
prompt = sys.argv[sys.argv.index("-p") + 1] if "-p" in sys.argv else sys.stdin.read()
mode = sys.argv[1]
role = re.search(r"# GoalAnchor (\w+) review", prompt).group(1)
agent_id = re.search(r'agent_id="([^"]+)"', prompt).group(1)
fingerprint = re.search(r'context_fingerprint="([0-9a-f]{64})"', prompt).group(1)
round_number = int(re.search(r"round (\d+)", prompt).group(1))
if role == "reviewer":
    payload = {
        "reviewer_id": agent_id,
        "round_number": round_number,
        "verdict": "PASS",
        "findings": [],
        "isolated": True,
        "context_fingerprint": fingerprint,
    }
elif role == "arbiter":
    reviewer_ids = sorted(set(re.findall(r'"reviewer_id": "(reviewer-\d+)"', prompt)))
    payload = {
        "arbiter_id": agent_id,
        "round_number": round_number,
        "verdict": "PASS",
        "summary": "consistent",
        "rationale": "reviewer results agree",
        "reviewer_ids": reviewer_ids,
        "context_fingerprint": fingerprint,
        "disagrees_with_reviewers": False,
    }
else:
    arbiter_id = re.search(r"arbiter_id: (\S+)", prompt).group(1)
    payload = {
        "reviewer_id": agent_id,
        "round_number": round_number,
        "arbiter_id": arbiter_id,
        "verdict": "PASS",
        "summary": "agrees",
        "findings": [],
        "context_fingerprint": fingerprint,
        "agrees_with_arbiter": True,
        "isolated": True,
    }
if mode == "envelope":
    print(json.dumps({"type": "result", "result": json.dumps(payload)}))
elif mode == "noisy":
    print("Here is my review:\n```json\n" + json.dumps(payload) + "\n```\nThanks.")
else:
    print(json.dumps(payload))
"""

FAKE_ARBITER = r"""
import json, re, sys
prompt = sys.argv[sys.argv.index("-p") + 1]
agent_id = re.search(r'agent_id="([^"]+)"', prompt).group(1)
fingerprint = re.search(r'context_fingerprint="([0-9a-f]{64})"', prompt).group(1)
round_number = int(re.search(r"round (\d+)", prompt).group(1))
reviewer_ids = sorted(set(re.findall(r'"reviewer_id": "(reviewer-\d+)"', prompt)))
disagree = round_number < 2
payload = {
    "arbiter_id": agent_id,
    "round_number": round_number,
    "verdict": "WARN" if disagree else "PASS",
    "summary": "round summary",
    "rationale": "challenged in round 1",
    "reviewer_ids": reviewer_ids,
    "context_fingerprint": fingerprint,
    "disagrees_with_reviewers": disagree,
}
print(json.dumps(payload))
"""

FAKE_META_BRIDGE = r"""
import json, sys
request = json.loads(sys.stdin.read())
payload = {
    "reviewer_id": request["agent_id"],
    "round_number": request["round_number"],
    "arbiter_id": request["arbiter_result"]["arbiter_id"],
    "verdict": "PASS",
    "summary": "agrees",
    "findings": [],
    "context_fingerprint": request["context"]["fingerprint"],
    "agrees_with_arbiter": True,
    "isolated": True,
}
print(json.dumps(payload))
"""


def _write_script(tmp_path: Path, name: str, source: str) -> str:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path)


@pytest.fixture()
def context() -> ReviewContext:
    return ReviewContext(
        goal="交付经过验证的修复",
        prompt="审核以下候选结论",
        constraints=("不得引入回归",),
        evidence_refs=("E-1",),
        candidate="候选修复说明",
    )


def _dispatch_config(tmp_path: Path, **overrides) -> dict:
    agent = _write_script(tmp_path, "agent.py", FAKE_AGENT)
    meta = _write_script(tmp_path, "meta_bridge.py", FAKE_META_BRIDGE)
    config = {
        "platforms": {
            "fake-clean": {
                "argv": [sys.executable, agent, "clean", "-p", "{prompt}"],
                "timeout_seconds": 120,
            },
            "fake-envelope": {"argv": [sys.executable, agent, "envelope", "-p", "{prompt}"]},
            "fake-noisy": {"argv": [sys.executable, agent, "noisy", "-p", "{prompt}"]},
        },
        "profiles": {
            "euclid": {
                "platform": "fake-envelope",
                "roles": ["reviewer"],
                "display_name": "Euclid",
            },
            "volt": {"platform": "fake-noisy", "roles": ["reviewer"]},
            "judge": {"platform": "fake-clean", "roles": ["arbiter"]},
            "watcher": {"platform": "fake-clean", "roles": ["meta_reviewer"]},
        },
        "role_bindings": {"arbiter": "judge", "meta_reviewer": "watcher"},
        "capabilities": {"supports_parallel": True, "max_concurrency": 4},
    }
    config.update(overrides)
    return config


class TestRenderAgentPrompt:
    def test_reviewer_prompt_contains_shared_context(self, context: ReviewContext):
        request = AgentRequest(
            role=AgentRole.REVIEWER,
            agent_id="reviewer-1",
            round_number=1,
            risk=ReviewRisk.HIGH,
            context=context,
        )
        prompt = render_agent_prompt(request)
        assert "independent reviewers running in parallel" in prompt
        assert 'agent_id="reviewer-1"' in prompt
        assert context.fingerprint in prompt
        assert "交付经过验证的修复" in prompt
        assert '"verdict": "PASS|WARN|FAIL"' in prompt

    def test_arbiter_prompt_includes_reviewer_results(self, context: ReviewContext):
        finding = ReviewFinding(
            finding_id="F-1",
            issue="缺少回归证据",
            evidence_refs=("E-2",),
            decision_impact="阻塞验收",
            remaining_uncertainty="未在目标设备复验",
        )
        request = AgentRequest(
            role=AgentRole.ARBITER,
            agent_id="arbiter",
            round_number=1,
            risk=ReviewRisk.HIGH,
            context=context,
            reviewer_results=(
                ReviewerResult(
                    reviewer_id="reviewer-1",
                    round_number=1,
                    verdict="WARN",
                    findings=(finding,),
                    context_fingerprint=context.fingerprint,
                ),
            ),
        )
        prompt = render_agent_prompt(request)
        assert "reviewer results" in prompt
        assert "F-1" in prompt
        assert "disagrees_with_reviewers" in prompt

    def test_dispute_packet_is_included(self, context: ReviewContext):
        request = AgentRequest(
            role=AgentRole.REVIEWER,
            agent_id="reviewer-1",
            round_number=2,
            risk=ReviewRisk.HIGH,
            context=context,
            dispute_packet=DisputePacket(
                context_fingerprint=context.fingerprint,
                round_number=1,
                arbiter_summary="arbiter summary",
                arbiter_rationale="arbiter rationale",
                reviewer_findings=(),
                re_review_prompt="re-review the same context",
            ),
        )
        prompt = render_agent_prompt(request)
        assert "DisputePacket" in prompt
        assert "re-review the same context" in prompt
        assert "(round 2)" in prompt


class TestExtractResultPayload:
    def test_direct_json_object(self):
        payload = {"verdict": "PASS", "summary": "ok"}
        assert extract_result_payload(json.dumps(payload)) == payload

    def test_claude_style_envelope(self):
        inner = json.dumps({"reviewer_id": "reviewer-1", "verdict": "PASS"})
        wrapped = json.dumps({"type": "result", "subtype": "success", "result": inner})
        assert extract_result_payload(wrapped)["verdict"] == "PASS"

    def test_json_embedded_in_prose_and_fence(self):
        text = 'My review:\n```json\n{"verdict": "WARN", "findings": []}\n```\nDone.'
        assert extract_result_payload(text)["verdict"] == "WARN"

    def test_nested_envelope_object(self):
        inner = {"verdict": "PASS", "summary": "s"}
        wrapped = {"response": {"content": inner}}
        assert extract_result_payload(json.dumps(wrapped)) == inner

    def test_codex_style_transcript_returns_final_answer(self):
        echoed_context = json.dumps(
            {"goal": "修复缺陷", "prompt": "审核", "constraints": [], "evidence_refs": ["E-1"], "candidate": "x"},
            ensure_ascii=False,
        )
        answer = json.dumps(
            {"reviewer_id": "reviewer-1", "round_number": 1, "verdict": "WARN",
             "findings": [], "isolated": True,
             "context_fingerprint": "a" * 64}
        )
        transcript = (
            "OpenAI Codex v0.154.0\n--------\nuser\n"
            + "# review\n```json\n" + echoed_context + "\n```\n"
            'Required shape: {"reviewer_id": "<id>", "verdict": "PASS|WARN|FAIL"}\n'
            "codex\n" + answer + "\ntokens used\n4,858\n" + answer
        )
        payload = extract_result_payload(transcript)
        assert payload["reviewer_id"] == "reviewer-1"
        assert payload["verdict"] == "WARN"
        assert payload["context_fingerprint"] == "a" * 64

    def test_deeply_nested_envelope_raises_cleanly(self):
        payload: dict = {"verdict": "PASS"}
        for _ in range(40):
            payload = {"result": payload}
        with pytest.raises(AgentRuntimeError, match="nest"):
            extract_result_payload(json.dumps(payload))

    def test_empty_stdout_raises(self):
        with pytest.raises(AgentRuntimeError):
            extract_result_payload("   \n")

    def test_no_json_raises(self):
        with pytest.raises(AgentRuntimeError):
            extract_result_payload("the agent replied in prose only")


class TestDispatchConfigValidation:
    def test_profile_rejects_platform_and_command(self):
        with pytest.raises(AgentRuntimeError):
            ProfileBinding(
                profile_id="x",
                roles=(AgentRole.REVIEWER,),
                display_name="x",
                platform="claude",
                command=("bridge",),
            )

    def test_unknown_platform_in_profile(self):
        with pytest.raises(AgentRuntimeError, match="unknown platform"):
            PlatformDispatchConfig.from_dict(
                {
                    "profiles": {
                        "r": {"platform": "no-such-platform", "roles": ["reviewer"]}
                    }
                }
            )

    def test_unknown_default_platform(self):
        with pytest.raises(AgentRuntimeError, match="unknown platform"):
            PlatformDispatchConfig.from_dict(
                {
                    "platform": "ghost",
                    "profiles": {"r": {"platform": "claude", "roles": ["reviewer"]}},
                }
            )

    def test_role_binding_to_unknown_profile(self):
        with pytest.raises(AgentRuntimeError, match="unknown profile"):
            PlatformDispatchConfig.from_dict(
                {
                    "profiles": {"r": {"platform": "claude", "roles": ["reviewer"]}},
                    "role_bindings": {"reviewer": "ghost"},
                }
            )

    def test_platform_argv_placeholder_must_be_unique(self):
        with pytest.raises(AgentRuntimeError, match="placeholder"):
            ProfileBinding(
                profile_id="x",
                roles=(AgentRole.REVIEWER,),
                display_name="x",
                argv=("tool", "{prompt}", "{prompt}"),
            )


class TestPlatformReviewCycle:
    def test_full_cycle_across_platforms(self, tmp_path, context: ReviewContext):
        adapter = adapter_from_config(_dispatch_config(tmp_path))
        result = run_review_cycle_with_adapter(context, ReviewRisk.HIGH, adapter)
        assert result.outcome.value == "PASS"
        usage = adapter.usage_report()
        platforms = {item["agent_id"]: item["platform"] for item in usage}
        assert platforms["reviewer-1"] == "fake-envelope"
        assert platforms["reviewer-2"] == "fake-noisy"
        assert platforms["arbiter"] == "fake-clean"
        assert {item["profile_id"] for item in usage} == {
            "euclid",
            "volt",
            "judge",
            "watcher",
        }
        reviewer_profiles = [
            item["profile_id"] for item in usage if item["role"] == "reviewer"
        ]
        assert len(set(reviewer_profiles)) == 2

    def test_stdin_platform_mode(self, tmp_path, context: ReviewContext):
        reviewer = _write_script(tmp_path, "reviewer.py", FAKE_AGENT)
        config = {
            "platforms": {
                "stdin-cli": {"argv": [sys.executable, reviewer, "clean"]}
            },
            "profiles": {
                "r1": {"platform": "stdin-cli", "roles": ["reviewer"]},
                "r2": {"platform": "stdin-cli", "roles": ["reviewer"]},
                "a1": {"platform": "stdin-cli", "roles": ["arbiter"]},
                "m1": {"platform": "stdin-cli", "roles": ["meta_reviewer"]},
            },
        }
        adapter = adapter_from_config(config)
        result = run_review_cycle_with_adapter(context, ReviewRisk.MEDIUM, adapter)
        assert result.outcome.value == "PASS"

    def test_bridge_command_mode_still_supported(self, tmp_path, context: ReviewContext):
        meta = _write_script(tmp_path, "meta_bridge.py", FAKE_META_BRIDGE)
        config = _dispatch_config(tmp_path)
        config["profiles"]["watcher"] = {
            "command": [sys.executable, meta],
            "roles": ["meta_reviewer"],
        }
        adapter = adapter_from_config(config)
        result = run_review_cycle_with_adapter(context, ReviewRisk.HIGH, adapter)
        assert result.outcome.value == "PASS"
        usage = {item["agent_id"]: item for item in adapter.usage_report()}
        assert usage["meta-reviewer"]["platform"] == "stdio-bridge"

    def test_dispute_rounds_converge_on_second_round(self, tmp_path, context: ReviewContext):
        config = _dispatch_config(tmp_path)
        config["profiles"]["judge"]["platform"] = "fake-arbiter"
        config["platforms"]["fake-arbiter"] = {
            "argv": [
                sys.executable,
                _write_script(tmp_path, "arbiter.py", FAKE_ARBITER),
                "clean",
                "-p",
                "{prompt}",
            ]
        }
        adapter = adapter_from_config(config)
        result = run_review_cycle_with_adapter(context, ReviewRisk.HIGH, adapter)
        assert result.outcome.value == "PASS"
        assert result.round_number == 2
        rounds = sorted({item["round_number"] for item in adapter.usage_report()})
        assert rounds == [1, 2]

    def test_profile_isolation_is_enforced(self, tmp_path, context: ReviewContext):
        config = _dispatch_config(tmp_path)
        config["profiles"]["volt"]["roles"] = ["arbiter", "meta_reviewer"]
        adapter = adapter_from_config(config)
        with pytest.raises(AgentRuntimeError):
            run_review_cycle_with_adapter(context, ReviewRisk.CRITICAL, adapter)

    def test_failing_platform_command_raises(self, tmp_path, context: ReviewContext):
        config = {
            "profiles": {
                "broken": {
                    "platform": "fake-clean",
                    "argv": [sys.executable, "-c", "import sys; sys.exit(3)"],
                    "roles": ["reviewer"],
                },
                "healthy": {"platform": "fake-clean", "roles": ["reviewer"]},
                "a1": {"platform": "fake-clean", "roles": ["arbiter"]},
                "m1": {"platform": "fake-clean", "roles": ["meta_reviewer"]},
            },
            "platforms": {},
        }
        config["platforms"]["fake-clean"] = {
            "argv": [
                sys.executable,
                _write_script(tmp_path, "reviewer.py", FAKE_AGENT),
                "clean",
                "-p",
                "{prompt}",
            ]
        }
        adapter = adapter_from_config(config)
        with pytest.raises(AgentRuntimeError, match="exited with code 3"):
            run_review_cycle_with_adapter(context, ReviewRisk.MEDIUM, adapter)


    def test_platform_env_reaches_subprocess(self, tmp_path, context: ReviewContext):
        env_agent = _write_script(
            tmp_path,
            "env_agent.py",
            "import json, os, re, sys\n"
            "prompt = sys.argv[sys.argv.index('-p') + 1]\n"
            "fingerprint = re.search(r'context_fingerprint=\"([0-9a-f]{64})\"', prompt).group(1)\n"
            "reviewer_ids = sorted(set(re.findall(r'\"reviewer_id\": \"(reviewer-\\d+)\"', prompt)))\n"
            "print(json.dumps({'arbiter_id': 'arbiter', 'round_number': 1, 'verdict': 'PASS',\n"
            "  'summary': os.environ.get('GA_TEST_VAR', 'missing'), 'rationale': 'r',\n"
            "  'reviewer_ids': reviewer_ids, 'context_fingerprint': fingerprint,\n"
            "  'disagrees_with_reviewers': False}))\n",
        )
        plain = _write_script(tmp_path, "plain_agent.py", FAKE_AGENT)
        config = {
            "platforms": {
                "env-cli": {
                    "argv": [sys.executable, env_agent, "-p", "{prompt}"],
                    "env": {"GA_TEST_VAR": "platform-value"},
                },
                "plain-cli": {"argv": [sys.executable, plain, "clean", "-p", "{prompt}"]},
            },
            "profiles": {
                "r1": {"platform": "plain-cli", "roles": ["reviewer"]},
                "r2": {"platform": "plain-cli", "roles": ["reviewer"]},
                "a1": {"platform": "env-cli", "roles": ["arbiter"]},
                "m1": {"platform": "plain-cli", "roles": ["meta_reviewer"]},
            },
        }
        adapter = adapter_from_config(config)
        result = run_review_cycle_with_adapter(context, ReviewRisk.MEDIUM, adapter)
        assert result.outcome.value == "PASS"
        assert result.arbiter_result.summary == "platform-value"

    def test_malformed_result_raises_clean_runtime_error(self, tmp_path, context: ReviewContext):
        garbage = _write_script(
            tmp_path,
            "garbage.py",
            "import sys\nprint('{\"type\": \"result\", \"unrelated\": true}')\n",
        )
        plain = _write_script(tmp_path, "plain_agent.py", FAKE_AGENT)
        config = {
            "platforms": {
                "garbage-cli": {"argv": [sys.executable, garbage]},
                "plain-cli": {"argv": [sys.executable, plain, "clean", "-p", "{prompt}"]},
            },
            "profiles": {
                "bad": {"platform": "garbage-cli", "roles": ["reviewer"]},
                "good": {"platform": "plain-cli", "roles": ["reviewer"]},
                "a1": {"platform": "plain-cli", "roles": ["arbiter"]},
                "m1": {"platform": "plain-cli", "roles": ["meta_reviewer"]},
            },
        }
        adapter = adapter_from_config(config)
        with pytest.raises(AgentRuntimeError, match="invalid result object"):
            run_review_cycle_with_adapter(context, ReviewRisk.MEDIUM, adapter)

    def test_hanging_platform_times_out_cleanly(self, tmp_path, context: ReviewContext):
        config = {
            "platforms": {
                "slow": {"argv": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout_seconds": 2}
            },
            "profiles": {
                "slow-reviewer": {"platform": "slow", "roles": ["reviewer"]},
                "a1": {"platform": "slow", "roles": ["arbiter"]},
                "m1": {"platform": "slow", "roles": ["meta_reviewer"]},
            },
        }
        adapter = adapter_from_config(config)
        with pytest.raises(AgentRuntimeError, match="timed out"):
            run_review_cycle_with_adapter(context, ReviewRisk.MEDIUM, adapter)

    def test_transient_failure_is_retried(self, tmp_path, context, monkeypatch):
        import goalanchor.platforms as platforms_module

        calls: list[str] = []

        def fake_run(argv, *, input_text, timeout_seconds, env):
            calls.append(argv[0])
            if len(calls) == 1:
                raise AgentRuntimeError("agent platform command exited with code 1")
            marker = input_text if input_text else " ".join(argv)
            fingerprint = re.search(
                r'context_fingerprint="([0-9a-f]{64})"', marker
            ).group(1)
            agent_id = re.search(r'agent_id="([^"]+)"', marker).group(1)
            round_number = int(re.search(r"round (\d+)", marker).group(1))
            if "# GoalAnchor reviewer review" in marker:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": round_number,
                    "verdict": "PASS",
                    "findings": [],
                    "isolated": True,
                    "context_fingerprint": fingerprint,
                }
            elif "# GoalAnchor arbiter review" in marker:
                payload = {
                    "arbiter_id": agent_id,
                    "round_number": round_number,
                    "verdict": "PASS",
                    "summary": "ok",
                    "rationale": "r",
                    "reviewer_ids": ["reviewer-1", "reviewer-2"],
                    "context_fingerprint": fingerprint,
                    "disagrees_with_reviewers": False,
                }
            else:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": round_number,
                    "arbiter_id": "arbiter",
                    "verdict": "PASS",
                    "summary": "ok",
                    "findings": [],
                    "context_fingerprint": fingerprint,
                    "agrees_with_arbiter": True,
                    "isolated": True,
                }
            return json.dumps(payload), "tokens used\n12,345"

        monkeypatch.setattr(platforms_module, "_run_subprocess", fake_run)
        config = {
            "platforms": {"flaky-cli": {"argv": ["tool", "-p", "{prompt}"], "retries": 2}},
            "profiles": {
                "r1": {"platform": "flaky-cli", "roles": ["reviewer"]},
                "r2": {"platform": "flaky-cli", "roles": ["reviewer"]},
                "a1": {"platform": "flaky-cli", "roles": ["arbiter"]},
                "m1": {"platform": "flaky-cli", "roles": ["meta_reviewer"]},
            },
        }
        adapter = adapter_from_config(config)
        result = run_review_cycle_with_adapter(context, ReviewRisk.MEDIUM, adapter)
        assert result.outcome.value == "PASS"
        attempts = {item["agent_id"]: item["attempts"] for item in adapter.usage_report()}
        assert attempts["reviewer-1"] == 2
        assert attempts["reviewer-2"] == 1

    def test_retry_exhaustion_raises_last_error(self, tmp_path, context, monkeypatch):
        import goalanchor.platforms as platforms_module

        def always_fail(argv, *, input_text, timeout_seconds, env):
            raise AgentRuntimeError("agent platform command exited with code 7")

        monkeypatch.setattr(platforms_module, "_run_subprocess", always_fail)
        config = {
            "platforms": {"flaky-cli": {"argv": ["tool"], "retries": 3}},
            "profiles": {
                "r1": {"platform": "flaky-cli", "roles": ["reviewer"]},
                "r2": {"platform": "flaky-cli", "roles": ["reviewer"]},
                "a1": {"platform": "flaky-cli", "roles": ["arbiter"]},
                "m1": {"platform": "flaky-cli", "roles": ["meta_reviewer"]},
            },
        }
        adapter = adapter_from_config(config)
        with pytest.raises(AgentRuntimeError, match="code 7"):
            run_review_cycle_with_adapter(context, ReviewRisk.MEDIUM, adapter)

    def test_dry_run_renders_without_invoking(self, tmp_path, context, capsys):
        from goalanchor import compat

        request_path = tmp_path / "request.json"
        config_path = tmp_path / "config.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH"}), encoding="utf-8"
        )
        config_path.write_text(json.dumps(_dispatch_config(tmp_path)), encoding="utf-8")
        exit_code = compat.main(
            [
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(config_path),
                "--dry-run",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert out["dry_run"] is True
        assert out["required_reviewers"] == 2
        assert set(out["reviewer_prompts"]) == {"reviewer-1", "reviewer-2"}
        assert context.fingerprint in out["reviewer_prompts"]["reviewer-1"]

    def test_platforms_list_reports_environment(self, capsys):
        from goalanchor import compat

        exit_code = compat.main(["platforms", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        by_name = {item["platform"]: item for item in out["builtin_platforms"]}
        assert by_name["codex"]["prompt_mode"] == "stdin"
        assert isinstance(by_name["codex"]["available"], bool)

    def test_review_run_alias_and_dry_run(self, tmp_path, context, capsys):
        from goalanchor import compat

        request_path = tmp_path / "request.json"
        config_path = tmp_path / "config.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH"}), encoding="utf-8"
        )
        config_path.write_text(json.dumps(_dispatch_config(tmp_path)), encoding="utf-8")
        exit_code = compat.main(
            [
                "review-run",
                "--request",
                str(request_path),
                "--config",
                str(config_path),
                "--dry-run",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert out["dry_run"] is True

    def test_bindings_carried_in_library_result(self, tmp_path, context: ReviewContext):
        adapter = adapter_from_config(_dispatch_config(tmp_path))
        result = run_review_cycle_with_adapter(context, ReviewRisk.HIGH, adapter)
        assert len(result.agent_bindings) == 4
        assert {item["platform"] for item in result.agent_bindings} == {
            "fake-envelope",
            "fake-noisy",
            "fake-clean",
        }
        assert "agent_bindings" in result.to_dict()

    def test_critical_requires_signed_risk_declaration(self, tmp_path, context, capsys, monkeypatch):
        import goalanchor.platforms as platforms_module
        from goalanchor import compat

        def fail_if_invoked(argv, *, input_text, timeout_seconds, env):
            raise AssertionError("must not invoke any agent without a declaration")

        monkeypatch.setattr(platforms_module, "_run_subprocess", fail_if_invoked)
        request_path = tmp_path / "request.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "CRITICAL"}), encoding="utf-8"
        )
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(_dispatch_config(tmp_path)), encoding="utf-8")
        exit_code = compat.main(
            [
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(config_path),
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 2
        assert "risk_declaration" in err

    def test_critical_with_declaration_runs_and_echoes(self, tmp_path, context, capsys, monkeypatch):
        import goalanchor.platforms as platforms_module
        from goalanchor import compat

        def fake_run(argv, *, input_text, timeout_seconds, env):
            marker = input_text if input_text else " ".join(argv)
            fingerprint = re.search(
                r'context_fingerprint="([0-9a-f]{64})"', marker
            ).group(1)
            agent_id = re.search(r'agent_id="([^"]+)"', marker).group(1)
            round_number = int(re.search(r"round (\d+)", marker).group(1))
            if "# GoalAnchor reviewer review" in marker:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": round_number,
                    "verdict": "PASS",
                    "findings": [],
                    "isolated": True,
                    "context_fingerprint": fingerprint,
                }
            elif "# GoalAnchor arbiter review" in marker:
                payload = {
                    "arbiter_id": agent_id,
                    "round_number": round_number,
                    "verdict": "PASS",
                    "summary": "ok",
                    "rationale": "r",
                    "reviewer_ids": ["reviewer-1", "reviewer-2", "reviewer-3"],
                    "context_fingerprint": fingerprint,
                    "disagrees_with_reviewers": False,
                }
            else:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": round_number,
                    "arbiter_id": "arbiter",
                    "verdict": "PASS",
                    "summary": "ok",
                    "findings": [],
                    "context_fingerprint": fingerprint,
                    "agrees_with_arbiter": True,
                    "isolated": True,
                }
            return json.dumps(payload), ""

        monkeypatch.setattr(platforms_module, "_run_subprocess", fake_run)
        request_path = tmp_path / "request.json"
        request_path.write_text(
            json.dumps(
                {
                    "context": context.to_dict(),
                    "risk": "CRITICAL",
                    "risk_declaration": {
                        "declared_by": "user",
                        "reason": "缺陷影响生产登录主路径",
                    },
                }
            ),
            encoding="utf-8",
        )
        config = {
            "platforms": {"tool": {"argv": ["tool", "-p", "{prompt}"]}},
            "profiles": {
                "r1": {"platform": "tool", "roles": ["reviewer"]},
                "r2": {"platform": "tool", "roles": ["reviewer"]},
                "r3": {"platform": "tool", "roles": ["reviewer"]},
                "a1": {"platform": "tool", "roles": ["arbiter"]},
                "m1": {"platform": "tool", "roles": ["meta_reviewer"]},
            },
            "role_bindings": {"arbiter": "a1", "meta_reviewer": "m1"},
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        exit_code = compat.main(
            [
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(config_path),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert out["outcome"] == "PASS"
        assert out["risk_declaration"]["declared_by"] == "user"
        assert len(out["agent_bindings"]) == 5

    def test_empty_stdout_is_retried(self, tmp_path, context, monkeypatch):
        import goalanchor.platforms as platforms_module

        calls: list[int] = []

        def fake_run(argv, *, input_text, timeout_seconds, env):
            calls.append(1)
            if len(calls) == 1:
                raise AgentRuntimeError("agent platform returned empty stdout")
            marker = input_text if input_text else " ".join(argv)
            fingerprint = re.search(
                r'context_fingerprint="([0-9a-f]{64})"', marker
            ).group(1)
            agent_id = re.search(r'agent_id="([^"]+)"', marker).group(1)
            if "# GoalAnchor reviewer review" in marker:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": 1,
                    "verdict": "PASS",
                    "findings": [],
                    "isolated": True,
                    "context_fingerprint": fingerprint,
                }
            elif "# GoalAnchor arbiter review" in marker:
                payload = {
                    "arbiter_id": agent_id,
                    "round_number": 1,
                    "verdict": "PASS",
                    "summary": "ok",
                    "rationale": "r",
                    "reviewer_ids": ["reviewer-1"],
                    "context_fingerprint": fingerprint,
                    "disagrees_with_reviewers": False,
                }
            else:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": 1,
                    "arbiter_id": "arbiter",
                    "verdict": "PASS",
                    "summary": "ok",
                    "findings": [],
                    "context_fingerprint": fingerprint,
                    "agrees_with_arbiter": True,
                    "isolated": True,
                }
            return json.dumps(payload), ""

        monkeypatch.setattr(platforms_module, "_run_subprocess", fake_run)
        config = {
            "platforms": {"tool": {"argv": ["tool"], "retries": 2}},
            "profiles": {
                "r1": {"platform": "tool", "roles": ["reviewer"]},
                "a1": {"platform": "tool", "roles": ["arbiter"]},
                "m1": {"platform": "tool", "roles": ["meta_reviewer"]},
            },
        }
        adapter = adapter_from_config(config)
        result = run_review_cycle_with_adapter(context, ReviewRisk.LOW, adapter)
        assert result.outcome.value == "PASS"
        attempts = {item["agent_id"]: item["attempts"] for item in adapter.usage_report()}
        assert attempts["reviewer-1"] == 2

    def test_cli_reports_usage_summary(self, tmp_path, context, capsys):
        from goalanchor import compat

        request_path = tmp_path / "request.json"
        config_path = tmp_path / "config.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH"}), encoding="utf-8"
        )
        config_path.write_text(json.dumps(_dispatch_config(tmp_path)), encoding="utf-8")
        exit_code = compat.main(
            [
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(config_path),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert out["usage_summary"]["calls"] == 4
        assert out["usage_summary"]["tokens_used_total"] == 0

    def test_platforms_probe_reports_available_platforms(self, capsys, monkeypatch):
        import goalanchor.platforms as platforms_module
        from goalanchor import compat

        monkeypatch.setattr(
            platforms_module,
            "_run_subprocess",
            lambda argv, **kwargs: ('{"probe": "ok"}', ""),
        )
        exit_code = compat.main(["platforms", "--probe", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        by_name = {item["platform"]: item for item in out["builtin_platforms"]}
        assert by_name["codex"]["probe"] == "ok"
        assert by_name["gemini"]["probe"] == "not-installed"

    def test_platforms_list_with_config_profiles(self, tmp_path, capsys):
        from goalanchor import compat

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(_dispatch_config(tmp_path)), encoding="utf-8")
        exit_code = compat.main(["platforms", "--config", str(config_path), "--json"])
        out = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert out["reviewer_profile_count"] == 2
        assert out["max_supported_risk"] == "HIGH"
        assert {item["profile_id"] for item in out["profiles"]} == {
            "euclid",
            "volt",
            "judge",
            "watcher",
        }

    def test_non_finite_timeout_rejected(self):
        with pytest.raises(AgentRuntimeError, match="finite"):
            ProfileBinding(
                profile_id="x",
                roles=(AgentRole.REVIEWER,),
                display_name="x",
                platform="claude",
                timeout_seconds=float("inf"),
            )


    def test_chinese_prompt_language(self, context: ReviewContext):
        request = AgentRequest(
            role=AgentRole.REVIEWER,
            agent_id="reviewer-1",
            round_number=1,
            risk=ReviewRisk.HIGH,
            context=context,
        )
        prompt = render_agent_prompt(request, language="zh")
        assert "独立评审" in prompt
        assert "只依据下方材料独立判断" in prompt
        assert 'agent_id="reviewer-1"' in prompt
        assert context.fingerprint in prompt
        assert f"# GoalAnchor reviewer review (round 1)" in prompt
        with pytest.raises(AgentRuntimeError, match="language"):
            render_agent_prompt(request, language="fr")

    def test_config_language_reaches_dispatch(self, tmp_path, context, monkeypatch):
        import goalanchor.platforms as platforms_module

        captured: list[str] = []

        def fake_run(argv, *, input_text, timeout_seconds, env):
            marker = input_text if input_text else " ".join(argv)
            captured.append(marker)
            fingerprint = re.search(
                r'context_fingerprint="([0-9a-f]{64})"', marker
            ).group(1)
            agent_id = re.search(r'agent_id="([^"]+)"', marker).group(1)
            if "# GoalAnchor reviewer review" in marker:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": 1,
                    "verdict": "PASS",
                    "findings": [],
                    "isolated": True,
                    "context_fingerprint": fingerprint,
                }
            elif "# GoalAnchor arbiter review" in marker:
                payload = {
                    "arbiter_id": agent_id,
                    "round_number": 1,
                    "verdict": "PASS",
                    "summary": "ok",
                    "rationale": "r",
                    "reviewer_ids": ["reviewer-1"],
                    "context_fingerprint": fingerprint,
                    "disagrees_with_reviewers": False,
                }
            else:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": 1,
                    "arbiter_id": "arbiter",
                    "verdict": "PASS",
                    "summary": "ok",
                    "findings": [],
                    "context_fingerprint": fingerprint,
                    "agrees_with_arbiter": True,
                    "isolated": True,
                }
            return json.dumps(payload), ""

        monkeypatch.setattr(platforms_module, "_run_subprocess", fake_run)
        config = {
            "language": "zh",
            "platforms": {"tool": {"argv": ["tool"]}},
            "profiles": {
                "r1": {"platform": "tool", "roles": ["reviewer"]},
                "a1": {"platform": "tool", "roles": ["arbiter"]},
                "m1": {"platform": "tool", "roles": ["meta_reviewer"]},
            },
        }
        adapter = adapter_from_config(config)
        result = run_review_cycle_with_adapter(context, ReviewRisk.LOW, adapter)
        assert result.outcome.value == "PASS"
        assert "独立评审" in captured[0]

    def test_tokens_used_recorded_from_stderr(self, tmp_path, context, monkeypatch):
        import goalanchor.platforms as platforms_module

        def fake_run(argv, *, input_text, timeout_seconds, env):
            marker = input_text if input_text else " ".join(argv)
            fingerprint = re.search(
                r'context_fingerprint="([0-9a-f]{64})"', marker
            ).group(1)
            agent_id = re.search(r'agent_id="([^"]+)"', marker).group(1)
            if "# GoalAnchor reviewer review" in marker:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": 1,
                    "verdict": "PASS",
                    "findings": [],
                    "isolated": True,
                    "context_fingerprint": fingerprint,
                }
            elif "# GoalAnchor arbiter review" in marker:
                payload = {
                    "arbiter_id": agent_id,
                    "round_number": 1,
                    "verdict": "PASS",
                    "summary": "ok",
                    "rationale": "r",
                    "reviewer_ids": ["reviewer-1"],
                    "context_fingerprint": fingerprint,
                    "disagrees_with_reviewers": False,
                }
            else:
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": 1,
                    "arbiter_id": "arbiter",
                    "verdict": "PASS",
                    "summary": "ok",
                    "findings": [],
                    "context_fingerprint": fingerprint,
                    "agrees_with_arbiter": True,
                    "isolated": True,
                }
            return json.dumps(payload), "tokens used\n12,345"

        monkeypatch.setattr(platforms_module, "_run_subprocess", fake_run)
        config = {
            "platforms": {"tool": {"argv": ["tool"]}},
            "profiles": {
                "r1": {"platform": "tool", "roles": ["reviewer"]},
                "a1": {"platform": "tool", "roles": ["arbiter"]},
                "m1": {"platform": "tool", "roles": ["meta_reviewer"]},
            },
        }
        adapter = adapter_from_config(config)
        run_review_cycle_with_adapter(context, ReviewRisk.LOW, adapter)
        usage = {item["agent_id"]: item for item in adapter.usage_report()}
        assert usage["reviewer-1"]["tokens_used"] == 12345
        assert usage["arbiter"]["tokens_used"] == 12345


class TestReviewPromptCli:
    """The host-native (visible) dispatch path: render prompts, validate rounds."""

    def _request_file(self, tmp_path: Path, context: ReviewContext) -> Path:
        path = tmp_path / "request.json"
        path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH"}), encoding="utf-8"
        )
        return path

    def _run(self, capsys, *argv: str) -> int:
        from goalanchor import compat

        exit_code = compat.main(["review-prompt", *argv])
        return exit_code

    def test_reviewer_prompt(self, tmp_path, context, capsys):
        request_path = self._request_file(tmp_path, context)
        exit_code = self._run(
            capsys,
            "--request",
            str(request_path),
            "--role",
            "reviewer",
            "--agent-id",
            "reviewer-2",
            "--round",
            "1",
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert 'agent_id="reviewer-2"' in out
        assert context.fingerprint in out
        assert "independent reviewers running in parallel" in out

    def test_arbiter_prompt_requires_reviewer_results(self, tmp_path, context, capsys):
        request_path = self._request_file(tmp_path, context)
        exit_code = self._run(
            capsys, "--request", str(request_path), "--role", "arbiter"
        )
        assert exit_code == 2
        assert "reviewer-results" in capsys.readouterr().err

    def test_arbiter_prompt_includes_results(self, tmp_path, context, capsys):
        request_path = self._request_file(tmp_path, context)
        results_path = tmp_path / "results.json"
        results_path.write_text(
            json.dumps(
                [
                    {
                        "reviewer_id": "reviewer-1",
                        "round_number": 1,
                        "verdict": "PASS",
                        "findings": [],
                        "isolated": True,
                        "context_fingerprint": context.fingerprint,
                    }
                ]
            ),
            encoding="utf-8",
        )
        exit_code = self._run(
            capsys,
            "--request",
            str(request_path),
            "--role",
            "arbiter",
            "--reviewer-results",
            str(results_path),
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert '"reviewer_id": "reviewer-1"' in out

    def test_meta_prompt_includes_arbiter(self, tmp_path, context, capsys):
        request_path = self._request_file(tmp_path, context)
        arbiter_path = tmp_path / "arbiter.json"
        arbiter_path.write_text(
            json.dumps(
                {
                    "arbiter_id": "arbiter",
                    "round_number": 1,
                    "verdict": "PASS",
                    "summary": "s",
                    "rationale": "r",
                    "reviewer_ids": ["reviewer-1"],
                    "context_fingerprint": context.fingerprint,
                    "disagrees_with_reviewers": False,
                }
            ),
            encoding="utf-8",
        )
        exit_code = self._run(
            capsys,
            "--request",
            str(request_path),
            "--role",
            "meta_reviewer",
            "--arbiter-result",
            str(arbiter_path),
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "agrees_with_arbiter" in out
        assert "Arbiter result under audit" in out

    def test_meta_prompt_can_include_reviewer_results(self, tmp_path, context, capsys):
        request_path = tmp_path / "request.json"
        results_path = tmp_path / "results.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH"}), encoding="utf-8"
        )
        results_path.write_text(
            json.dumps(
                [
                    {
                        "reviewer_id": "reviewer-1",
                        "round_number": 1,
                        "verdict": "WARN",
                        "findings": [
                            {
                                "finding_id": "F-9",
                                "issue": "证据缺口",
                                "evidence_refs": ["E-1"],
                                "decision_impact": "阻断",
                                "remaining_uncertainty": "未知",
                            }
                        ],
                        "isolated": True,
                        "context_fingerprint": context.fingerprint,
                    }
                ]
            ),
            encoding="utf-8",
        )
        arbiter_path = tmp_path / "arbiter.json"
        arbiter_path.write_text(
            json.dumps(
                {
                    "arbiter_id": "arbiter",
                    "round_number": 1,
                    "verdict": "PASS",
                    "summary": "认为可以收敛",
                    "rationale": "r",
                    "reviewer_ids": ["reviewer-1"],
                    "context_fingerprint": context.fingerprint,
                    "disagrees_with_reviewers": False,
                }
            ),
            encoding="utf-8",
        )
        exit_code = self._run(
            capsys,
            "--request",
            str(request_path),
            "--role",
            "meta_reviewer",
            "--arbiter-result",
            str(arbiter_path),
            "--reviewer-results",
            str(results_path),
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Reviewer results for this round" in out
        assert "F-9" in out

    def test_dispute_round_prompt(self, tmp_path, context, capsys):
        request_path = self._request_file(tmp_path, context)
        dispute_path = tmp_path / "dispute.json"
        dispute_path.write_text(
            json.dumps(
                {
                    "context_fingerprint": context.fingerprint,
                    "round_number": 1,
                    "arbiter_summary": "as",
                    "arbiter_rationale": "ar",
                    "reviewer_findings": [],
                    "re_review_prompt": "re-review independently",
                }
            ),
            encoding="utf-8",
        )
        exit_code = self._run(
            capsys,
            "--request",
            str(request_path),
            "--role",
            "reviewer",
            "--round",
            "2",
            "--dispute",
            str(dispute_path),
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "DisputePacket" in out
        assert "(round 2)" in out


class TestLegacyCompatibility:
    def test_legacy_bridge_config_unchanged(self):
        config = {
            "role_bindings": {"reviewer": "euclid"},
            "profiles": {
                "euclid": {
                    "display_name": "Euclid",
                    "roles": ["reviewer"],
                    "command": ["goalanchor-host-bridge", "--profile", "euclid"],
                }
            },
        }
        adapter = adapter_from_config(config)
        assert isinstance(adapter, JsonStdioAgentAdapter)

    def test_platform_config_is_detected(self, tmp_path):
        adapter = adapter_from_config(_dispatch_config(tmp_path))
        assert hasattr(adapter, "usage_report")


class TestCliReviewCycle:
    def test_cli_runs_platform_dispatch(self, tmp_path, context, capsys, monkeypatch):
        from goalanchor import compat

        request_path = tmp_path / "request.json"
        config_path = tmp_path / "config.json"
        request_path.write_text(
            json.dumps(
                {
                    "context": context.to_dict(),
                    "risk": "HIGH",
                    "max_rounds": 2,
                }
            ),
            encoding="utf-8",
        )
        config_path.write_text(
            json.dumps(_dispatch_config(tmp_path)), encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        exit_code = compat.main(
            [
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(config_path),
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["outcome"] == "PASS"
        assert exit_code == 0
        bindings = payload["agent_bindings"]
        assert len(bindings) == 4
        assert {item["platform"] for item in bindings} == {
            "fake-envelope",
            "fake-noisy",
            "fake-clean",
        }
        by_agent = {item["agent_id"]: item["profile_id"] for item in bindings}
        assert by_agent["reviewer-1"] == "euclid"
        assert by_agent["reviewer-2"] == "volt"

    def test_cli_rejects_invalid_config(self, tmp_path, context, capsys):
        from goalanchor import compat

        request_path = tmp_path / "request.json"
        config_path = tmp_path / "config.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH"}),
            encoding="utf-8",
        )
        config_path.write_text(
            json.dumps({"profiles": {"r": {"platform": "missing", "roles": ["reviewer"]}}}),
            encoding="utf-8",
        )
        exit_code = compat.main(
            [
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(config_path),
            ]
        )
        assert exit_code == 2
        assert "goalanchor:" in capsys.readouterr().err

    def test_gate_malformed_arbiter_raises_cleanly(self, tmp_path, context, capsys):
        from goalanchor import compat

        record_path = tmp_path / "cycle.json"
        record = {
            "context": context.to_dict(),
            "risk": "HIGH",
            "reviewer_results": [
                {
                    "reviewer_id": "reviewer-1",
                    "round_number": 1,
                    "verdict": "PASS",
                    "findings": [],
                    "isolated": True,
                    "context_fingerprint": context.fingerprint,
                },
                {
                    "reviewer_id": "reviewer-2",
                    "round_number": 1,
                    "verdict": "PASS",
                    "findings": [],
                    "isolated": True,
                    "context_fingerprint": context.fingerprint,
                },
            ],
            "arbiter": {
                "arbiter_id": "arbiter",
                "round_number": 1,
                "verdict": "PASS",
                "reviewer_ids": ["reviewer-1", "reviewer-2"],
                "context_fingerprint": context.fingerprint,
                "disagrees_with_reviewers": False,
                "unexpected_extra_field": True,
            },
            "round_number": 1,
            "max_rounds": 2,
        }
        record_path.write_text(json.dumps(record), encoding="utf-8")
        exit_code = compat.main(
            ["gate", "--phase", "review-cycle", "--record", str(record_path)]
        )
        err = capsys.readouterr().err
        assert exit_code == 2
        assert "does not match the schema" in err
