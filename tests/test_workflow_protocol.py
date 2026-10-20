from __future__ import annotations

import json
import pickle
from dataclasses import asdict

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.workflow_protocol import (
    Baseline,
    Criterion,
    EventAuthority,
    EvidenceSnapshot,
    Operation,
    SignedEvent,
    canonical,
    strict_loads,
)


def test_canonical_rejects_ambiguous_or_invalid_json() -> None:
    assert canonical({"b": [2], "a": "中文"}) == '{"a":"中文","b":[2]}'
    for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
        with pytest.raises(ContractValidationError):
            strict_loads(raw)
    with pytest.raises(ContractValidationError):
        canonical({1: "ambiguous"})


def test_signed_event_cannot_be_forged_or_retargeted() -> None:
    authority = EventAuthority(b"a" * 32, "trusted-host")
    event = authority.sign("user.answer", "question-hash", {"selected": ["approve"]}, event_id="event-1")
    assert authority.verify(event, kind="user.answer", subject="question-hash") == {"selected": ["approve"]}
    assert SignedEvent.from_dict(event.to_dict()) == event
    forged = event.to_dict()
    forged["payload"]["selected"] = ["other"]
    with pytest.raises(ContractValidationError, match="signature"):
        authority.verify(SignedEvent.from_dict(forged))
    with pytest.raises(ContractValidationError):
        authority.verify(event, subject="another-question")
    with pytest.raises(ContractValidationError):
        EventAuthority(b"b" * 32, "trusted-host").verify(event)


def test_baseline_operation_and_snapshot_are_content_bound() -> None:
    criterion = Criterion("C1", "file content", canonical("done"), "artifact")
    baseline = Baseline("G1", 1, "deliver", "minimal plan", (criterion,))
    assert Baseline.from_dict(baseline.to_dict()) == baseline
    assert Baseline.from_dict({**baseline.to_dict(), "revision": 2}).fingerprint != baseline.fingerprint
    operation = Operation("O1", "G1", baseline.fingerprint, "write_text", canonical({"text": "done"}), ".", "W1")
    assert Operation.from_dict(operation.to_dict()) == operation
    altered = operation.to_dict()
    altered["arguments"]["text"] = "changed"
    assert Operation.from_dict(altered).fingerprint != operation.fingerprint
    snapshot = EvidenceSnapshot.capture("E1", "artifact", b"done", source="test-runner")
    assert snapshot.content == b"done"
    assert EvidenceSnapshot.from_dict(snapshot.to_dict()) == snapshot
    tampered = snapshot.to_dict()
    tampered["sha256"] = "0" * 64
    with pytest.raises(ContractValidationError):
        EvidenceSnapshot.from_dict(tampered)


def test_v08_baseline_wire_and_fingerprint_remain_unchanged() -> None:
    baseline = Baseline(
        "goal-v08",
        3,
        "objective",
        "plan",
        (Criterion("c1", "check", canonical(True), "item-1"),),
    )

    assert baseline.to_dict() == {
        "goal_id": "goal-v08",
        "revision": 3,
        "objective": "objective",
        "plan": "plan",
        "criteria": [{
            "criterion_id": "c1",
            "description": "check",
            "expected": True,
            "object_ref": "item-1",
        }],
        "constraints": [],
        "required_operations": [],
    }
    assert baseline.fingerprint == "d49e607d064ba34fdb2196d638cb8411da1731f3b5f0dd8aa713f85320077180"
    assert Baseline.from_dict(baseline.to_dict()) == baseline
    explicit_empty = Baseline(
        baseline.goal_id,
        baseline.revision,
        baseline.objective,
        baseline.plan,
        baseline.criteria,
        decision_dependencies={},
    )
    assert explicit_empty.to_dict() == baseline.to_dict()
    assert explicit_empty.fingerprint == baseline.fingerprint


