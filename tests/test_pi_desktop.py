from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from goalanchor.pi_desktop import build_plugin, dispatch, evaluate_preflight


def completion(request, *, dispute: bool = False, model: str = "provider/model") -> dict:
    role = request.role.value
    result = {"verdict": "PASS", "context_fingerprint": request.context.fingerprint,
              "round_number": request.round_number, "future_field": "ignored"}
    if role == "reviewer":
        result.update(reviewer_id=request.agent_id, isolated=True, findings=[])
    elif role == "arbiter":
        result.update(arbiter_id=request.agent_id, summary="checked", rationale="evidence compared",
                      reviewer_ids=[item.reviewer_id for item in request.reviewer_results],
                      disagrees_with_reviewers=dispute and request.round_number == 1)
    else:
        result.update(reviewer_id=request.agent_id, arbiter_id=request.arbiter_result.arbiter_id,
                      summary="checked arbitration", findings=[], isolated=True, agrees_with_arbiter=True)
    return {"text": json.dumps(result), "modelKey": model, "usage": {"totalTokens": 5}}


def review_request(risk: str = "HIGH") -> dict:
    return {"protocol_version": 1, "operation": "review", "model": "provider/model", "request": {
        "context": {"goal": "fix", "prompt": "review", "constraints": ["no regression"],
                    "evidence_refs": ["E1"], "candidate": "patch"},
        "risk": risk, "max_rounds": 2,
        "risk_declaration": {"declared_by": "user", "reason": "accepted for review"},
    }}


def preflight() -> dict:
    return {
        "contract": {
            "task_objective": "修复登录超时并交付",
            "constraints": ["保留 API"],
            "acceptance_criteria": ["登录回归通过"],
            "current_blocking_uncertainty": None,
            "stage": "PLAN",
            "deliverables": ["修复与测试"],
        },
        "work_items": [{
            "item_id": "W1", "issue": "登录超时", "input_evidence": ["E1"],
            "action": "修改超时", "expected_decision_impact": "完成回归",
            "exit_criterion": "登录回归通过", "expected_result": "登录成功",
            "decision_link": "交付修复",
            "assumptions": [], "assumptions_verified": False, "unverified_patch_refs": [],
        }],
        "structure_review": {},
        "verify": {
            "verification_object": "登录修复", "decision_significance": "决定是否交付",
            "exit_criterion": "固定回归用例全部通过", "observable": True, "finite": True,
        },
        "execute": {
            "plan_state": "VERIFY", "verify_passed": True, "constraints_acknowledged": True,
            "change_scope": "登录超时", "recovery_path": "恢复原修改",
        },
        "operations": [{"tool": "edit", "input": {"path": "登录.py", "text": "fix"}}],
    }


def test_preflight_runs_actual_gates_instead_of_trusting_boolean() -> None:
    record = preflight()
    result = evaluate_preflight(record)
    assert result["passed"] is True
    assert result["contract"]["stage"] == "VERIFY"
    record["verify"]["finite"] = False
    assert evaluate_preflight(record)["passed"] is False


def test_preflight_requires_original_plan_and_explicit_operations() -> None:
    record = preflight()
    record["operations"] = []
    with pytest.raises(ValueError):
        evaluate_preflight(record)
    record = preflight()
    record["contract"]["stage"] = "CLOSED"
    with pytest.raises(ValueError):
        evaluate_preflight(record)


def test_unknown_protocol_rejected_and_extra_envelope_fields_allowed() -> None:
    with pytest.raises(ValueError, match="protocol"):
        dispatch({"protocol_version": 2, "operation": "preflight", "record": preflight()})
    result = dispatch({
        "protocol_version": 1, "operation": "preflight", "record": preflight(), "future_field": 42,
    })
    assert result["passed"]


def test_build_preserves_skill_and_runs_without_repository(tmp_path: Path) -> None:
    destination = tmp_path / "独立 插件"
    archive = tmp_path / "goalanchor.piplug"
    build_plugin(destination, archive=archive)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["engines"]["piDesktop"] == ">=0.18.0"
    assert set(manifest["permissions"]) >= {"agent.extension", "agent.prompt.inject", "agent.complete"}
    assert (destination / "skills/goalanchor/SKILL.md").read_bytes() == (
        Path(__file__).resolve().parents[1] / "skills/goalanchor/SKILL.md"
    ).read_bytes()
    with zipfile.ZipFile(archive) as package:
        assert package.getinfo("manifest.json").compress_type == zipfile.ZIP_STORED
        assert all(not name.startswith("/") and ".." not in Path(name).parts for name in package.namelist())
    result = subprocess.run(
        [sys.executable, "-I", str(destination / "runtime/bridge.py")],
        input=json.dumps({"protocol_version": 1, "operation": "preflight", "record": preflight()}),
        text=True, capture_output=True, cwd=tmp_path, timeout=15, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"]["passed"]
    with pytest.raises(FileExistsError):
        build_plugin(destination)


def test_review_requires_model_and_valid_round_bound_before_dispatch() -> None:
    invoked = []
    payload = {
        "protocol_version": 1, "operation": "review", "model": "",
        "request": {
            "context": {"goal": "goal", "prompt": "review", "constraints": [],
                        "evidence_refs": ["E1"], "candidate": "fix"},
            "risk": "HIGH", "max_rounds": 3,
        },
    }
    with pytest.raises(ValueError):
        dispatch(payload, invoke=lambda request: invoked.append(request))
    assert not invoked


@pytest.mark.parametrize(("risk", "reviewers"), [("LOW", 1), ("MEDIUM", 2), ("HIGH", 2), ("CRITICAL", 3)])
def test_review_uses_domain_policy_and_returns_audit(risk: str, reviewers: int) -> None:
    result = dispatch(review_request(risk), invoke=completion)
    assert result["outcome"] == "PASS"
    assert len(result["reviewer_ids"]) == reviewers
    assert len(result["agent_bindings"]) == reviewers + 2
    assert {entry["model"] for entry in result["agent_bindings"]} == {"provider/model"}
    assert result["dispatch_mode"] == "host-completion"


def test_dispute_round_preserves_original_context_and_is_bounded() -> None:
    seen = []

    def invoke(request):
        seen.append(request)
        return completion(request, dispute=True)

    result = dispatch(review_request(), invoke=invoke)
    assert result["round_number"] == 2
    assert len(seen) == 8
    assert len({request.context.fingerprint for request in seen}) == 1
    assert any(request.dispute_packet is not None for request in seen)


def test_critical_missing_declaration_and_wrong_model_are_rejected() -> None:
    request = review_request("CRITICAL")
    del request["request"]["risk_declaration"]
    with pytest.raises(ValueError, match="declaration"):
        dispatch(request, invoke=completion)
    with pytest.raises(ValueError, match="model"):
        dispatch(review_request(), invoke=lambda request: completion(request, model="provider/other"))


def test_node_behavior_suite(tmp_path: Path) -> None:
    import os
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for PI-Desktop extension tests")
    root = Path(__file__).resolve().parents[1]
    plugin = tmp_path / "桌面插件"
    build_plugin(plugin)
    result = subprocess.run(
        [node, "--test", "tests/js/test_pi_desktop.cjs"], cwd=root,
        text=True, capture_output=True, timeout=45, encoding="utf-8",
        env={**os.environ, "GOALANCHOR_TEST_PYTHON": sys.executable, "GOALANCHOR_TEST_PLUGIN": str(plugin)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
