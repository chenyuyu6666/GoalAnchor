"""Strict delivery attestation acceptance, not proof of a model's comprehension."""

from copy import deepcopy

import pytest
from test_workflow import NOW, setup_workflow

from goalanchor.core import ContractValidationError
from goalanchor.integration import ReviewFinding
from goalanchor.review_delivery import build_packets, validate_receipts
from goalanchor.review_service import ArbiterResult, MetaReviewResult, ReviewerResult, ReviewVerdict
from goalanchor.workflow_protocol import fingerprint


def delivery_bundle(runtime):
    ctx = runtime.review_context("G1")
    reviewers = [ReviewerResult(name, 1, ReviewVerdict.PASS, (), True, ctx.fingerprint) for name in ("A", "B")]
    arbiter = ArbiterResult(
        "arbiter", 1, ReviewVerdict.PASS, "checked", "raw evidence", ("A", "B"), ctx.fingerprint, False
    )
    meta = MetaReviewResult("meta", 1, "arbiter", ReviewVerdict.PASS, "raw material", (), ctx.fingerprint, True)
    return {
        "context": ctx.to_dict(),
        "risk": "HIGH",
        "round_number": 1,
        "max_rounds": 2,
        "reviewer_results": [item.to_dict() for item in reviewers],
        "arbiter": arbiter.to_dict(),
        "meta_review": meta.to_dict(),
        "dispositions": {},
        "bindings": [
            {"agent_id": name, "session_id": name + "-session", "model": "test-model"}
            for name in ("A", "B", "arbiter", "meta")
        ],
    }


def attest(runtime, bundle):
    packets = runtime.review_packets("G1", bundle)
    bundle["delivery_receipts"] = [
        {
            "agent_id": binding["agent_id"],
            "session_id": binding["session_id"],
            "input_digest": fingerprint(packets[binding["agent_id"]]),
            "transport_receipt_id": "delivered-" + binding["session_id"],
            "status": "DELIVERED",
        }
        for binding in bundle["bindings"]
    ]
    return packets


def submit(runtime, observer, bundle):
    return runtime.submit_review(
        "G1", bundle, observer.sign("observer.review", runtime.review_context("G1").fingerprint, bundle, at=NOW)
    )


def test_signed_report_without_delivery_attestation_cannot_pass(tmp_path):
    runtime, _, _, observer = setup_workflow(tmp_path)
    with pytest.raises(ContractValidationError):
        submit(runtime, observer, delivery_bundle(runtime))
    assert runtime.store.get("review", "G1") is None


def test_packets_contain_raw_material_and_same_reviewer_input(tmp_path):
    runtime, _, _, observer = setup_workflow(tmp_path)
    bundle = delivery_bundle(runtime)
    packets = attest(runtime, bundle)
    assert packets["A"] == packets["B"]
    assert packets["meta"]["context"] == bundle["context"]
    assert packets["meta"]["reviewer_results"] == bundle["reviewer_results"]
    assert packets["meta"]["arbiter"] == bundle["arbiter"]
    assert packets["arbiter"]["reviewer_results"] == bundle["reviewer_results"]
    assert submit(runtime, observer, bundle)["status"] == "PASSED"

def test_optional_skill_digest_is_bound_into_every_packet(tmp_path):
    runtime, _, _, _ = setup_workflow(tmp_path)
    bundle = delivery_bundle(runtime)
    bundle["skill_digest"] = "a" * 64
    packets = runtime.review_packets("G1", bundle)
    assert {packet["skill_digest"] for packet in packets.values()} == {"a" * 64}
    legacy_packets = runtime.review_packets("G1", delivery_bundle(runtime))
    assert all("skill_digest" not in packet for packet in legacy_packets.values())


