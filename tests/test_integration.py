from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from goalanchor import (
    ClaimKind,
    GraphConfidence,
    GraphEvidenceRef,
    ReviewFinding,
    check_graphify_output,
    check_graphify_refs,
    check_review_output,
    graphify_claims_as_observations,
    normalize_graphify_output,
    normalize_review_output,
)
from goalanchor.compat import main
from goalanchor.core import ContractValidationError


def make_review(**overrides) -> dict[str, object]:
    finding = {
        "finding_id": "R1",
        "issue": "Snapshot path may dominate latency",
        "evidence_refs": ["log:E1"],
        "decision_impact": "decide whether to run timer-only profiling",
        "remaining_uncertainty": "timer scan contribution is unmeasured",
        "source_refs": ["review.md:12"],
    }
    finding.update(overrides)
    return {"findings": [finding]}


def make_graph(**overrides) -> dict[str, object]:
    node = {
        "node_id": "display_snapshot",
        "label": "snapshot_take_to_draw_buf",
        "source_file": "src/display.c",
        "source_location": "L42",
        "confidence": "EXTRACTED",
        "relation": "calls",
    }
    node.update(overrides)
    return {"nodes": [node]}


def test_review_finding_round_trip_and_alias_normalization() -> None:
    finding = ReviewFinding.from_dict(make_review()["findings"][0])
    assert ReviewFinding.from_dict(finding.to_dict()) == finding
    aliases = {
        "findings": [
            {
                "finding_id": "R2",
                "problem": "timer scan cost unknown",
                "evidence": ["source:L8"],
                "decision_link": "run timer-only test",
                "uncertainty": "no isolated timing",
            }
        ]
    }
    assert normalize_review_output(aliases)[0].issue == "timer scan cost unknown"


def test_review_output_requires_problem_evidence_decision_and_uncertainty() -> None:
    assert check_review_output(make_review()).passed
    for field in ("issue", "evidence_refs", "decision_impact", "remaining_uncertainty"):
        finding = make_review()["findings"][0].copy()
        finding.pop(field)
        result = check_review_output({"findings": [finding]})
        assert not result.passed
        assert result.findings[0].code == "REVIEW_OUTPUT_INVALID"

    assert check_review_output({"findings": []}).findings[0].code == "REVIEW_FINDINGS_EMPTY"


def test_graphify_reference_requires_location_and_confidence() -> None:
    assert check_graphify_output(make_graph()).passed
    for field in ("source_file", "source_location", "confidence"):
        node = make_graph()["nodes"][0].copy()
        node.pop(field)
        result = check_graphify_output({"nodes": [node]})
        assert not result.passed
        assert result.findings[0].code == "GRAPHIFY_OUTPUT_INVALID"

    assert check_graphify_output({"nodes": []}).findings[0].code == "GRAPHIFY_REFS_EMPTY"


def test_graphify_is_always_observation_not_fact_or_root_cause() -> None:
    refs = normalize_graphify_output(make_graph())
    assert refs[0].confidence is GraphConfidence.EXTRACTED
    claims = graphify_claims_as_observations(make_graph())
    assert len(claims) == 1
    assert claims[0].kind is ClaimKind.OBSERVATION
    assert claims[0].source == "src/display.c:L42"

    ambiguous = GraphEvidenceRef(
        "n2", "possible caller", "src/main.c", "L7", GraphConfidence.AMBIGUOUS
    )
    assert check_graphify_refs((ambiguous,)).passed
    assert ambiguous.as_claim().kind is ClaimKind.OBSERVATION


def test_graphify_normalization_rejects_non_mapping_nodes() -> None:
    with pytest.raises(ContractValidationError):
        normalize_graphify_output({"nodes": ["not a node"]})


