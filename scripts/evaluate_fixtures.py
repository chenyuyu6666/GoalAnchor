"""Evaluate fixed GoalAnchor regression fixtures against one package root."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _sha256_fixtures(fixtures_dir: Path, fixture_paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in fixture_paths:
        digest.update(path.relative_to(fixtures_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _acceptance_sha256(cases: list[Mapping[str, Any]]) -> str:
    acceptance = [
        {
            "fixture_id": case["fixture_id"],
            "expected_passed": case["expected_passed"],
            "expected_finding_codes": case.get("expected_finding_codes", []),
        }
        for case in cases
    ]
    encoded = json.dumps(acceptance, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _contract(GoalContract, LifecycleState, **overrides):
    fields = {
        "task_objective": "达到可接受的页面滑动流畅度",
        "constraints": ("保持视频和页面状态正确",),
        "acceptance_criteria": ("handler latency 满足目标",),
        "current_blocking_uncertainty": None,
        "stage": LifecycleState.PLAN,
        "deliverables": ("验证报告",),
    }
    fields.update(overrides)
    return GoalContract(**fields)


def _work_item(WorkItem, **overrides):
    fields = {
        "item_id": "W1",
        "issue": "无法区分 Snapshot 与 timer 的耗时贡献",
        "input_evidence": ("日志 E1",),
        "action": "分别执行 timer-only 与 capture-only profiling",
        "expected_decision_impact": "决定优化调度还是捕获路径",
        "exit_criterion": "得到两个路径的可归因耗时",
        "expected_result": "输出 handler latency 分解",
        "decision_link": "是否进入 Snapshot 架构重构",
    }
    fields.update(overrides)
    return WorkItem(**fields)


def _evidence(EvidenceRecord, EvidenceLevel, ConclusionLevel, EvidenceStatus, **overrides):
    fields = {
        "evidence_id": "E1",
        "claim": "Snapshot API wall-time is 343–532 ms",
        "level": EvidenceLevel.REPRODUCIBLE_EXPERIMENT,
        "source_ref": ("log:E1", "source:display.c:42"),
        "scope": "LVGL handler during page swipe",
        "observation_window_seconds": 30,
        "event_period_seconds": 10,
        "controls": ("resolution=720x720", "ARGB8888"),
        "repetitions": 3,
        "supports": (ConclusionLevel.ASSOCIATION,),
        "does_not_support": (ConclusionLevel.ROOT_CAUSE,),
        "status": EvidenceStatus.VALID,
        "measured_object": "snapshot_take_to_draw_buf",
        "measurement_value": "343–532",
        "measurement_unit": "ms",
    }
    fields.update(overrides)
    return EvidenceRecord(**fields)


def _experiment(ExperimentPlan, AttributionStatus, **overrides):
    fields = {
        "experiment_id": "X1",
        "question": "Does timer scan add measurable handler latency?",
        "variable_under_test": "timer_scan",
        "controls": ("snapshot_capture=off", "same firmware", "same input trace"),
        "metrics": ("handler_duration", "frame_interval", "input_latency"),
        "observation_window_seconds": 30,
        "repetitions": 3,
        "exit_criterion": "three runs report the median handler duration",
        "decision_links": ("choose timer or snapshot optimization",),
        "event_period_seconds": 10,
        "declared_attribution": AttributionStatus.ATTRIBUTABLE,
    }
    fields.update(overrides)
    return ExperimentPlan(**fields)


def _manifest(ArtifactManifest, ArtifactKind, **overrides):
    fields = {
        "manifest_id": "M1",
        "artifact_id": "app-fw-001",
        "source_revision": "commit-abc",
        "change_summary": "修复恢复路径竞争",
        "build_command": "build firmware",
        "built_at": "2026-09-04T12:00:00Z",
        "artifact_kind": ArtifactKind.FW,
        "artifact_path": "build/app.fw",
        "artifact_sha256": "a" * 64,
        "test_object": "device-01",
        "build_success": True,
        "hash_verified": True,
    }
    fields.update(overrides)
    return ArtifactManifest(**fields)


def _chain(ProvenanceChain, ProvenanceNode, ProvenanceLink, ProvenanceKind, manifest, variant=None):
    nodes = (
        ProvenanceNode("source", ProvenanceKind.SOURCE, "src-tree", "1" * 64, manifest.source_revision),
        ProvenanceNode("object", ProvenanceKind.OBJECT, "app.o", "2" * 64, manifest.source_revision),
        ProvenanceNode("elf", ProvenanceKind.ELF, "app.elf", "3" * 64, manifest.source_revision),
        ProvenanceNode(
            "bin", ProvenanceKind.BIN_FW, manifest.artifact_id,
            manifest.artifact_sha256, manifest.source_revision,
        ),
        ProvenanceNode("flashed", ProvenanceKind.FLASHED_ARTIFACT, "flash-001", "5" * 64),
        ProvenanceNode("dut", ProvenanceKind.DEVICE_UNDER_TEST, "device-01"),
    )
    links = tuple(
        ProvenanceLink(nodes[index].node_id, nodes[index + 1].node_id)
        for index in range(len(nodes) - 1)
    )
    if variant == "missing_device":
        nodes = nodes[:-1]
        links = links[:-1]
    if variant == "wrong_order":
        nodes = (nodes[1], nodes[0], *nodes[2:])
    return ProvenanceChain(nodes=nodes, links=links)


def _closure(ClosureRecord, ClosureLevel, manifest, chain, **overrides):
    fields = {
        "requested_level": ClosureLevel.PRODUCT_CLOSURE,
        "evidence_refs": ("E-root", "E-system"),
        "containment_scope": "all supported devices",
        "root_cause_proven": True,
        "fix_change_ref": "commit-fix-001",
        "build_success": True,
        "artifact_verified": True,
        "system_validation_passed": True,
        "acceptance_complete": True,
        "artifact_manifest": manifest,
        "provenance": chain,
        "test_object": "device-01",
    }
    fields.update(overrides)
    return ClosureRecord(**fields)


def _codes(result: Any) -> tuple[str, ...]:
    if hasattr(result, "findings"):
        return tuple(item.code for item in result.findings)
    if hasattr(result, "results"):
        values: list[str] = []
        for nested in result.results:
            values.extend(item.code for item in nested.findings)
        return tuple(values)
    return ()


def evaluate_case(case: Mapping[str, Any]) -> Any:
    from goalanchor import (
        ArtifactKind,
        ArtifactManifest,
        AttributionStatus,
        ClosureLevel,
        ClosureRecord,
        ConclusionLevel,
        EvidenceLevel,
        EvidenceRecord,
        EvidenceStatus,
        ExperimentPlan,
        GoalContract,
        LifecycleState,
        ProvenanceChain,
        ProvenanceKind,
        ProvenanceLink,
        ProvenanceNode,
        StructureReview,
        WorkItem,
        check_artifact_manifest,
        check_closure,
        check_conclusion,
        check_evidence,
        check_experiment,
        check_provenance,
        evaluate_plan,
    )

    case_data = case.get("case", {})
    phase = case["phase"]
    if phase == "plan":
        contract = _contract(GoalContract, LifecycleState, **case_data.get("contract", {}))
        work_items = tuple(
            _work_item(WorkItem, **item) for item in case_data.get("work_items", ({},))
        )
        structure = StructureReview(
            **case_data.get("structure_review", {"responsibility_keys": ("snapshot",)})
        )
        return evaluate_plan(contract, work_items, structure)
    if phase == "evidence":
        return check_evidence(
            _evidence(EvidenceRecord, EvidenceLevel, ConclusionLevel, EvidenceStatus,
                      **case_data.get("evidence", {}))
        )
    if phase == "conclusion":
        evidence = _evidence(EvidenceRecord, EvidenceLevel, ConclusionLevel,
                             EvidenceStatus, **case_data.get("evidence", {}))
        return check_conclusion(case_data.get("conclusion_level", "Causal"), (evidence,))
    if phase == "experiment":
        return check_experiment(
            _experiment(ExperimentPlan, AttributionStatus,
                        **case_data.get("experiment", {}))
        )
    if phase == "provenance":
        manifest = _manifest(ArtifactManifest, ArtifactKind)
        chain = _chain(ProvenanceChain, ProvenanceNode, ProvenanceLink,
                       ProvenanceKind, manifest, case_data.get("provenance_variant"))
        return check_provenance(chain, manifest)
    if phase == "closure":
        manifest = _manifest(ArtifactManifest, ArtifactKind)
        chain = _chain(ProvenanceChain, ProvenanceNode, ProvenanceLink,
                       ProvenanceKind, manifest)
        return check_closure(
            _closure(ClosureRecord, ClosureLevel, manifest, chain,
                     **case_data.get("closure", {}))
        )
    if phase == "artifact":
        return check_artifact_manifest(
            _manifest(ArtifactManifest, ArtifactKind,
                      **case_data.get("artifact_manifest", {}))
        )
    raise ValueError(f"unsupported fixture phase: {phase}")


def run(fixtures_dir: Path, revision: str) -> dict[str, Any]:
    fixture_paths = sorted(fixtures_dir.glob("*.json"))
    if not fixture_paths:
        raise ValueError(f"no JSON fixtures found in {fixtures_dir}")
    cases: list[Mapping[str, Any]] = []
    for path in fixture_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cases.extend(payload.get("cases", []))
    outcomes = []
    for case in cases:
        result = evaluate_case(case)
        outcomes.append(
            {
                "fixture_id": case["fixture_id"],
                "failure_class": case["failure_class"],
                "expected_passed": case["expected_passed"],
                "actual_passed": result.passed,
                "finding_codes": list(_codes(result)),
                "expected_finding_codes": case.get("expected_finding_codes", []),
                "aligned": (
                    case["expected_passed"] == result.passed
                    and (
                        not case.get("expected_finding_codes")
                        or tuple(_codes(result)) == tuple(case["expected_finding_codes"])
                    )
                ),
            }
        )
    return {
        "revision": revision,
        "fixtures_sha256": _sha256_fixtures(fixtures_dir, fixture_paths),
        "acceptance_sha256": _acceptance_sha256(cases),
        "total": len(outcomes),
        "aligned_count": sum(item["aligned"] for item in outcomes),
        "accepted_count": sum(item["actual_passed"] for item in outcomes),
        "rejected_count": sum(not item["actual_passed"] for item in outcomes),
        "outcomes": outcomes,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate GoalAnchor regression fixtures")
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    if args.package_root is not None:
        sys.path.insert(0, str(args.package_root.resolve()))
    try:
        result = run(args.fixtures.resolve(), args.revision)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"fixture evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return 0 if result["aligned_count"] == result["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
