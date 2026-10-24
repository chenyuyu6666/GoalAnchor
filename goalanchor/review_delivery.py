"""Pure canonical review inputs and trusted-controller delivery checks.

Receipts attest transport delivery only when enclosed by the controller's signed
review event. They do not demonstrate a model's comprehension or independence.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .core import ContractValidationError
from .review_service import ReviewContext
from .workflow_protocol import fingerprint, shape, text

_QUESTIONS = (
    "Q1 Goal: Does the proposal preserve the final objective, constraints, blocker and completion condition?\n"
    "Q2 Logic: Is the problem -> action -> result or decision chain supported?\n"
    "Q3 Path: Are premises verified rather than stacked on unconfirmed conclusions?\n"
    "Q4 Minimum: Is this the minimum decision-relevant action for the blocking uncertainty?\n"
    "Q5 Structure: Does this avoid duplicate responsibility, hidden coupling and over-design?\n"
    "Answer each with yes/no and concrete evidence; preserve all important objections."
)
_PROMPTS = {
    "reviewer": "Independently review the same frozen objective and evidence.\n" + _QUESTIONS,
    "arbiter": "Assess every original reviewer objection against the frozen evidence.\n" + _QUESTIONS,
    "meta_reviewer": "Independently audit the arbiter using the original evidence and reviewer reports.\n" + _QUESTIONS,
}


def build_packets(
    context: ReviewContext, bundle: dict[str, Any], prior_rounds: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    skill_digest = bundle.get("skill_digest")
    if skill_digest is not None:
        if not isinstance(skill_digest, str) or len(skill_digest) != 64 or any(
            char not in "0123456789abcdef" for char in skill_digest
        ):
            raise ContractValidationError("skill_digest must be a lowercase SHA-256 digest")
    if ReviewContext.from_dict(bundle["context"]) != context:
        raise ContractValidationError("review packet context mismatch")
    reviewers = bundle["reviewer_results"]
    roles = [(report["reviewer_id"], "reviewer") for report in reviewers]
    roles += [(bundle["arbiter"]["arbiter_id"], "arbiter"), (bundle["meta_review"]["reviewer_id"], "meta_reviewer")]
    if len({name for name, _ in roles}) != len(roles):
        raise ContractValidationError("review packet roles must be distinct")
    packets = {}
    for name, role in roles:
        text(name, "agent_id")
        packet = {
            "role": role,
            "round_number": bundle["round_number"],
            "prompt": _PROMPTS[role],
            "context": context.to_dict(),
            "reviewer_results": [] if role == "reviewer" else reviewers,
            "arbiter": bundle["arbiter"] if role == "meta_reviewer" else None,
            "prior_rounds": prior_rounds,
            "dispositions": bundle["dispositions"],
        }
        if skill_digest is not None:
            packet["skill_digest"] = skill_digest
        packets[name] = deepcopy(packet)
    return packets


def validate_receipts(bundle: dict[str, Any], packets: dict[str, dict[str, Any]]) -> None:
    """Check signed attestation coverage and exact input/session association."""
    receipts = bundle.get("delivery_receipts")
    bindings = bundle.get("bindings")
    if not isinstance(receipts, list) or not isinstance(bindings, list):
        raise ContractValidationError("review delivery receipts and bindings required")
    expected: dict[str, str] = {}
    sessions: set[str] = set()
    for raw in bindings:
        binding = shape(raw, {"agent_id", "session_id"}, {"model"})
        agent_id = text(binding["agent_id"], "agent_id")
        session_id = text(binding["session_id"], "session_id")
        if agent_id in expected or session_id in sessions:
            raise ContractValidationError("delivery bindings must use unique agents and sessions")
        expected[agent_id] = session_id
        sessions.add(session_id)
    if len(expected) != len(bindings) or set(expected) != set(packets) or len(receipts) != len(expected):
        raise ContractValidationError("delivery bindings must cover all roles exactly")
    seen_agents: set[str] = set()
    seen_receipts: set[str] = set()
    for raw in receipts:
        receipt = shape(raw, {"agent_id", "session_id", "input_digest", "transport_receipt_id", "status"})
        agent_id = text(receipt["agent_id"], "agent_id")
        receipt_id = text(receipt["transport_receipt_id"], "transport_receipt_id")
        if (
            agent_id in seen_agents
            or receipt_id in seen_receipts
            or agent_id not in expected
            or receipt["session_id"] != expected[agent_id]
            or receipt["status"] != "DELIVERED"
            or receipt["input_digest"] != fingerprint(packets[agent_id])
        ):
            raise ContractValidationError("review delivery receipt does not match complete role input")
        seen_agents.add(agent_id)
        seen_receipts.add(receipt_id)
