"""Proof binding tests use synthetic signed sources, never native host evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.review_delivery import build_packets
from goalanchor.review_proof import ReviewProofExpired, normalize_review_proof
from goalanchor.review_service import ArbiterResult, MetaReviewResult, ReviewContext, ReviewerResult
from goalanchor.workflow_protocol import EventAuthority, canonical, fingerprint

NOW = "2026-10-25T10:00:00+00:00"
ISSUED = "2026-10-25T09:59:00+00:00"
EXPIRES = "2026-10-25T10:30:00+00:00"
SOURCE_TIME = "2026-10-25T09:58:00+00:00"
POLICY = "a" * 64
RUNTIME = "b" * 64
SKILL = "c" * 64
HOST_FACTS = {"host": "zcode", "user_agent": "test-zcode/0.17", "model": "test-model", "mode": "test-only",
              "hook_enabled": False, "hook_trust": "synthetic-controller"}
HOST = canonical(HOST_FACTS)


def _case():
    context = ReviewContext(
        goal="test-only proof binding", prompt="review frozen material", constraints=("no invented evidence",),
        evidence_refs=("E1",), candidate="candidate", evidence_digests=(("E1", "d" * 64),),
        strict_binding={"policy_digest": POLICY, "runtime_binding_digest": RUNTIME},
    )
    reviewers = [ReviewerResult(name, 1, "PASS", context_fingerprint=context.fingerprint).to_dict()
                 for name in ("r1", "r2")]
    arbiter = ArbiterResult("arbiter", 1, "PASS", "summary", "reason", ("r1", "r2"),
                            context.fingerprint, False).to_dict()
    meta = MetaReviewResult("meta", 1, "arbiter", "PASS", "summary", (), context.fingerprint, True).to_dict()
    reports = {"r1": reviewers[0], "r2": reviewers[1], "arbiter": arbiter, "meta": meta}
    bundle = {
        "context": context.to_dict(), "risk": "MEDIUM", "round_number": 1, "max_rounds": 2,
        "reviewer_results": reviewers, "arbiter": arbiter, "meta_review": meta, "dispositions": {},
        "skill_digest": SKILL,
        "bindings": [{"agent_id": name, "session_id": "session-" + name, "model": "test-model"}
                     for name in reports],
    }
    packets = build_packets(context, bundle, [])
    bundle["delivery_receipts"] = [
        {"agent_id": name, "session_id": "session-" + name, "input_digest": fingerprint(packets[name]),
         "transport_receipt_id": "event-" + name, "status": "DELIVERED"} for name in reports
    ]
    authority = EventAuthority(b"synthetic-unit-test-key" * 2, "test-capture")
    roles = []
    for name, report in reports.items():
        payload = {
            "agent_id": name, "session_id": "session-" + name, "model": "test-model",
            "input_id": "input-" + name, "turn_id": "turn-" + name,
            "packet": packets[name], "response": canonical(report),
        }
        event = authority.sign("host.review.completed", context.fingerprint, payload,
                               event_id="event-" + name, at=SOURCE_TIME).to_dict()
        roles.append({
            **{key: payload[key] for key in ("agent_id", "session_id", "model", "input_id", "turn_id")},
            "input_digest": fingerprint(packets[name]), "result_digest": fingerprint(report),
            "source_event_id": event["event_id"], "source_event_digest": fingerprint(event), "source_event": event,
        })
    proof = {
        "schema_version": "goalanchor.review-proof/v1", "review_digest": fingerprint(bundle),
        "context_fingerprint": context.fingerprint, "policy_digest": POLICY, "host_binding": HOST,
        "runtime_binding_digest": RUNTIME, "round_number": 1, "skill_digest": SKILL,
        "issued_at": ISSUED, "expires_at": EXPIRES, "roles": roles,
    }
    _seal(proof)
    return context, bundle, packets, proof


def _seal(proof):
    proof["proof_digest"] = fingerprint({key: value for key, value in proof.items() if key != "proof_digest"})


def _normalize(case, *, now=NOW, **changes):
    context, bundle, packets, proof = case
    kwargs = {"expected_context": context, "expected_policy_digest": POLICY,
              "expected_host_binding": HOST, "expected_packets": packets, "now": now, **changes}
    return normalize_review_proof(proof, bundle, **kwargs)


def test_valid_complete_proof_is_copied_without_mutating_inputs():
    case = _case()
    before = deepcopy(case[1:])
    result = _normalize(case)
    assert result == before[-1]
    result["roles"][0]["source_event"]["payload"]["packet"]["context"]["goal"] = "changed"
    result["roles"].pop()
    assert case[1:] == before


@pytest.mark.parametrize("field", [
    "schema_version", "review_digest", "context_fingerprint", "policy_digest", "host_binding",
    "runtime_binding_digest", "round_number", "skill_digest", "issued_at", "expires_at", "roles", "proof_digest",
])
def test_every_proof_field_is_required(field):
    case = _case()
    del case[3][field]
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", "goalanchor.review-proof/v2"), ("review_digest", "0" * 64),
    ("context_fingerprint", "0" * 64), ("policy_digest", "0" * 64), ("host_binding", None),
    ("runtime_binding_digest", "0" * 64), ("skill_digest", "0" * 64),
    ("round_number", 2), ("round_number", True), ("round_number", 1.0),
    ("round_number", 0), ("issued_at", "2026-10-25T10:01:00+00:00"),
    ("issued_at", "2026-10-25T09:59:00"), ("expires_at", "broken"),
    ("roles", []), ("roles", {}), ("extra", "unsupported"),
])
def test_tampered_proof_fields_cannot_be_resealed_to_pass(field, value):
    case = _case()
    case[3][field] = value
    _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case)


def test_proof_digest_is_checked():
    case = _case()
    case[3]["proof_digest"] = "0" * 64
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize("now", [EXPIRES, "2026-10-25T11:00:00+00:00"])
def test_expiry_is_distinct_and_includes_exact_deadline(now):
    with pytest.raises(ReviewProofExpired):
        _normalize(_case(), now=now)


@pytest.mark.parametrize("field", ["review_digest", "context_fingerprint", "policy_digest",
                                  "runtime_binding_digest", "skill_digest", "proof_digest"])
@pytest.mark.parametrize("value", [True, 1, "A" * 64, "a" * 63, " a" * 32])
def test_digest_types_and_lowercase_are_exact(field, value):
    case = _case()
    case[3][field] = value
    if field != "proof_digest":
        _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize("field", ["agent_id", "session_id", "model", "input_digest", "result_digest",
                                  "input_id", "turn_id", "source_event_id", "source_event_digest", "source_event"])
def test_every_role_field_is_required(field):
    case = _case()
    del case[3]["roles"][0][field]
    _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize("field", ["agent_id", "session_id", "model", "input_digest", "result_digest",
                                  "input_id", "turn_id", "source_event_id", "source_event_digest", "extra"])
def test_each_role_binding_is_verified(field):
    case = _case()
    case[3]["roles"][0][field] = "0" * 64
    _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "duplicate-session", "duplicate-input",
                                     "duplicate-turn", "duplicate-event"])
def test_role_coverage_and_unique_source_identity(mutation):
    case = _case()
    roles = case[3]["roles"]
    if mutation == "missing":
        roles.pop()
    elif mutation in {"extra", "duplicate"}:
        roles.append(deepcopy(roles[0]))
    else:
        field = {"duplicate-session": "session_id", "duplicate-input": "input_id",
                 "duplicate-turn": "turn_id", "duplicate-event": "source_event_id"}[mutation]
        roles[1][field] = roles[0][field]
        if field != "source_event_id":
            roles[1]["source_event"]["payload"][field] = roles[0][field]
        else:
            roles[1]["source_event"]["event_id"] = roles[0][field]
        roles[1]["source_event_digest"] = fingerprint(roles[1]["source_event"])
    _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize(("field", "value"), [
    ("kind", "observer.review"), ("subject", "wrong-context"), ("event_id", "foreign-event"),
    ("source", ""), ("signature", "A" * 64), ("signature", True),
    ("created_at", "2026-10-25T09:59:01+00:00"), ("extra", 1),
])
def test_source_event_structure_and_binding(field, value):
    case = _case()
    role = case[3]["roles"][0]
    role["source_event"][field] = value
    role["source_event_digest"] = fingerprint(role["source_event"])
    _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize("field", ["agent_id", "session_id", "model", "input_id", "turn_id", "packet", "response"])
@pytest.mark.parametrize("remove", [False, True])
def test_source_payload_is_complete_and_matches_role(field, remove):
    case = _case()
    role = case[3]["roles"][0]
    if remove:
        del role["source_event"]["payload"][field]
    else:
        role["source_event"]["payload"][field] = "changed"
    role["source_event_digest"] = fingerprint(role["source_event"])
    _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize("mutation", ["extra", "round-bool", "isolated-int", "duplicate-key", "trailing", "not-text"])
def test_raw_response_must_decode_to_the_complete_exact_report(mutation):
    case = _case()
    role = case[3]["roles"][0]
    report = deepcopy(case[1]["reviewer_results"][0])
    if mutation == "extra":
        report["extra"] = "unsupported"
    elif mutation == "round-bool":
        report["round_number"] = True
    elif mutation == "isolated-int":
        report["isolated"] = 1
    raw = canonical(report)
    if mutation == "duplicate-key":
        raw = raw[:-1] + ',"verdict":"PASS"}'
    elif mutation == "trailing":
        raw += '{}'
    role["source_event"]["payload"]["response"] = report if mutation == "not-text" else raw
    role["source_event_digest"] = fingerprint(role["source_event"])
    _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize("mutation", ["context", "round", "binding-session", "binding-model", "missing-binding",
                                     "receipt-session", "receipt-digest", "receipt-status", "receipt-source",
                                     "missing-receipt", "duplicate-receipt", "duplicate-report", "skill-missing"])
def test_resealed_review_still_must_match_all_expected_dependencies(mutation):
    case = _case()
    bundle, proof = case[1], case[3]
    if mutation == "context":
        bundle["context"]["goal"] = "other goal"
    elif mutation == "round":
        bundle["round_number"] = True
    elif mutation.startswith("binding-"):
        bundle["bindings"][0]["session_id" if mutation.endswith("session") else "model"] = "changed"
    elif mutation == "missing-binding":
        bundle["bindings"].pop()
    elif mutation == "missing-receipt":
        bundle["delivery_receipts"].pop()
    elif mutation == "duplicate-receipt":
        bundle["delivery_receipts"].append(deepcopy(bundle["delivery_receipts"][0]))
    elif mutation == "duplicate-report":
        bundle["reviewer_results"].append(deepcopy(bundle["reviewer_results"][0]))
    elif mutation == "skill-missing":
        del bundle["skill_digest"]
    else:
        field = {"receipt-session": "session_id", "receipt-digest": "input_digest",
                 "receipt-status": "status", "receipt-source": "transport_receipt_id"}[mutation]
        bundle["delivery_receipts"][0][field] = "changed"
    proof["review_digest"] = fingerprint(bundle)
    _seal(proof)
    with pytest.raises(ContractValidationError):
        _normalize(case)


@pytest.mark.parametrize("binding", [None, {}, {"policy_digest": POLICY},
                                     {"policy_digest": "0" * 64, "runtime_binding_digest": RUNTIME}])
def test_expected_context_requires_policy_and_runtime_binding(binding):
    case = _case()
    with pytest.raises(ContractValidationError):
        _normalize(case, expected_context=replace(case[0], strict_binding=binding))


def test_expected_context_policy_host_and_packet_are_not_caller_rebindable():
    case = _case()
    for replacement in ({"expected_policy_digest": "0" * 64}, {"expected_host_binding": None},
                        {"expected_context": replace(case[0], candidate="other")}, {"expected_packets": {}}):
        with pytest.raises(ContractValidationError):
            _normalize(case, **replacement)


def test_packet_equality_distinguishes_bool_from_int_even_after_digest_refresh():
    case = _case()
    role = case[3]["roles"][0]
    role["source_event"]["payload"]["packet"] = deepcopy(role["source_event"]["payload"]["packet"])
    role["source_event"]["payload"]["packet"]["round_number"] = True
    role["input_digest"] = fingerprint(role["source_event"]["payload"]["packet"])
    role["source_event_digest"] = fingerprint(role["source_event"])
    _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case)


def test_pure_normalizer_does_not_claim_to_verify_authority_signature():
    case = _case()
    role = case[3]["roles"][0]
    role["source_event"]["signature"] = "0" * 64  # Structurally valid; verifier must authenticate it upstream.
    role["source_event_digest"] = fingerprint(role["source_event"])
    _seal(case[3])
    assert _normalize(case) == case[3]


def test_uniformly_rebinding_every_role_to_another_model_still_fails_host_binding():
    case = _case()
    bundle, proof = case[1], case[3]
    for binding in bundle["bindings"]:
        binding["model"] = "other-model"
    authority = EventAuthority(b"synthetic-unit-test-key" * 2, "test-capture")
    for role in proof["roles"]:
        role["model"] = "other-model"
        source = role["source_event"]
        source["payload"]["model"] = "other-model"
        role["source_event"] = authority.sign(
            source["kind"], source["subject"], source["payload"],
            event_id=source["event_id"], at=source["created_at"],
        ).to_dict()
        role["source_event_digest"] = fingerprint(role["source_event"])
    proof["review_digest"] = fingerprint(bundle)
    _seal(proof)
    with pytest.raises(ContractValidationError, match="model"):
        _normalize(case)


@pytest.mark.parametrize("host", [None, "", '{"host":"zcode","model":"test-model"}',
                                  canonical({**HOST_FACTS, "hook_enabled": 1}),
                                  canonical({**HOST_FACTS, "host": "unknown"}),
                                  canonical({**HOST_FACTS, "model": True}),
                                  " " + HOST])
def test_even_consistent_host_bindings_require_complete_canonical_host_contract(host):
    case = _case()
    case[3]["host_binding"] = host
    _seal(case[3])
    with pytest.raises(ContractValidationError):
        _normalize(case, expected_host_binding=host)
