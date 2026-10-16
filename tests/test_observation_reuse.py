import hashlib
from dataclasses import replace

import pytest
from test_workflow import NOW, approve, operation, review, setup_workflow

from goalanchor.core import ContractValidationError
from goalanchor.workflow_protocol import Criterion, canonical, fingerprint


def prepared(tmp_path, changed=False):
    runtime, base, user, observer = setup_workflow(tmp_path)
    review(runtime, observer)
    approve(runtime, operation(tmp_path, base), user)
    runtime.execute("O1", "original", lambda _: {"exit_code": 0, "object_ref": "artifact"})
    runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, {
        "goal_id": "G1", "baseline_digest": base.fingerprint, "criterion_id": "C1", "object_ref": "artifact",
        "actual": "done", "attempt_id": "original"}, at=NOW))
    source = runtime.store.get("observation", base.fingerprint + ":C1")
    runtime.close("G1")
    criteria = (Criterion("C1", "artifact content", canonical("new"), "artifact"),) if changed else base.criteria
    new = replace(base, revision=2, plan="Revised plan", criteria=criteria, required_operations=())
    runtime.start_baseline(new, user.sign("user.baseline", new.fingerprint, new.to_dict(), at=NOW))
    review(runtime, observer)
    proof = {"goal_id": "G1", "baseline_digest": new.fingerprint, "source_baseline_digest": base.fingerprint,
             "criterion_id": "C1", "object_ref": "artifact", "actual": "done",
             "source_observation_fingerprint": fingerprint(source),
             "object_digest": hashlib.sha256(b"done").hexdigest(), "no_impact_reason": "Runner read same current bytes"}
    return runtime, base, new, observer, proof


def test_unchanged_criterion_can_reuse_with_current_signed_no_impact_proof(tmp_path):
    runtime, base, new, observer, proof = prepared(tmp_path)
    reused = runtime.reuse_observation(
        "G1", observer.sign("observer.reuse_observation", new.fingerprint, proof, at=NOW))
    assert reused["status"] == "PASS"
    assert runtime.close("G1")["state"] == "CLOSED"
    assert runtime.store.get("closure", base.fingerprint)["revision"] == 1


@pytest.mark.parametrize("change", ["criterion", "object", "digest", "reason"])
def test_changed_dependency_or_missing_proof_cannot_reuse(tmp_path, change):
    runtime, _, new, observer, proof = prepared(tmp_path, changed=change == "criterion")
    if change == "object":
        proof["actual"] = "changed"
    elif change == "digest":
        proof["object_digest"] = "0" * 64
    elif change == "reason":
        proof["no_impact_reason"] = ""
    with pytest.raises(ContractValidationError):
        runtime.reuse_observation("G1", observer.sign("observer.reuse_observation", new.fingerprint, proof, at=NOW))
    with pytest.raises(ContractValidationError):
        runtime.close("G1")


def test_reuse_validation_cannot_replace_closure_original_generation_guard(tmp_path, monkeypatch):
    runtime, _, new, observer, proof = prepared(tmp_path)
    runtime.reuse_observation("G1", observer.sign("observer.reuse_observation", new.fingerprint, proof, at=NOW))
    user = runtime.user_authority
    op = replace(operation(tmp_path, new), operation_id="O2", work_item_id="W2")
    approve(runtime, op, user)
    validate = runtime._reuse_record

    def interleave(*args):
        def lost(_):
            raise TimeoutError("unknown effect during closure")
        assert runtime.execute("O2", "race-unknown", lost)["state"] == "UNKNOWN"
        return validate(*args)

    monkeypatch.setattr(runtime, "_reuse_record", interleave)
    with pytest.raises(ContractValidationError):
        runtime.close("G1")
    assert runtime.store.get("goal", "G1")["state"] != "CLOSED"


def test_t07_changed_c1_cannot_reuse_but_unchanged_c2_can_reuse_same_new_baseline(tmp_path):
    runtime, initial, user, observer = setup_workflow(tmp_path)
    c2 = Criterion("C2", "second artifact", canonical("done"), "artifact2")
    base = replace(initial, revision=2, criteria=initial.criteria + (c2,))
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict(), at=NOW))
    review(runtime, observer)
    for index, criterion in enumerate(base.criteria):
        op = replace(operation(tmp_path, base), operation_id=f"old-{index}", work_item_id=f"W{index}")
        approve(runtime, op, user)
        runtime.execute(op.operation_id, f"old-attempt-{index}",
                        lambda _, ref=criterion.object_ref: {"exit_code": 0, "object_ref": ref})
        runtime.observe("G1", observer.sign("observer.observation", base.fingerprint, {
            "goal_id": "G1", "baseline_digest": base.fingerprint, "criterion_id": criterion.criterion_id,
            "object_ref": criterion.object_ref, "actual": "done", "attempt_id": f"old-attempt-{index}"}, at=NOW))
    new = replace(base, revision=3, criteria=(replace(base.criteria[0], expected_json=canonical("new")), c2),
                  required_operations=())
    runtime.start_baseline(new, user.sign("user.baseline", new.fingerprint, new.to_dict(), at=NOW))
    review(runtime, observer)
    for criterion in base.criteria:
        source = runtime.store.get("observation", base.fingerprint + ":" + criterion.criterion_id)
        proof = {"goal_id": "G1", "baseline_digest": new.fingerprint, "source_baseline_digest": base.fingerprint,
                 "criterion_id": criterion.criterion_id, "object_ref": criterion.object_ref, "actual": "done",
                 "object_digest": hashlib.sha256(b"done").hexdigest(), "no_impact_reason": "Current bytes same",
                 "source_observation_fingerprint": fingerprint(source)}
        event = observer.sign("observer.reuse_observation", new.fingerprint, proof, at=NOW)
        if criterion.criterion_id == "C1":
            with pytest.raises(ContractValidationError):
                runtime.reuse_observation("G1", event)
        else:
            assert runtime.reuse_observation("G1", event)["status"] == "PASS"
    assert runtime.store.get("observation", new.fingerprint + ":C1") is None
    assert runtime.store.get("observation", new.fingerprint + ":C2")["status"] == "PASS"
    with pytest.raises(ContractValidationError):
        runtime.close("G1")
