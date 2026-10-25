"""Pure, versioned review-proof binding checks; not an authentication provider.

The trusted composition root must authenticate every embedded source event and
its controller/host/account provenance before supplying this proof. This module
has no authority key, performs no I/O, and cannot authenticate a signature or
prove that a Skill was loaded. A matching digest or an ``isolated`` claim is not
native proof. Callers must additionally protect persisted proofs against replay,
expiry extension and verifier-mode downgrade.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .core import ContractValidationError
from .host_contract import validate_host_binding
from .review_delivery import validate_receipts
from .review_service import ReviewContext
from .workflow_protocol import SignedEvent, canonical, fingerprint, parse_time, shape, strict_loads

_FIELDS = {
    "schema_version", "review_digest", "context_fingerprint", "policy_digest", "host_binding",
    "runtime_binding_digest", "round_number", "skill_digest", "issued_at", "expires_at", "roles", "proof_digest",
}
_DIGEST_FIELDS = {
    "review_digest", "context_fingerprint", "policy_digest", "runtime_binding_digest", "skill_digest", "proof_digest",
}
_ROLE_FIELDS = {
    "agent_id", "session_id", "model", "input_digest", "result_digest", "input_id", "turn_id",
    "source_event_id", "source_event_digest", "source_event",
}
_SOURCE_PAYLOAD_FIELDS = {"agent_id", "session_id", "model", "input_id", "turn_id", "packet", "response"}
_UNIQUE_ROLE_FIELDS = ("agent_id", "session_id", "input_id", "turn_id", "source_event_id")


class ReviewProofExpired(ContractValidationError):
    """A structurally bound review proof has reached its persisted deadline."""


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ContractValidationError("review proof digests must be lowercase SHA-256 text")
    return value


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ContractValidationError("review proof identities must be nonempty canonical text")
    return value


def _round(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ContractValidationError("review proof round must be a positive integer")
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ContractValidationError("review proof arrays must be JSON lists")
    return value


def _same(left: Any, right: Any) -> bool:
    # Python equality would conflate True with 1, including inside packets.
    return canonical(left) == canonical(right)


def _reports(bundle: dict[str, Any], context: ReviewContext, round_number: int) -> dict[str, dict[str, Any]]:
    reviewers = _array(bundle["reviewer_results"])
    if not reviewers:
        raise ContractValidationError("review proof requires reviewer reports")
    candidates = [(item, "reviewer_id") for item in reviewers]
    candidates += [(bundle["arbiter"], "arbiter_id"), (bundle["meta_review"], "reviewer_id")]
    reports: dict[str, dict[str, Any]] = {}
    for raw, id_field in candidates:
        if not isinstance(raw, dict):
            raise ContractValidationError("review proof report must be an object")
        agent_id = _identity(raw[id_field])
        if (agent_id in reports or _round(raw["round_number"]) != round_number
                or raw.get("context_fingerprint") != context.fingerprint):
            raise ContractValidationError("review proof reports have duplicate or mismatched identities")
        reports[agent_id] = raw
    return reports


def _check_source(
    role: dict[str, Any], report: dict[str, Any], packet: dict[str, Any], context: ReviewContext, issued_at: str,
) -> None:
    raw = role["source_event"]
    if not isinstance(raw, dict):
        raise ContractValidationError("review proof source event must be an object")
    _digest(raw.get("signature"))
    event = SignedEvent.from_dict(raw)
    if (not _same(event.to_dict(), raw) or fingerprint(raw) != role["source_event_digest"]
            or event.event_id != role["source_event_id"] or event.kind != "host.review.completed"
            or event.subject != context.fingerprint):
        raise ContractValidationError("review proof source event binding mismatch")
    if parse_time(event.created_at) > parse_time(issued_at):
        raise ContractValidationError("review proof predates its source event")
    payload = shape(event.payload, _SOURCE_PAYLOAD_FIELDS)
    if any(payload[field] != role[field] for field in ("agent_id", "session_id", "model", "input_id", "turn_id")):
        raise ContractValidationError("review proof source identity mismatch")
    if not _same(payload["packet"], packet) or role["input_digest"] != fingerprint(packet):
        raise ContractValidationError("review proof source packet mismatch")
    response = payload["response"]
    if not isinstance(response, str):
        raise ContractValidationError("review proof raw response must be JSON text")
    if not _same(strict_loads(response), report) or role["result_digest"] != fingerprint(report):
        raise ContractValidationError("review proof raw response does not match the complete report")


def normalize_review_proof(
    proof: Any,
    bundle: dict[str, Any],
    *,
    expected_context: ReviewContext,
    expected_policy_digest: str,
    expected_host_binding: str | None,
    expected_packets: dict[str, dict[str, Any]],
    now: str,
) -> dict[str, Any]:
    """Validate immutable claims and return an independent canonical JSON copy.

    Source signatures are checked for wire shape only. The trusted proof provider
    owns cryptographic verification and provenance. No timestamp is refreshed.
    """
    try:
        return _normalize(proof, bundle, expected_context, expected_policy_digest,
                          expected_host_binding, expected_packets, now)
    except ContractValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
        raise ContractValidationError("malformed review proof or binding material") from exc


def _normalize(
    proof: Any, bundle: dict[str, Any], context: ReviewContext, policy_digest: str,
    host_binding: str | None, packets: dict[str, dict[str, Any]], now: str,
) -> dict[str, Any]:
    data = shape(proof, _FIELDS)
    if data["schema_version"] != "goalanchor.review-proof/v1":
        raise ContractValidationError("unsupported review proof schema")
    for field in _DIGEST_FIELDS:
        _digest(data[field])
    _digest(policy_digest)
    if not isinstance(host_binding, str) or validate_host_binding(host_binding) != host_binding:
        raise ContractValidationError("review proof expected host binding is invalid")
    host_model = _identity(strict_loads(host_binding)["model"])
    binding = context.strict_binding
    if not isinstance(binding, Mapping):
        raise ContractValidationError("review proof requires a Strict context binding")
    runtime = _digest(binding.get("runtime_binding_digest"))
    if _digest(binding.get("policy_digest")) != policy_digest:
        raise ContractValidationError("review proof expected policy differs from its context")
    round_number = _round(data["round_number"])
    if (not _same(bundle["context"], context.to_dict()) or data["context_fingerprint"] != context.fingerprint
            or data["policy_digest"] != policy_digest or data["host_binding"] != host_binding
            or data["runtime_binding_digest"] != runtime or _round(bundle["round_number"]) != round_number
            or data["skill_digest"] != _digest(bundle["skill_digest"])):
        raise ContractValidationError("review proof context, round, host or Skill binding mismatch")
    body = {key: value for key, value in data.items() if key != "proof_digest"}
    if data["review_digest"] != fingerprint(bundle) or data["proof_digest"] != fingerprint(body):
        raise ContractValidationError("review proof content digest mismatch")
    issued, expires, current = (parse_time(data["issued_at"]), parse_time(data["expires_at"]), parse_time(now))
    if issued > current or expires <= issued:
        raise ContractValidationError("review proof issuance or validity interval is invalid")
    if current >= expires:
        raise ReviewProofExpired("review proof expired")

    reports = _reports(bundle, context, round_number)
    if not isinstance(packets, dict) or set(packets) != set(reports):
        raise ContractValidationError("review proof packets must cover all roles exactly")
    validate_receipts(bundle, packets)
    bindings: dict[str, dict[str, Any]] = {}
    for raw in _array(bundle["bindings"]):
        entry = shape(raw, {"agent_id", "session_id", "model"})
        for field in entry:
            _identity(entry[field])
        if entry["agent_id"] in bindings:
            raise ContractValidationError("review proof role binding is duplicated")
        bindings[entry["agent_id"]] = entry
    receipts = {item["agent_id"]: item for item in bundle["delivery_receipts"]}
    if set(bindings) != set(reports) or set(receipts) != set(reports):
        raise ContractValidationError("review proof bindings and receipts must cover all roles exactly")

    seen: dict[str, set[str]] = {field: set() for field in _UNIQUE_ROLE_FIELDS}
    for raw in _array(data["roles"]):
        role = shape(raw, _ROLE_FIELDS)
        for field in (*_UNIQUE_ROLE_FIELDS, "model"):
            _identity(role[field])
        if role["model"] != host_model:
            raise ContractValidationError("review proof role model differs from the host binding")
        for field in ("input_digest", "result_digest", "source_event_digest"):
            _digest(role[field])
        for field, values in seen.items():
            if role[field] in values:
                raise ContractValidationError("review proof roles must have independent source identities")
            values.add(role[field])
        agent_id = role["agent_id"]
        if agent_id not in reports:
            raise ContractValidationError("review proof contains an unexpected role")
        expected = bindings[agent_id]
        receipt = receipts[agent_id]
        if (any(role[field] != expected[field] for field in ("session_id", "model"))
                or role["source_event_id"] != receipt["transport_receipt_id"]
                or role["input_digest"] != _digest(receipt["input_digest"])):
            raise ContractValidationError("review proof role differs from delivery binding")
        packet = packets[agent_id]
        if (not isinstance(packet, dict) or packet.get("skill_digest") != data["skill_digest"]
                or not _same(packet.get("context"), context.to_dict())
                or _round(packet.get("round_number")) != round_number):
            raise ContractValidationError("review proof packet has an invalid context or Skill binding")
        _check_source(role, reports[agent_id], packet, context, data["issued_at"])
    if seen["agent_id"] != set(reports):
        raise ContractValidationError("review proof roles must cover every report")
    return shape(strict_loads(canonical(data)), _FIELDS)