def test_baseline_decision_dependencies_are_canonical_and_version_bound() -> None:
    input_dependencies = {
        "operation:W1": ["W2", "W1"],
        "direction:choose": [],
    }
    baseline = Baseline(
        "G1",
        1,
        "deliver",
        "minimal plan",
        (Criterion("C1", "file content", canonical("done"), "artifact"),),
        decision_dependencies=input_dependencies,
    )
    input_dependencies["operation:W1"].append("mutated")

    assert baseline.to_dict()["decision_dependencies"] == {
        "direction:choose": [],
        "operation:W1": ["W1", "W2"],
    }
    assert baseline.decision_dependency_map["operation:W1"] == ("W1", "W2")
    with pytest.raises(TypeError):
        baseline.decision_dependency_map["new"] = ("W3",)
    assert Baseline.from_dict(baseline.to_dict()) == baseline
    assert hash(baseline) == hash(Baseline.from_dict(baseline.to_dict()))
    assert pickle.loads(pickle.dumps(baseline)) == baseline
    assert asdict(baseline)["decision_dependencies"] == baseline.decision_dependencies
    without_dependencies = Baseline(
        baseline.goal_id,
        baseline.revision,
        baseline.objective,
        baseline.plan,
        baseline.criteria,
    )
    assert baseline.fingerprint != without_dependencies.fingerprint


def test_baseline_accepts_immutable_dependency_pairs_for_reconstruction() -> None:
    baseline = Baseline(
        "G1",
        1,
        "deliver",
        "minimal plan",
        (Criterion("C1", "file content", canonical("done"), "artifact"),),
        decision_dependencies=(("operation:W1", ("W2", "W1")),),
    )

    assert baseline.decision_dependencies == (("operation:W1", ("W1", "W2")),)
    assert baseline.decision_dependency_map["operation:W1"] == ("W1", "W2")
    assert Baseline.from_dict(baseline.to_dict()) == baseline


@pytest.mark.parametrize(
    "dependencies",
    [
        {"": ["W1"]},
        {"key": [""]},
        {"key": ["W1", "W1"]},
        {"key": "W1"},
        {1: ["W1"]},
        {" decision:key ": ["W1"], "decision:key": []},
        [(" decision:key ", ["W1"]), ("decision:key", [])],
    ],
)
def test_baseline_rejects_malformed_decision_dependencies(dependencies) -> None:
    with pytest.raises(ContractValidationError):
        Baseline(
            "G1",
            1,
            "deliver",
            "plan",
            (Criterion("C1", "check", canonical(True), "item"),),
            decision_dependencies=dependencies,
        )


@pytest.mark.parametrize(
    "dependencies",
    [
        {" decision:key ": ["W1"], "decision:key": []},
        {"decision:key": [], " decision:key ": ["W1"]},
    ],
)
def test_baseline_deserialization_rejects_normalized_key_collisions(dependencies) -> None:
    baseline = Baseline(
        "G1", 1, "goal", "plan", (Criterion("C1", "check", canonical(True), "item"),)
    )
    payload = {**baseline.to_dict(), "decision_dependencies": dependencies}

    with pytest.raises(ContractValidationError, match="collide"):
        Baseline.from_dict(payload)


def test_baseline_deserialization_requires_dependency_json_object() -> None:
    baseline = Baseline(
        "G1", 1, "goal", "plan", (Criterion("C1", "check", canonical(True), "item"),)
    )
    with pytest.raises(ContractValidationError, match="must be an object"):
        Baseline.from_dict({**baseline.to_dict(), "decision_dependencies": [["key", ["W1"]]]})


def test_protocol_data_does_not_keep_mutable_payloads() -> None:
    original = {"nested": ["safe"]}
    authority = EventAuthority(b"k" * 32, "host")
    event = authority.sign("user.answer", "subject", original)
    original["nested"].append("mutated")
    view = event.payload
    view["nested"].append("also mutated")
    assert event.payload == {"nested": ["safe"]}
    assert json.loads(event.payload_json) == {"nested": ["safe"]}


@pytest.mark.parametrize("kind", [bytearray, memoryview, bytes])
@pytest.mark.parametrize("from_dict", [False, True])
def test_snapshot_rejects_non_text_base64(kind, from_dict) -> None:
    data = EvidenceSnapshot.capture("E1", "artifact", b"done", source="test").to_dict()
    data["content_base64"] = kind(b"ZG9uZQ==")
    with pytest.raises(ContractValidationError, match="snapshot content"):
        if from_dict:
            EvidenceSnapshot.from_dict(data)
        else:
            EvidenceSnapshot(**data)


@pytest.mark.parametrize("revision", [True, -1, 0, "1"])
def test_baseline_revision_is_positive_integer(revision) -> None:
    with pytest.raises(ContractValidationError):
        Baseline("G1", revision, "goal", "plan", (Criterion("C1", "test", "true", "artifact"),))
