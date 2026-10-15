"""Bounded aggregation protocol for independently produced review results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .core import ContractValidationError, LifecycleState
from .integration import ReviewFinding


class ReviewRisk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ReviewVerdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class ReviewOutcome(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
    STOPPED = "STOPPED"


# Multi-review policy: a single independent reviewer is not credible for
# acceptance decisions (weak-model variance, unverifiable isolation), so LOW
# still requires one mandatory review and every non-LOW risk requires at
# least two reviewers on the same context. HIGH/CRITICAL stay capped to
# bound platform cost.
_REVIEWERS_BY_RISK = {
    ReviewRisk.LOW: 1,
    ReviewRisk.MEDIUM: 2,
    ReviewRisk.HIGH: 2,
    ReviewRisk.CRITICAL: 3,
}


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{field_name} must be an array of strings")
    return tuple(_text(item, f"{field_name}[]") for item in value)


def _enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(f"{field_name} is invalid") from exc


@dataclass(frozen=True)
class ReviewContext:
    """Immutable subject shared by every independent reviewer."""

    goal: str
    prompt: str
    constraints: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    candidate: str
    evidence_digests: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal", _text(self.goal, "goal"))
        object.__setattr__(self, "prompt", _text(self.prompt, "prompt"))
        object.__setattr__(self, "constraints", _strings(self.constraints, "constraints"))
        object.__setattr__(self, "evidence_refs", _strings(self.evidence_refs, "evidence_refs"))
        object.__setattr__(self, "candidate", _text(self.candidate, "candidate"))
        if isinstance(self.evidence_digests, (str, bytes)) or not isinstance(self.evidence_digests, Sequence):
            raise ContractValidationError("evidence_digests must be an array of ref/SHA-256 pairs")
        digests: dict[str, str] = {}
        for pair in self.evidence_digests:
            if isinstance(pair, (str, bytes)) or not isinstance(pair, Sequence) or len(pair) != 2:
                raise ContractValidationError("evidence_digests entries must be ref/SHA-256 pairs")
            ref = _text(pair[0], "evidence_digests ref")
            digest = _text(pair[1], "evidence_digests SHA-256").lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ContractValidationError("evidence_digests require 64 hexadecimal SHA-256 characters")
            if ref in digests:
                raise ContractValidationError("evidence_digests must not repeat a ref")
            digests[ref] = digest
        if digests and set(digests) != set(self.evidence_refs):
            raise ContractValidationError("evidence_digests must cover exactly the evidence_refs")
        object.__setattr__(self, "evidence_digests", tuple(sorted(digests.items())))

    @property
    def fingerprint(self) -> str:
        payload: dict[str, Any] = {
            "goal": self.goal,
            "prompt": self.prompt,
            "constraints": list(self.constraints),
            "evidence_refs": list(self.evidence_refs),
            "candidate": self.candidate,
        }
        if self.evidence_digests:
            payload["evidence_digests"] = [list(pair) for pair in self.evidence_digests]
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "goal": self.goal,
            "prompt": self.prompt,
            "constraints": list(self.constraints),
            "evidence_refs": list(self.evidence_refs),
            "candidate": self.candidate,
            "fingerprint": self.fingerprint,
        }
        if self.evidence_digests:
            payload["evidence_digests"] = [list(pair) for pair in self.evidence_digests]
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ReviewContext:
        if not isinstance(payload, Mapping):
            raise ContractValidationError("payload must be a mapping")
        data = dict(payload)
        expected_fingerprint = data.pop("fingerprint", None)
        context = cls(**data)
        if expected_fingerprint is not None:
            if not isinstance(expected_fingerprint, str) or expected_fingerprint != context.fingerprint:
                raise ContractValidationError("fingerprint does not match ReviewContext")
        return context


@dataclass(frozen=True)
class ReviewerResult:
    reviewer_id: str
    round_number: int
    verdict: ReviewVerdict
    findings: tuple[ReviewFinding, ...] = ()
    isolated: bool = True
    context_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reviewer_id, str) or not self.reviewer_id.strip():
            raise ContractValidationError("reviewer_id must be a non-empty string")
        if isinstance(self.round_number, bool) or not isinstance(self.round_number, int):
            raise ContractValidationError("round_number must be an integer")
        if self.round_number < 1:
            raise ContractValidationError("round_number must be at least 1")
        verdict = self.verdict
        if not isinstance(verdict, ReviewVerdict):
            try:
                verdict = ReviewVerdict(verdict)
            except (TypeError, ValueError) as exc:
                raise ContractValidationError("verdict is invalid") from exc
            object.__setattr__(self, "verdict", verdict)
        findings = tuple(self.findings)
        if any(not isinstance(item, ReviewFinding) for item in findings):
            raise ContractValidationError("findings must contain ReviewFinding values")
        if verdict is not ReviewVerdict.PASS and not findings:
            raise ContractValidationError("WARN and FAIL reviewer results require findings")
        object.__setattr__(self, "findings", findings)
        if not isinstance(self.isolated, bool):
            raise ContractValidationError("isolated must be a boolean")
        if self.context_fingerprint is not None:
            object.__setattr__(
                self, "context_fingerprint", _text(self.context_fingerprint, "context_fingerprint")
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer_id": self.reviewer_id,
            "round_number": self.round_number,
            "verdict": self.verdict.value,
            "findings": [item.to_dict() for item in self.findings],
            "isolated": self.isolated,
            "context_fingerprint": self.context_fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ReviewerResult:
        data = dict(payload)
        findings = tuple(
            ReviewFinding.from_dict(item) for item in data.pop("findings", ())
        )
        return cls(findings=findings, **data)


@dataclass(frozen=True)
class ArbiterResult:
    """The main Agent's summary and judgment over one review round."""

    arbiter_id: str
    round_number: int
    verdict: ReviewVerdict
    summary: str
    rationale: str
    reviewer_ids: tuple[str, ...]
    context_fingerprint: str
    disagrees_with_reviewers: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "arbiter_id", _text(self.arbiter_id, "arbiter_id"))
        if isinstance(self.round_number, bool) or not isinstance(self.round_number, int) or self.round_number < 1:
            raise ContractValidationError("round_number must be a positive integer")
        object.__setattr__(self, "verdict", _enum(self.verdict, ReviewVerdict, "verdict"))
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))
        reviewer_ids = _strings(self.reviewer_ids, "reviewer_ids")
        if not reviewer_ids or len(reviewer_ids) != len(set(reviewer_ids)):
            raise ContractValidationError("reviewer_ids must be unique and non-empty")
        object.__setattr__(self, "reviewer_ids", reviewer_ids)
        object.__setattr__(self, "context_fingerprint", _text(self.context_fingerprint, "context_fingerprint"))
        if not isinstance(self.disagrees_with_reviewers, bool):
            raise ContractValidationError("disagrees_with_reviewers must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "arbiter_id": self.arbiter_id,
            "round_number": self.round_number,
            "verdict": self.verdict.value,
            "summary": self.summary,
            "rationale": self.rationale,
            "reviewer_ids": list(self.reviewer_ids),
            "context_fingerprint": self.context_fingerprint,
            "disagrees_with_reviewers": self.disagrees_with_reviewers,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ArbiterResult:
        return cls(**dict(payload))


@dataclass(frozen=True)
class MetaReviewResult:
    """An independent review of the main Agent's arbiter conclusion."""

    reviewer_id: str
    round_number: int
    arbiter_id: str
    verdict: ReviewVerdict
    summary: str
    findings: tuple[ReviewFinding, ...]
    context_fingerprint: str
    agrees_with_arbiter: bool
    isolated: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "reviewer_id", _text(self.reviewer_id, "reviewer_id"))
        if isinstance(self.round_number, bool) or not isinstance(self.round_number, int) or self.round_number < 1:
            raise ContractValidationError("round_number must be a positive integer")
        object.__setattr__(self, "arbiter_id", _text(self.arbiter_id, "arbiter_id"))
        object.__setattr__(self, "verdict", _enum(self.verdict, ReviewVerdict, "verdict"))
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        findings = tuple(self.findings)
        if self.verdict is not ReviewVerdict.PASS and not findings:
            raise ContractValidationError("challenging meta review requires findings")
        if any(not isinstance(item, ReviewFinding) for item in findings):
            raise ContractValidationError("findings must contain ReviewFinding values")
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "context_fingerprint", _text(self.context_fingerprint, "context_fingerprint"))
        if not isinstance(self.agrees_with_arbiter, bool) or not isinstance(self.isolated, bool):
            raise ContractValidationError("meta review flags must be booleans")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer_id": self.reviewer_id,
            "round_number": self.round_number,
            "arbiter_id": self.arbiter_id,
            "verdict": self.verdict.value,
            "summary": self.summary,
            "findings": [item.to_dict() for item in self.findings],
            "context_fingerprint": self.context_fingerprint,
            "agrees_with_arbiter": self.agrees_with_arbiter,
            "isolated": self.isolated,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MetaReviewResult:
        data = dict(payload)
        findings = tuple(
            ReviewFinding.from_dict(item) for item in data.pop("findings", ())
        )
        return cls(findings=findings, **data)


