from __future__ import annotations

import json

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