def test_integration_cli_returns_pass_and_fail(tmp_path: Path, capsys) -> None:
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(make_review(), ensure_ascii=False), encoding="utf-8")
    assert main(["gate", "--phase", "review", "--record", str(review_path)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(make_graph(), ensure_ascii=False), encoding="utf-8")
    assert main(["gate", "--phase", "graphify", "--record", str(graph_path)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True

    failed_path = tmp_path / "failed.json"
    failed_path.write_text(json.dumps({"nodes": [{"label": "no source"}]}), encoding="utf-8")
    assert main(["gate", "--phase", "graphify", "--record", str(failed_path)]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_review_cycle_cli_accepts_external_agent_results(tmp_path: Path, capsys) -> None:
    from goalanchor import ReviewContext

    review_context = ReviewContext(
        goal="判断候选方案",
        prompt="按同一提示独立审核",
        constraints=("不能偷换目标",),
        evidence_refs=("E-1",),
        candidate="方案 A",
    )
    context = review_context.to_dict()
    fingerprint = review_context.fingerprint
    finding = {
        "finding_id": "F1",
        "issue": "存在争议",
        "evidence_refs": ["E-1"],
        "decision_impact": "是否继续",
        "remaining_uncertainty": "争议是否解决",
    }
    record = {
        "context": context,
        "risk": "HIGH",
        "round_number": 1,
        "max_rounds": 2,
        "reviewer_results": [
            {
                "reviewer_id": "A",
                "round_number": 1,
                "verdict": "PASS",
                "findings": [],
                "context_fingerprint": fingerprint,
            },
            {
                "reviewer_id": "B",
                "round_number": 1,
                "verdict": "FAIL",
                "findings": [finding],
                "context_fingerprint": fingerprint,
            },
        ],
        "arbiter": {
            "arbiter_id": "arbiter",
            "round_number": 1,
            "verdict": "PASS",
            "summary": "主 Agent 汇总",
            "rationale": "需要复审",
            "reviewer_ids": ["A", "B"],
            "context_fingerprint": fingerprint,
            "disagrees_with_reviewers": True,
        },
        "meta_review": {
            "reviewer_id": "meta",
            "round_number": 1,
            "arbiter_id": "arbiter",
            "verdict": "FAIL",
            "summary": "主 Agent 判断需要复审",
            "findings": [finding],
            "context_fingerprint": fingerprint,
            "agrees_with_arbiter": False,
        },
    }
    path = tmp_path / "review-cycle.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    assert main(["gate", "--phase", "review-cycle", "--record", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "REVIEW_REQUIRED"
    assert output["has_dispute_packet"] is True


def test_review_cycle_cli_stops_and_reports_after_second_unresolved_round(
    tmp_path: Path, capsys
) -> None:
    from goalanchor import ReviewContext

    context = {
        "goal": "判断候选方案",
        "prompt": "按同一提示独立审核",
        "constraints": ["不能偷换目标"],
        "evidence_refs": ["E-1"],
        "candidate": "方案 A",
    }
    fingerprint = ReviewContext(**context).fingerprint
    finding = {
        "finding_id": "F2",
        "issue": "第二轮仍存在高风险争议",
        "evidence_refs": ["E-1"],
        "decision_impact": "是否停止当前路径",
        "remaining_uncertainty": "用户需要选择下一步",
    }
    record = {
        "context": context,
        "risk": "HIGH",
        "round_number": 2,
        "max_rounds": 2,
        "reviewer_results": [
            {
                "reviewer_id": "A",
                "round_number": 2,
                "verdict": "PASS",
                "findings": [],
                "context_fingerprint": fingerprint,
            },
            {
                "reviewer_id": "B",
                "round_number": 2,
                "verdict": "FAIL",
                "findings": [finding],
                "context_fingerprint": fingerprint,
            },
        ],
        "arbiter": {
            "arbiter_id": "arbiter",
            "round_number": 2,
            "verdict": "PASS",
            "summary": "主 Agent 仍倾向通过",
            "rationale": "仍不同意 Reviewer B",
            "reviewer_ids": ["A", "B"],
            "context_fingerprint": fingerprint,
            "disagrees_with_reviewers": True,
        },
        "meta_review": {
            "reviewer_id": "meta",
            "round_number": 2,
            "arbiter_id": "arbiter",
            "verdict": "FAIL",
            "summary": "Meta Reviewer 仍不同意主 Agent",
            "findings": [finding],
            "context_fingerprint": fingerprint,
            "agrees_with_arbiter": False,
        },
    }
    path = tmp_path / "review-cycle-stopped.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    assert main(["gate", "--phase", "review-cycle", "--record", str(path)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "STOPPED"
    assert output["next_state"] == "STOPPED"
    assert output["decision_report"]["required_user_decision"]


def test_review_cycle_command_dispatches_named_profiles(tmp_path: Path, capsys) -> None:
    from goalanchor import ReviewContext

    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        """
import json
import sys

request = json.load(sys.stdin)
role = request["role"]
fingerprint = request["context"]["fingerprint"]
if role == "reviewer":
    result = {
        "reviewer_id": request["agent_id"],
        "round_number": request["round_number"],
        "verdict": "PASS",
        "findings": [],
        "isolated": True,
        "context_fingerprint": fingerprint,
    }
elif role == "arbiter":
    result = {
        "arbiter_id": "arbiter",
        "round_number": request["round_number"],
        "verdict": "PASS",
        "summary": "汇总 Reviewer",
        "rationale": "所有 Reviewer 通过",
        "reviewer_ids": [item["reviewer_id"] for item in request["reviewer_results"]],
        "context_fingerprint": fingerprint,
        "disagrees_with_reviewers": False,
    }
else:
    result = {
        "reviewer_id": "meta-reviewer",
        "round_number": request["round_number"],
        "arbiter_id": "arbiter",
        "verdict": "PASS",
        "summary": "独立复核通过",
        "findings": [],
        "context_fingerprint": fingerprint,
        "agrees_with_arbiter": True,
        "isolated": True,
    }
print(json.dumps(result, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    review_context = ReviewContext(
        goal="判断候选方案",
        prompt="独立审核",
        constraints=("不能偷换目标",),
        evidence_refs=("E-1",),
        candidate="方案 A",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "context": review_context.to_dict(),
                "risk": "HIGH",
                "max_rounds": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    command = [sys.executable, str(bridge)]
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "role_bindings": {
                    "reviewer-1": "euclid",
                    "reviewer-2": "hera",
                    "arbiter": "arbiter",
                    "meta_reviewer": "meta",
                },
                "capabilities": {"supports_parallel": True, "max_concurrency": 2},
                "profiles": {
                    "euclid": {"display_name": "Euclid", "roles": ["reviewer"], "command": command},
                    "hera": {"display_name": "Hera", "roles": ["reviewer"], "command": command},
                    "arbiter": {"display_name": "Arbiter", "roles": ["arbiter"], "command": command},
                    "meta": {"display_name": "Meta Reviewer", "roles": ["meta_reviewer"], "command": command},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert main(["review-cycle", "--request", str(request_path), "--config", str(config_path)]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "PASS"