@pytest.mark.parametrize(
    "mutation",
    ["missing-meta", "summary-only", "wrong-session", "not-delivered", "duplicate-receipt", "unknown-recipient"],
)
def test_invalid_delivery_attestation_never_passes(tmp_path, mutation):
    runtime, _, _, observer = setup_workflow(tmp_path)
    bundle = delivery_bundle(runtime)
    packets = attest(runtime, bundle)
    receipts = bundle["delivery_receipts"]
    if mutation == "missing-meta":
        receipts.pop()
    elif mutation == "summary-only":
        receipts[-1]["input_digest"] = fingerprint({"summary": "all reports passed"})
    elif mutation == "wrong-session":
        receipts[-1]["session_id"] = "unrelated-session"
    elif mutation == "not-delivered":
        receipts[-1]["status"] = "PREPARED"
    elif mutation == "duplicate-receipt":
        receipts[-1]["transport_receipt_id"] = receipts[0]["transport_receipt_id"]
    else:
        receipts[-1]["agent_id"] = "invented"
    with pytest.raises(ContractValidationError):
        submit(runtime, observer, bundle)
    assert runtime.store.get("review", "G1") is None
    assert packets["meta"]["context"] == bundle["context"]


def test_delivery_hash_cannot_survive_raw_report_mutation(tmp_path):
    runtime, _, _, observer = setup_workflow(tmp_path)
    bundle = delivery_bundle(runtime)
    attest(runtime, bundle)
    changed = deepcopy(bundle)
    changed["arbiter"]["rationale"] = "different original rationale"
    with pytest.raises(ContractValidationError):
        submit(runtime, observer, changed)
    assert runtime.store.get("review", "G1") is None


def test_dispute_packets_retain_original_findings_and_main_opinion(tmp_path):
    runtime, _, _, observer = setup_workflow(tmp_path)
    bundle = delivery_bundle(runtime)
    finding = ReviewFinding("F1", "original objection", ("E1",), "blocking", "unresolved")
    bundle["reviewer_results"][0]["findings"] = [finding.to_dict()]
    attest(runtime, bundle)
    first = submit(runtime, observer, bundle)
    assert first["status"] == "REVIEW_REQUIRED"
    second = delivery_bundle(runtime)
    second["round_number"] = 2
    for report in second["reviewer_results"]:
        report["round_number"] = 2
    second["arbiter"]["round_number"] = 2
    second["meta_review"]["round_number"] = 2
    for binding in second["bindings"]:
        binding["session_id"] += "-second"
    second["dispositions"] = {"F1": {"status": "REFUTED", "evidence_refs": ["E1"]}}
    packets = attest(runtime, second)
    for packet in packets.values():
        assert packet["prior_rounds"] == first["history"]
        assert packet["prior_rounds"][0]["reviewer_results"][0]["findings"] == [finding.to_dict()]
        assert packet["prior_rounds"][0]["arbiter"] == bundle["arbiter"]


def test_pure_builder_freezes_complete_independent_packets(tmp_path):
    runtime, _, _, _ = setup_workflow(tmp_path)
    bundle = delivery_bundle(runtime)
    before = runtime.store.audit()
    packets = build_packets(runtime.review_context("G1"), bundle, [])
    assert packets["A"] == packets["B"]
    assert packets["A"]["reviewer_results"] == []
    for label in ("Q1 Goal", "Q2 Logic", "Q3 Path", "Q4 Minimum", "Q5 Structure"):
        assert label in packets["A"]["prompt"]
    bundle["arbiter"]["rationale"] = "changed after packet creation"
    assert packets["meta"]["arbiter"] != bundle["arbiter"]
    packets["A"]["context"]["goal"] = "mutate only one packet"
    assert packets["A"] != packets["B"]
    assert runtime.store.audit() == before


def test_pure_receipt_check_requires_exact_signed_controller_material(tmp_path):
    runtime, _, _, _ = setup_workflow(tmp_path)
    bundle = delivery_bundle(runtime)
    packets = build_packets(runtime.review_context("G1"), bundle, [])
    bundle["delivery_receipts"] = [
        {
            "agent_id": binding["agent_id"],
            "session_id": binding["session_id"],
            "input_digest": fingerprint(packets[binding["agent_id"]]),
            "transport_receipt_id": "synthetic-" + binding["session_id"],
            "status": "DELIVERED",
        }
        for binding in bundle["bindings"]
    ]
    validate_receipts(bundle, packets)
    bundle["delivery_receipts"][-1]["summary_only"] = True
    with pytest.raises(ContractValidationError):
        validate_receipts(bundle, packets)
