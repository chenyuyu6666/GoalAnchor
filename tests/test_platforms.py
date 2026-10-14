"""Tests for the built-in multi-platform Agent dispatch adapters.

Test-class index (in file order):

- ``TestRenderAgentPrompt``     prompt rendering per role/language
- ``TestExtractResultPayload``  platform output envelope parsing
- ``TestDispatchConfigValidation``  config rejection rules
- ``TestPlatformReviewCycle``   full review-cycle dispatch (largest class)
- ``TestReviewPromptCli``       ``review-prompt`` CLI
- ``TestLegacyCompatibility``  legacy stdio-bridge configs
- ``TestCliReviewCycle``        ``review-cycle`` CLI end to end
- ``TestModelConsistency``      model pinning/warnings behavior
"""

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
    DisputePacket,
    JsonStdioAgentAdapter,
    ReviewRisk,
    run_review_cycle_with_adapter,
)
from goalanchor.integration import ReviewFinding
from goalanchor.platforms import (
    PlatformAgentRouter,
    PlatformDispatchConfig,
    ProfileBinding,
    adapter_from_config,
    extract_result_payload,
    render_agent_prompt,
)
from goalanchor.review_service import ReviewContext, ReviewerResult

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
    _write_script(tmp_path, "meta_bridge.py", FAKE_META_BRIDGE)
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
            "shutil.which", lambda name: f"/fake/{name}" if name == "codex" else None
        )
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

    def test_platforms_probe_uses_config_model(self, tmp_path, capsys, monkeypatch):
        from goalanchor import compat

        calls: list = []
        monkeypatch.setattr("shutil.which", lambda name: f"/fake/{name}")
        monkeypatch.setattr(
            "goalanchor.platforms.probe_platform",
            lambda name, timeout_seconds=120.0, model=None: (
                calls.append((name, model)) or {"platform": name, "probe": "ok"}
            ),
        )
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "model": "main-model",
                    "model_divergence": {
                        "confirmed_by": "user",
                        "reason": "test fixture: platform-level model differs on purpose",
                    },
                    "platforms": {"claude": {"model": "platform-claude"}},
                    "profiles": {
                        "r": {"platform": "claude", "roles": ["reviewer"]},
                        "c": {
                            "platform": "codex",
                            "roles": ["reviewer"],
                            "model": "profile-codex",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        exit_code = compat.main(["platforms", "--config", str(config_path), "--probe"])
        assert exit_code == 0
        by_name = dict(calls)
        # each probe uses the model the dispatch would pin for that platform:
        # platform-level first, then a profile-level pin, then the top-level
        # main model
        assert by_name["claude"] == "platform-claude"
        assert by_name["codex"] == "profile-codex"
        assert by_name["gemini"] == "main-model"
        assert set(by_name) == {"codex", "claude", "gemini", "opencode"}

    def test_platforms_invalid_config_fails_before_probing(
        self, tmp_path, capsys, monkeypatch
    ):
        from goalanchor import compat

        probed: list = []
        monkeypatch.setattr("shutil.which", lambda name: f"/fake/{name}")
        monkeypatch.setattr(
            "goalanchor.platforms.probe_platform",
            lambda name, timeout_seconds=120.0, model=None: (
                probed.append(name) or {"platform": name, "probe": "ok"}
            ),
        )
        config_path = tmp_path / "broken.json"
        config_path.write_text(
            json.dumps(
                {
                    "model": "main-model",
                    "profiles": {"r": {"platform": "custom", "roles": ["reviewer"]}},
                    "platforms": {
                        "custom": {"argv": ["tool", "-m", "preset", "-p", "{prompt}"]}
                    },
                }
            ),
            encoding="utf-8",
        )
        exit_code = compat.main(["platforms", "--config", str(config_path), "--probe"])
        assert exit_code == 2
        assert probed == []
        assert "already carries" in capsys.readouterr().err

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
        assert "# GoalAnchor reviewer review (round 1)" in prompt
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


class TestModelConsistency:
    """Sub-agents must run on the main agent's current model."""

    @staticmethod
    def _fake_subprocess(captured: list):
        def fake(argv, *, input_text, timeout_seconds, env):
            captured.append(tuple(argv))
            prompt = input_text
            if prompt is None:
                prompt = argv[list(argv).index("-p") + 1] if "-p" in argv else argv[-1]
            if not prompt.lstrip().startswith("{"):
                agent_id = re.search(r'agent_id="([^"]+)"', prompt).group(1)
                round_number = int(re.search(r"round (\d+)", prompt).group(1))
                fingerprint = re.search(
                    r'context_fingerprint="([0-9a-f]{64})"', prompt
                ).group(1)
                payload = {
                    "reviewer_id": agent_id,
                    "round_number": round_number,
                    "verdict": "PASS",
                    "findings": [],
                    "isolated": True,
                    "context_fingerprint": fingerprint,
                }
            else:
                payload = json.loads(prompt)
            return json.dumps(payload), ""

        return fake

    @staticmethod
    def _dispatch_config_with_argv_log(tmp_path, log_path, **overrides):
        """Dispatch config whose platform scripts log their argv to a file.

        Each process writes its own ``<log_path>.<pid>`` so parallel
        reviewers never interleave lines.
        """

        agent = _write_script(
            tmp_path,
            "agent_logged.py",
            FAKE_AGENT.replace(
                "import json, re, sys\n",
                "import json, re, os, sys\n"
                "_log = os.environ.get('GOALANCHOR_ARGV_LOG')\n"
                "if _log:\n"
                "    with open(f'{_log}.{os.getpid()}', 'a', encoding='utf-8') as _fh:\n"
                "        _fh.write(json.dumps(sys.argv[1:]) + '\\n')\n",
                1,
            ),
        )
        config = _dispatch_config(tmp_path, **overrides)
        for spec in config["platforms"].values():
            spec["argv"] = [sys.executable, agent] + list(spec["argv"])[2:]
            spec["env"] = {"GOALANCHOR_ARGV_LOG": str(log_path)}
        return config

    @staticmethod
    def _read_argv_log(log_path) -> list:
        lines: list = []
        for fragment in sorted(log_path.parent.glob(log_path.name + ".*")):
            lines.extend(
                json.loads(line)
                for line in fragment.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        return lines

    @staticmethod
    def _write_request(tmp_path, context) -> Path:
        request_path = tmp_path / "request.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH", "max_rounds": 2}),
            encoding="utf-8",
        )
        return request_path

    def test_builtin_model_flags_injected(self, context, monkeypatch):
        captured: list = []
        monkeypatch.setattr(
            "goalanchor.platforms._run_subprocess", self._fake_subprocess(captured)
        )
        expected = {
            "codex": ("codex", "exec", "-m", "main-model", "-"),
            "claude": ("claude", "--model", "main-model", "-p"),
            "gemini": ("gemini", "-m", "main-model"),
            "opencode": ("opencode", "run", "-m", "main-model"),
        }
        for name, argv in expected.items():
            config = PlatformDispatchConfig.from_dict(
                {
                    "model": "main-model",
                    "profiles": {"r": {"platform": name, "roles": ["reviewer"]}},
                }
            )
            request = AgentRequest(
                role=AgentRole.REVIEWER,
                agent_id="reviewer-1",
                round_number=1,
                risk=ReviewRisk.LOW,
                context=context,
            )
            PlatformAgentRouter(config).dispatch("r", request)
            assert captured[-1] == argv, name

    def test_top_level_model_reaches_subprocess_and_audit(
        self, tmp_path, context, capsys
    ):
        from goalanchor import compat

        log_path = tmp_path / "argv.log"
        request_path = self._write_request(tmp_path, context)
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                self._dispatch_config_with_argv_log(tmp_path, log_path, model="main-model")
            ),
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
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0 and payload["outcome"] == "PASS"
        bindings = payload["agent_bindings"]
        assert {item["model"] for item in bindings} == {"main-model"}
        assert payload["usage_summary"]["models_used"] == ["main-model"]
        lines = self._read_argv_log(log_path)
        assert lines
        for argv in lines:
            flag_index = argv.index("-m")
            assert argv[flag_index + 1] == "main-model"
            # the prompt stays last and intact after the flag insertion
            assert argv.index("-p") == flag_index + 2

    def test_divergent_profile_model_rejected_before_any_launch(
        self, tmp_path, context, capsys, monkeypatch
    ):
        from goalanchor import compat

        captured: list = []
        monkeypatch.setattr(
            "goalanchor.platforms._run_subprocess", self._fake_subprocess(captured)
        )
        config = _dispatch_config(tmp_path, model="main-model")
        config["profiles"]["volt"]["model"] = "other-model"
        request_path = tmp_path / "request.json"
        config_path = tmp_path / "config.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH"}),
            encoding="utf-8",
        )
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
        err = capsys.readouterr().err
        assert exit_code == 2
        assert "model_divergence" in err
        assert "other-model" in err and "main-model" in err
        assert captured == []

    def test_divergent_model_runs_only_with_user_confirmation(
        self, tmp_path, context, capsys
    ):
        from goalanchor import compat

        log_path = tmp_path / "argv.log"
        config = self._dispatch_config_with_argv_log(tmp_path, log_path, model="main-model")
        config["profiles"]["volt"]["model"] = "other-model"
        config["model_divergence"] = {
            "confirmed_by": "user",
            "reason": "用户确认允许该评审使用独立模型",
        }
        request_path = self._write_request(tmp_path, context)
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
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0 and payload["outcome"] == "PASS"
        assert payload["model_divergence"] == {
            "confirmed_by": "user",
            "reason": "用户确认允许该评审使用独立模型",
        }
        divergence_warnings = [
            item for item in payload["warnings"] if item["code"] == "MODEL_DIVERGENCE_APPROVED"
        ]
        assert len(divergence_warnings) == 1
        assert divergence_warnings[0]["confirmed_by"] == "user"
        by_agent = {item["agent_id"]: item["model"] for item in payload["agent_bindings"]}
        assert by_agent["reviewer-2"] == "other-model"
        assert by_agent["reviewer-1"] == "main-model"
        assert sorted(payload["usage_summary"]["models_used"]) == [
            "main-model",
            "other-model",
        ]
        # the confirmed divergent profile receives its own model flag; the
        # rest stay pinned to the main model
        lines = self._read_argv_log(log_path)
        by_mode = {argv[0]: argv[argv.index("-m") + 1] for argv in lines}
        assert by_mode["noisy"] == "other-model"
        assert set(by_mode.values()) == {"main-model", "other-model"}

    def test_cli_model_flag_overrides_config(self, tmp_path, context, capsys):
        from goalanchor import compat

        request_path = tmp_path / "request.json"
        config_path = tmp_path / "config.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH", "max_rounds": 2}),
            encoding="utf-8",
        )
        config_path.write_text(
            json.dumps(_dispatch_config(tmp_path, model="stale-model")),
            encoding="utf-8",
        )
        exit_code = compat.main(
            [
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(config_path),
                "--model",
                "fresh-main-model",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0 and payload["outcome"] == "PASS"
        assert payload["usage_summary"]["models_used"] == ["fresh-main-model"]

    def test_custom_platform_model_placeholder_and_missing_model_error(
        self, context, monkeypatch
    ):
        captured: list = []
        monkeypatch.setattr(
            "goalanchor.platforms._run_subprocess", self._fake_subprocess(captured)
        )
        with pytest.raises(AgentRuntimeError, match="no model is configured"):
            PlatformDispatchConfig.from_dict(
                {
                    "profiles": {
                        "r": {"platform": "custom", "roles": ["reviewer"]},
                    },
                    "platforms": {
                        "custom": {"argv": ["tool", "{model}", "--run"]},
                    },
                }
            )
        request = AgentRequest(
            role=AgentRole.REVIEWER,
            agent_id="reviewer-1",
            round_number=1,
            risk=ReviewRisk.LOW,
            context=context,
        )
        pinned = PlatformDispatchConfig.from_dict(
            {
                "model": "main-model",
                "profiles": {"r": {"platform": "custom", "roles": ["reviewer"]}},
                "platforms": {"custom": {"argv": ["tool", "{model}", "--run"]}},
            }
        )
        PlatformAgentRouter(pinned).dispatch("r", request)
        assert captured[-1] == ("tool", "main-model", "--run")

    def test_platform_model_precedence(self, context, monkeypatch):
        captured: list = []
        monkeypatch.setattr(
            "goalanchor.platforms._run_subprocess", self._fake_subprocess(captured)
        )
        config = PlatformDispatchConfig.from_dict(
            {
                "profiles": {
                    "plain": {"platform": "custom", "roles": ["reviewer"]},
                    "override": {"platform": "custom", "roles": ["reviewer"], "model": "profile-model"},
                },
                "platforms": {"custom": {"argv": ["tool", "{model}"], "model": "platform-model"}},
            }
        )
        request = AgentRequest(
            role=AgentRole.REVIEWER,
            agent_id="reviewer-1",
            round_number=1,
            risk=ReviewRisk.LOW,
            context=context,
        )
        PlatformAgentRouter(config).dispatch("plain", request)
        assert captured[-1] == ("tool", "platform-model")
        PlatformAgentRouter(config).dispatch("override", request)
        assert captured[-1] == ("tool", "profile-model")

    def test_dry_run_validates_model_consistency_and_reports_main_model(
        self, tmp_path, context, capsys
    ):
        from goalanchor import compat

        request_path = tmp_path / "request.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "HIGH"}),
            encoding="utf-8",
        )
        divergent = _dispatch_config(tmp_path, model="main-model")
        divergent["profiles"]["volt"]["model"] = "other-model"
        config_path = tmp_path / "divergent.json"
        config_path.write_text(json.dumps(divergent), encoding="utf-8")
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
        err = capsys.readouterr().err
        assert exit_code == 2
        assert "model_divergence" in err
        unpinned_path = tmp_path / "unpinned.json"
        unpinned_path.write_text(
            json.dumps(_dispatch_config(tmp_path)), encoding="utf-8"
        )
        exit_code = compat.main(
            [
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(unpinned_path),
                "--dry-run",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert [item["code"] for item in payload["warnings"]] == ["MODEL_NOT_PINNED"]
        ok_path = tmp_path / "ok.json"
        ok_path.write_text(
            json.dumps(_dispatch_config(tmp_path, model="main-model")), encoding="utf-8"
        )
        exit_code = compat.main(
            [
                "review-cycle",
                "--request",
                str(request_path),
                "--config",
                str(ok_path),
                "--dry-run",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload["main_model"] == "main-model"
        assert payload["model_divergence"] is None
        assert payload["warnings"] == []

    def test_hardcoded_model_flag_in_argv_is_rejected(self):
        # The rejection happens at config load, before any agent can launch.
        with pytest.raises(AgentRuntimeError, match=r"\{model\}"):
            PlatformDispatchConfig.from_dict(
                {
                    "model": "main-model",
                    "profiles": {"r": {"platform": "custom", "roles": ["reviewer"]}},
                    "platforms": {"custom": {"argv": ["tool", "-m", "preset", "-p", "{prompt}"]}},
                }
            )

    def test_attached_flag_spellings_are_rejected(self):
        from goalanchor.platforms import _apply_model

        for argv, flag in (
            (("claude", "--model=opus", "-p"), "--model"),
            (("gemini", "-m=opus"), "-m"),
        ):
            with pytest.raises(AgentRuntimeError, match="already carries"):
                _apply_model(argv, "main-model", model_flag=flag)
        # an unrelated option sharing the long-flag prefix is not a model flag
        assert _apply_model(
            ("tool", "--model-config=x", "{prompt}"), "main-model", model_flag="--model"
        ) == ("tool", "--model", "main-model", "--model-config=x", "{prompt}")
        # documented boundary: the glued short form cannot be told apart from
        # an unrelated same-prefix option, so it is not detected
        assert _apply_model(
            ("tool", "-max-tokens", "4000", "{prompt}"), "main-model", model_flag="-m"
        ) == ("tool", "-m", "main-model", "-max-tokens", "4000", "{prompt}")

    def test_flag_with_placeholder_value_is_accepted(self):
        # `-m {model}` / `--model={model}` is the placeholder site, not a
        # hardcoded override: it must fill in place instead of being rejected.
        from goalanchor.platforms import _apply_model

        assert _apply_model(
            ("tool", "-m", "{model}", "-p", "{prompt}"), "main-model", model_flag="-m"
        ) == ("tool", "-m", "main-model", "-p", "{prompt}")
        assert _apply_model(
            ("claude", "--model={model}", "-p"), "main-model", model_flag="--model"
        ) == ("claude", "--model=main-model", "-p")
        assert _apply_model(
            ("tool", "-m{model}", "-p"), "main-model", model_flag="-m"
        ) == ("tool", "-mmain-model", "-p")
        assert _apply_model(
            ("tool", "{model}", "-p"), "main-model", model_flag="-m"
        ) == ("tool", "main-model", "-p")

    def test_placeholder_outside_the_model_slot_is_literal(self):
        # `{model}` inside another argument is text, not a model slot: the
        # flag pair must still be injected (skipping it would leave the
        # sub-agent unpinned while the audit records a pinned model), and the
        # literal text is passed through.
        from goalanchor.platforms import _apply_model

        assert _apply_model(
            ("tool", "--note", "run as {model}", "-p", "{prompt}"),
            "main-model",
            model_flag="-m",
        ) == ("tool", "-m", "main-model", "--note", "run as {model}", "-p", "{prompt}")
        # ... and without a declared model it stays literal, as in v0.5.0
        assert _apply_model(
            ("tool", "--tag={model}", "-p"), None, model_flag="-m"
        ) == ("tool", "--tag={model}", "-p")

    def test_placeholder_plus_hardcoded_flag_is_rejected(self):
        # A {model} placeholder does not excuse a second, hardcoded model flag:
        # the substituted value would sit next to it and usually lose.
        from goalanchor.platforms import _apply_model

        with pytest.raises(AgentRuntimeError, match="already carries"):
            _apply_model(
                ("tool", "{model}", "--model=evil", "-p"),
                "main-model",
                model_flag="--model",
            )

    def test_disabled_profile_divergence_is_skipped(self):
        # A disabled profile never dispatches, so its divergent model must not
        # block the config — disabling a diverging profile is the natural fix.
        config = PlatformDispatchConfig.from_dict(
            {
                "model": "main-model",
                "platforms": {"custom": {"argv": ["tool", "-p", "{prompt}"]}},
                "profiles": {
                    "on": {"platform": "custom", "roles": ["reviewer"]},
                    "off": {
                        "platform": "custom",
                        "roles": ["reviewer"],
                        "enabled": False,
                        "model": "other-model",
                    },
                },
            }
        )
        assert config.binding("on").platform == "custom"

    def test_bridge_profile_model_rejected_in_platform_config(self):
        # A bridge profile inside a platform-shaped config cannot honor a
        # declared model either; it must be rejected like the legacy path.
        with pytest.raises(AgentRuntimeError, match="stdio bridge command"):
            PlatformDispatchConfig.from_dict(
                {
                    "platforms": {"custom": {"argv": ["tool", "{model}"]}},
                    "profiles": {
                        "b": {
                            "command": ["bridge", "--profile", "b"],
                            "roles": ["reviewer"],
                            "model": "ignored",
                        }
                    },
                }
            )

    def test_disabled_profile_argv_is_not_preflighted(self):
        # A disabled profile never dispatches (binding() rejects it and
        # AgentCatalog.select skips it), so its unusable argv must not fail
        # the whole config.
        config = PlatformDispatchConfig.from_dict(
            {
                "platforms": {
                    "live": {"argv": ["tool", "-p", "{prompt}"]},
                    "dead": {"argv": ["tool", "{model}"]},
                },
                "profiles": {
                    "on": {"platform": "live", "roles": ["reviewer"]},
                    "off": {"platform": "dead", "roles": ["reviewer"], "enabled": False},
                },
            }
        )
        assert config.binding("on").platform == "live"

    def test_custom_model_flag_override(self, context, monkeypatch):
        captured: list = []
        monkeypatch.setattr(
            "goalanchor.platforms._run_subprocess", self._fake_subprocess(captured)
        )
        config = PlatformDispatchConfig.from_dict(
            {
                "model": "main-model",
                "profiles": {"r": {"platform": "custom", "roles": ["reviewer"]}},
                "platforms": {"custom": {"argv": ["tool", "-p", "{prompt}"], "model_flag": "--model"}},
            }
        )
        request = AgentRequest(
            role=AgentRole.REVIEWER,
            agent_id="reviewer-1",
            round_number=1,
            risk=ReviewRisk.LOW,
            context=context,
        )
        PlatformAgentRouter(config).dispatch("r", request)
        # argv mode: the model flag precedes the prompt slot, prompt stays last
        assert captured[-1][:4] == ("tool", "--model", "main-model", "-p")
        assert "GoalAnchor reviewer review" in captured[-1][4]

    def test_legacy_bridge_config_rejects_declared_model(self, tmp_path, context, capsys):
        from goalanchor import compat

        with pytest.raises(AgentRuntimeError, match="stdio bridge configuration"):
            adapter_from_config(
                {
                    "profiles": {
                        "r": {"roles": ["reviewer"], "command": ["bridge"], "model": "m"}
                    }
                }
            )
        request_path = tmp_path / "request.json"
        config_path = tmp_path / "config.json"
        request_path.write_text(
            json.dumps({"context": context.to_dict(), "risk": "LOW"}),
            encoding="utf-8",
        )
        config_path.write_text(
            json.dumps(
                {
                    "model": "main-model",
                    "profiles": {"r": {"roles": ["reviewer"], "command": ["bridge"]}},
                }
            ),
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
        err = capsys.readouterr().err
        assert exit_code == 2
        assert "stdio bridge configuration" in err

    def test_unpinned_platform_dispatch_warns_and_audits_null(
        self, tmp_path, context, capsys
    ):
        from goalanchor import compat

        request_path = self._write_request(tmp_path, context)
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(_dispatch_config(tmp_path)), encoding="utf-8"
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
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert exit_code == 0 and payload["outcome"] == "PASS"
        assert "consistency cannot be enforced" in captured.err
        assert payload["usage_summary"]["main_model"] is None
        assert payload["usage_summary"]["models_used"] == []
        assert [item["code"] for item in payload["warnings"]] == ["MODEL_NOT_PINNED"]
        message = payload["warnings"][0]["message"]
        assert "consistency cannot be enforced" in message
        assert "no sub-agent model is pinned" not in message

    def test_pinned_dispatch_warns_nothing_and_audits_main_model(
        self, tmp_path, context, capsys
    ):
        from goalanchor import compat

        request_path = self._write_request(tmp_path, context)
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(_dispatch_config(tmp_path, model="main-model")), encoding="utf-8"
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
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert exit_code == 0
        assert "no sub-agent model pinned" not in captured.err
        assert payload["usage_summary"]["main_model"] == "main-model"
        assert payload["warnings"] == []

    def test_legacy_bridge_collects_no_warnings(self):
        from goalanchor import compat

        adapter = adapter_from_config(
            {
                "role_bindings": {"reviewer": "euclid"},
                "profiles": {
                    "euclid": {
                        "display_name": "Euclid",
                        "roles": ["reviewer"],
                        "command": ["goalanchor-host-bridge", "--profile", "euclid"],
                    }
                },
            }
        )
        warnings, declared = compat._model_audit_warnings(adapter, {})
        assert warnings == []
        assert declared is None

    def test_platform_level_pin_warns_consistency_unverified(
        self, tmp_path, context, capsys
    ):
        """Every sub-agent is pinned to the platform model, but the main model
        is undeclared, so consistency cannot be enforced: the warning must say
        exactly that instead of claiming nothing is pinned."""

        from goalanchor import compat

        log_path = tmp_path / "argv.log"
        config = self._dispatch_config_with_argv_log(tmp_path, log_path)
        for platform in ("fake-clean", "fake-envelope", "fake-noisy"):
            config["platforms"][platform]["model"] = "platform-model"
        request_path = self._write_request(tmp_path, context)
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
        captured_out = capsys.readouterr()
        payload = json.loads(captured_out.out)
        assert exit_code == 0
        assert [item["code"] for item in payload["warnings"]] == ["MODEL_NOT_PINNED"]
        message = payload["warnings"][0]["message"]
        assert "consistency cannot be enforced" in message
        assert "no sub-agent model is pinned" not in message
        assert payload["usage_summary"]["main_model"] is None
        # the platform-level pin did reach the dispatch
        assert payload["usage_summary"]["models_used"] == ["platform-model"]
        pinned_invocations = [
            argv
            for argv in self._read_argv_log(log_path)
            if "-m" in argv and argv[argv.index("-m") + 1] == "platform-model"
        ]
        assert pinned_invocations
