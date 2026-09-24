from __future__ import annotations

import sys
from threading import Barrier

from goalanchor import (
    AgentCatalog,
    AgentProfile,
    AgentRequest,
    AgentRole,
    AgentRuntimeCapabilities,
    ArbiterResult,
    JsonStdioAgentAdapter,
    MetaReviewResult,
    ReviewContext,
    ReviewerResult,
    ReviewFinding,
    ReviewRisk,
    ReviewVerdict,
    run_review_cycle_with_dispatcher,
)


def context() -> ReviewContext:
    return ReviewContext(
        goal="判断候选方案是否满足目标",
        prompt="独立分析目标、约束、证据和候选结果",
        constraints=("不能偷换目标",),
        evidence_refs=("E-1",),
        candidate="候选方案 A",
    )


def finding(finding_id: str) -> ReviewFinding:
    return ReviewFinding(
        finding_id=finding_id,
        issue="发现需要复核的争议",
        evidence_refs=("E-1",),
        decision_impact="决定是否继续",
        remaining_uncertainty="争议是否已解决",
    )


class FakeNamedDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, AgentRole, int, bool]] = []
        self.profiles = (
            AgentProfile("euclid", "Euclid", (AgentRole.REVIEWER,)),
            AgentProfile("hera", "Hera", (AgentRole.REVIEWER,)),
            AgentProfile("arbiter", "Arbiter", (AgentRole.ARBITER,)),
            AgentProfile("meta", "Meta Reviewer", (AgentRole.META_REVIEWER,)),
        )

    def list_agents(self):
        return self.profiles

    def capabilities(self):
        return AgentRuntimeCapabilities(supports_parallel=True, max_concurrency=2)

    def dispatch(self, profile_id: str, request: AgentRequest):
        self.calls.append(
            (profile_id, request.role, request.round_number, request.dispute_packet is not None)
        )
        if request.role is AgentRole.REVIEWER:
            failed = request.round_number == 1 and request.agent_id == "reviewer-2"
            return ReviewerResult(
                reviewer_id=request.agent_id,
                round_number=request.round_number,
                verdict=ReviewVerdict.FAIL if failed else ReviewVerdict.PASS,
                findings=(finding("F-1"),) if failed else (),
                context_fingerprint=request.context.fingerprint,
            ).to_dict()
        if request.role is AgentRole.ARBITER:
            return ArbiterResult(
                arbiter_id="arbiter",
                round_number=request.round_number,
                verdict=ReviewVerdict.PASS,
                summary="汇总 Reviewer",
                rationale="第二轮重新检查原始证据" if request.round_number == 2 else "需要复审",
                reviewer_ids=tuple(item.reviewer_id for item in request.reviewer_results),
                context_fingerprint=request.context.fingerprint,
                disagrees_with_reviewers=request.round_number == 1,
            ).to_dict()
        return MetaReviewResult(
            reviewer_id="meta-reviewer",
            round_number=request.round_number,
            arbiter_id="arbiter",
            verdict=ReviewVerdict.FAIL if request.round_number == 1 else ReviewVerdict.PASS,
            summary="独立复核 Arbiter",
            findings=(finding("META-1"),) if request.round_number == 1 else (),
            context_fingerprint=request.context.fingerprint,
            agrees_with_arbiter=request.round_number == 2,
        ).to_dict()


def test_named_dispatcher_runs_reviewers_in_parallel_and_retries_dispute() -> None:
    dispatcher = FakeNamedDispatcher()
    result = run_review_cycle_with_dispatcher(
        context(),
        ReviewRisk.HIGH,
        dispatcher,
        role_bindings={
            "reviewer-1": "euclid",
            "reviewer-2": "hera",
            AgentRole.ARBITER: "arbiter",
            AgentRole.META_REVIEWER: "meta",
        },
    )

    assert result.outcome.value == "PASS"
    assert result.round_number == 2
    assert [call[0] for call in dispatcher.calls] == [
        "euclid",
        "hera",
        "arbiter",
        "meta",
        "euclid",
        "hera",
        "arbiter",
        "meta",
    ]
    assert all(call[3] is False for call in dispatcher.calls[:2])
    assert all(call[3] is True for call in dispatcher.calls[4:6])


def test_dispatcher_capability_allows_reviewer_calls_to_overlap() -> None:
    class BarrierDispatcher(FakeNamedDispatcher):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = Barrier(2)

        def dispatch(self, profile_id: str, request: AgentRequest):
            if request.role is AgentRole.REVIEWER:
                self.calls.append(
                    (profile_id, request.role, request.round_number, request.dispute_packet is not None)
                )
                self.barrier.wait(timeout=2)
                return ReviewerResult(
                    reviewer_id=request.agent_id,
                    round_number=request.round_number,
                    verdict=ReviewVerdict.PASS,
                    findings=(),
                    context_fingerprint=request.context.fingerprint,
                ).to_dict()
            return super().dispatch(profile_id, request)

    dispatcher = BarrierDispatcher()
    result = run_review_cycle_with_dispatcher(
        context(),
        ReviewRisk.HIGH,
        dispatcher,
        role_bindings={"reviewer-1": "euclid", "reviewer-2": "hera"},
        max_rounds=1,
    )
    assert result.round_number == 1
    assert len([call for call in dispatcher.calls if call[1] is AgentRole.REVIEWER]) == 2


def test_agent_catalog_selects_named_enabled_profiles() -> None:
    catalog = AgentCatalog(
        (
            AgentProfile("euclid", "Euclid", (AgentRole.REVIEWER,)),
            AgentProfile("meta", "Meta Reviewer", (AgentRole.META_REVIEWER,)),
        )
    )
    assert catalog.select(AgentRole.REVIEWER).display_name == "Euclid"
    assert catalog.select(AgentRole.META_REVIEWER, "meta").profile_id == "meta"


def test_json_stdio_adapter_is_platform_neutral() -> None:
    bridge = (
        "import json, sys; request=json.load(sys.stdin); "
        "print(json.dumps({'reviewer_id': request['agent_id'], 'round_number': request['round_number'], "
        "'verdict': 'PASS', 'findings': [], 'isolated': True, "
        "'context_fingerprint': request['context']['fingerprint']}))"
    )
    adapter = JsonStdioAgentAdapter.from_dict(
        {
            "role_bindings": {"reviewer": "euclid"},
            "profiles": {
                "euclid": {
                    "display_name": "Euclid",
                    "roles": ["reviewer"],
                    "command": [sys.executable, "-c", bridge],
                }
            },
        }
    )
    assert adapter.list_agents()[0].display_name == "Euclid"
    request = AgentRequest(
        role=AgentRole.REVIEWER,
        agent_id="reviewer-1",
        round_number=1,
        risk=ReviewRisk.MEDIUM,
        context=context(),
    )
    response = adapter.invoke(request)
    assert response["reviewer_id"] == "reviewer-1"
    assert response["context_fingerprint"] == request.context.fingerprint
