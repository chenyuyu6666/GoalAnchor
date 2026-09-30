from __future__ import annotations

import json
from pathlib import Path

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