@dataclass(frozen=True)
class DisputePacket:
    context_fingerprint: str
    round_number: int
    arbiter_summary: str
    arbiter_rationale: str
    reviewer_findings: tuple[ReviewFinding, ...]
    re_review_prompt: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_fingerprint", _text(self.context_fingerprint, "context_fingerprint"))
        if isinstance(self.round_number, bool) or not isinstance(self.round_number, int) or self.round_number < 1:
            raise ContractValidationError("round_number must be a positive integer")
        object.__setattr__(self, "arbiter_summary", _text(self.arbiter_summary, "arbiter_summary"))
        object.__setattr__(self, "arbiter_rationale", _text(self.arbiter_rationale, "arbiter_rationale"))
        findings = tuple(self.reviewer_findings)
        if any(not isinstance(item, ReviewFinding) for item in findings):
            raise ContractValidationError("reviewer_findings must contain ReviewFinding values")
        object.__setattr__(self, "reviewer_findings", findings)
        object.__setattr__(self, "re_review_prompt", _text(self.re_review_prompt, "re_review_prompt"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_fingerprint": self.context_fingerprint,
            "round_number": self.round_number,
            "arbiter_summary": self.arbiter_summary,
            "arbiter_rationale": self.arbiter_rationale,
            "reviewer_findings": [item.to_dict() for item in self.reviewer_findings],
            "re_review_prompt": self.re_review_prompt,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DisputePacket:
        data = dict(payload)
        findings = tuple(
            ReviewFinding.from_dict(item) for item in data.pop("reviewer_findings", ())
        )
        return cls(reviewer_findings=findings, **data)


@dataclass(frozen=True)
class DecisionReport:
    status: ReviewOutcome
    context_fingerprint: str
    rounds: int
    summary: str
    arbiter_summary: str
    meta_review_summary: str
    reviewer_findings: tuple[ReviewFinding, ...]
    stop_reason: str
    required_user_decision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _enum(self.status, ReviewOutcome, "status"))
        object.__setattr__(self, "context_fingerprint", _text(self.context_fingerprint, "context_fingerprint"))
        if isinstance(self.rounds, bool) or not isinstance(self.rounds, int) or self.rounds < 1:
            raise ContractValidationError("rounds must be a positive integer")
        for field_name in (
            "summary",
            "arbiter_summary",
            "meta_review_summary",
            "stop_reason",
            "required_user_decision",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        findings = tuple(self.reviewer_findings)
        if any(not isinstance(item, ReviewFinding) for item in findings):
            raise ContractValidationError("reviewer_findings must contain ReviewFinding values")
        object.__setattr__(self, "reviewer_findings", findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "context_fingerprint": self.context_fingerprint,
            "rounds": self.rounds,
            "summary": self.summary,
            "arbiter_summary": self.arbiter_summary,
            "meta_review_summary": self.meta_review_summary,
            "reviewer_findings": [item.to_dict() for item in self.reviewer_findings],
            "stop_reason": self.stop_reason,
            "required_user_decision": self.required_user_decision,
        }


@dataclass(frozen=True)
class ReviewServiceResult:
    outcome: ReviewOutcome
    round_number: int
    reviewer_results: tuple[ReviewerResult, ...]
    next_state: LifecycleState | None = None
    arbiter_result: ArbiterResult | None = None
    meta_review_result: MetaReviewResult | None = None
    dispute_packet: DisputePacket | None = None
    decision_report: DecisionReport | None = None
    required_action: str | None = None
    agent_bindings: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "round_number": self.round_number,
            "reviewer_ids": [item.reviewer_id for item in self.reviewer_results],
            "verdicts": [item.verdict.value for item in self.reviewer_results],
            "next_state": None if self.next_state is None else self.next_state.value,
            "arbiter": None if self.arbiter_result is None else self.arbiter_result.summary,
            "meta_review": None if self.meta_review_result is None else self.meta_review_result.summary,
            "has_dispute_packet": self.dispute_packet is not None,
            "decision_report": None if self.decision_report is None else self.decision_report.to_dict(),
            "required_action": self.required_action,
            "agent_bindings": [dict(item) for item in self.agent_bindings],
        }


def required_reviewers(risk: ReviewRisk) -> int:
    if not isinstance(risk, ReviewRisk):
        try:
            risk = ReviewRisk(risk)
        except (TypeError, ValueError) as exc:
            raise ContractValidationError("risk is invalid") from exc
    return _REVIEWERS_BY_RISK[risk]


def _validate_round(
    risk: ReviewRisk,
    results: Sequence[ReviewerResult],
    *,
    round_number: int,
    max_rounds: int,
    context: ReviewContext | None = None,
) -> tuple[ReviewerResult, ...]:
    required = required_reviewers(risk)
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 2:
        raise ContractValidationError("max_rounds must be 1 or 2")
    if not 1 <= round_number <= max_rounds:
        raise ContractValidationError("round_number is outside the configured review rounds")
    values = tuple(results)
    if required == 0:
        if values:
            raise ContractValidationError("LOW risk does not require review results")
        return values
    if len(values) != required:
        raise ContractValidationError(f"{risk.value} risk requires {required} reviewer results")
    if len({item.reviewer_id for item in values}) != len(values):
        raise ContractValidationError("reviewer IDs must be unique within a round")
    if any(item.round_number != round_number for item in values):
        raise ContractValidationError("reviewer round_number does not match the review round")
    if any(not item.isolated for item in values):
        raise ContractValidationError("first-party reviewer results must be isolated")
    if context is not None and any(item.context_fingerprint != context.fingerprint for item in values):
        raise ContractValidationError("all reviewers must use the exact same ReviewContext")
    return values


def evaluate_review_round(
    risk: ReviewRisk,
    results: Sequence[ReviewerResult],
    *,
    round_number: int,
    max_rounds: int = 2,
    context: ReviewContext | None = None,
) -> ReviewServiceResult:
    """Aggregate one isolated round without invoking or recursively reviewing agents."""

    values = _validate_round(
        risk,
        results,
        round_number=round_number,
        max_rounds=max_rounds,
        context=context,
    )
    if not values:
        return ReviewServiceResult(ReviewOutcome.PASS, round_number, values)

    verdicts = {item.verdict for item in values}
    if verdicts == {ReviewVerdict.PASS}:
        return ReviewServiceResult(ReviewOutcome.PASS, round_number, values)
    if verdicts <= {ReviewVerdict.PASS, ReviewVerdict.WARN} and ReviewVerdict.WARN in verdicts:
        return ReviewServiceResult(ReviewOutcome.WARN, round_number, values)
    if len(verdicts) == 1 and verdicts == {ReviewVerdict.FAIL}:
        return ReviewServiceResult(
            ReviewOutcome.HUMAN_DECISION_REQUIRED,
            round_number,
            values,
            LifecycleState.HUMAN_DECISION_REQUIRED,
        )
    if round_number < max_rounds:
        return ReviewServiceResult(ReviewOutcome.REVIEW_REQUIRED, round_number, values)
    return ReviewServiceResult(
        ReviewOutcome.HUMAN_DECISION_REQUIRED,
        round_number,
        values,
        LifecycleState.HUMAN_DECISION_REQUIRED,
    )


def evaluate_review_cycle(
    context: ReviewContext,
    risk: ReviewRisk,
    reviewer_results: Sequence[ReviewerResult],
    arbiter_result: ArbiterResult,
    meta_review_result: MetaReviewResult | None,
    *,
    round_number: int,
    max_rounds: int = 2,
) -> ReviewServiceResult:
    """Evaluate one reviewer -> arbiter -> meta-review cycle."""

    if not isinstance(context, ReviewContext):
        raise ContractValidationError("context must be a ReviewContext")
    values = _validate_round(
        risk,
        reviewer_results,
        round_number=round_number,
        max_rounds=max_rounds,
        context=context,
    )
    if not isinstance(arbiter_result, ArbiterResult):
        raise ContractValidationError("arbiter_result is required")
    if arbiter_result.round_number != round_number or arbiter_result.context_fingerprint != context.fingerprint:
        raise ContractValidationError("arbiter result does not match the review context and round")
    if set(arbiter_result.reviewer_ids) != {item.reviewer_id for item in values}:
        raise ContractValidationError("arbiter must summarize exactly this reviewer round")
    if meta_review_result is None:
        return ReviewServiceResult(
            ReviewOutcome.REVIEW_REQUIRED,
            round_number,
            values,
            arbiter_result=arbiter_result,
            required_action="obtain an independent MetaReviewer result for the arbiter conclusion",
        )
    if not isinstance(meta_review_result, MetaReviewResult):
        raise ContractValidationError("meta_review_result must be a MetaReviewResult")
    if (
        meta_review_result.round_number != round_number
        or meta_review_result.arbiter_id != arbiter_result.arbiter_id
        or meta_review_result.context_fingerprint != context.fingerprint
        or not meta_review_result.isolated
    ):
        raise ContractValidationError("meta review does not match the arbiter, context and round")

    # HIGH/CRITICAL convergence additionally requires the meta reviewer's
    # verdict to match the arbiter's: a "agrees=true" flag next to a different
    # verdict is an unconfirmed arbiter judgment, not a converged one.
    verdict_mismatch = (
        risk in (ReviewRisk.HIGH, ReviewRisk.CRITICAL)
        and meta_review_result.verdict is not arbiter_result.verdict
    )
    dispute = (
        arbiter_result.disagrees_with_reviewers
        or any(item.verdict is not arbiter_result.verdict for item in values)
        or not meta_review_result.agrees_with_arbiter
        or verdict_mismatch
    )
    if dispute:
        if round_number < max_rounds:
            packet = DisputePacket(
                context_fingerprint=context.fingerprint,
                round_number=round_number,
                arbiter_summary=arbiter_result.summary,
                arbiter_rationale=arbiter_result.rationale,
                reviewer_findings=tuple(finding for item in values for finding in item.findings),
                re_review_prompt=(
                    "Re-review the exact same ReviewContext independently. Compare the original "
                    "evidence with the arbiter rationale; do not inherit any prior verdict."
                ),
            )
            return ReviewServiceResult(
                ReviewOutcome.REVIEW_REQUIRED,
                round_number,
                values,
                arbiter_result=arbiter_result,
                meta_review_result=meta_review_result,
                dispute_packet=packet,
                required_action="run the next isolated review round with the DisputePacket",
            )
        report = DecisionReport(
            status=ReviewOutcome.STOPPED,
            context_fingerprint=context.fingerprint,
            rounds=round_number,
            summary="Review did not converge after the configured rounds.",
            arbiter_summary=arbiter_result.summary,
            meta_review_summary=meta_review_result.summary,
            reviewer_findings=tuple(finding for item in values for finding in item.findings),
            stop_reason="arbiter disagreement or meta-review challenge remained unresolved",
            required_user_decision="补充证据、接受当前风险或选择其他方案",
        )
        return ReviewServiceResult(
            ReviewOutcome.STOPPED,
            round_number,
            values,
            LifecycleState.STOPPED,
            arbiter_result,
            meta_review_result,
            decision_report=report,
        )

    if arbiter_result.verdict is ReviewVerdict.PASS and meta_review_result.verdict is ReviewVerdict.PASS:
        outcome = ReviewOutcome.PASS
    elif arbiter_result.verdict is ReviewVerdict.WARN or meta_review_result.verdict is ReviewVerdict.WARN:
        outcome = ReviewOutcome.WARN
    else:
        outcome = ReviewOutcome.HUMAN_DECISION_REQUIRED
    next_state = (
        LifecycleState.HUMAN_DECISION_REQUIRED
        if outcome is ReviewOutcome.HUMAN_DECISION_REQUIRED
        else None
    )
    return ReviewServiceResult(
        outcome,
        round_number,
        values,
        next_state,
        arbiter_result,
        meta_review_result,
    )


def run_review_cycle(
    context: ReviewContext,
    risk: ReviewRisk,
    reviewer_call: Callable[[ReviewContext, str, int, DisputePacket | None], ReviewerResult],
    arbiter_call: Callable[
        [ReviewContext, tuple[ReviewerResult, ...], int, DisputePacket | None], ArbiterResult
    ],
    meta_review_call: Callable[
        [ReviewContext, tuple[ReviewerResult, ...], ArbiterResult, int], MetaReviewResult
    ],
    *,
    max_rounds: int = 2,
    reviewer_batch_call: Callable[
        [ReviewContext, tuple[str, ...], int, DisputePacket | None],
        Sequence[ReviewerResult],
    ] | None = None,
) -> ReviewServiceResult:
    """Run injected agent callbacks with one immutable context and bounded rounds.

    The Meta Reviewer receives the round's reviewer results alongside the
    arbiter conclusion so it can verify the arbiter's summary against the
    actual reviewer output instead of trusting it.
    """

    if not isinstance(context, ReviewContext):
        raise ContractValidationError("context must be a ReviewContext")
    required = required_reviewers(risk)
    if required == 0:
        return ReviewServiceResult(ReviewOutcome.PASS, 1, ())
    dispute: DisputePacket | None = None
    for round_number in range(1, max_rounds + 1):
        reviewer_ids = tuple(f"reviewer-{index + 1}" for index in range(required))
        reviewer_results = (
            tuple(reviewer_batch_call(context, reviewer_ids, round_number, dispute))
            if reviewer_batch_call is not None
            else tuple(
                reviewer_call(context, reviewer_id, round_number, dispute)
                for reviewer_id in reviewer_ids
            )
        )
        if any(
            not isinstance(item, ReviewerResult)
            or item.reviewer_id != f"reviewer-{index + 1}"
            or item.context_fingerprint != context.fingerprint
            for index, item in enumerate(reviewer_results)
        ):
            raise ContractValidationError(
                "reviewer callbacks must return the requested ID and exact ReviewContext fingerprint"
            )
        arbiter_result = arbiter_call(context, reviewer_results, round_number, dispute)
        meta_review_result = meta_review_call(context, reviewer_results, arbiter_result, round_number)
        result = evaluate_review_cycle(
            context,
            risk,
            reviewer_results,
            arbiter_result,
            meta_review_result,
            round_number=round_number,
            max_rounds=max_rounds,
        )
        if result.outcome is not ReviewOutcome.REVIEW_REQUIRED:
            return result
        dispute = result.dispute_packet
    raise ContractValidationError("review cycle exhausted without a terminal result")


__all__ = [
    "ArbiterResult",
    "DecisionReport",
    "DisputePacket",
    "MetaReviewResult",
    "ReviewContext",
    "ReviewOutcome",
    "ReviewRisk",
    "ReviewServiceResult",
    "ReviewVerdict",
    "ReviewerResult",
    "evaluate_review_cycle",
    "evaluate_review_round",
    "required_reviewers",
    "run_review_cycle",
]
