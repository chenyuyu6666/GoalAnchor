from __future__ import annotations

import hashlib
import multiprocessing as mp
import sqlite3
import time
from pathlib import Path

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.efficiency_admission import EfficiencyAdmissionStore
from goalanchor.efficiency_governance import ComplexityResult, EfficiencyAdmissionError
from goalanchor.workflow_protocol import canonical, fingerprint

_NOW = "2026-11-18T00:00:00+00:00"
_LATER = "2026-11-18T01:00:00+00:00"


def _admit(
    ledger,
    *,
    key: str = "admission-1",
    scope: str = "scope-1",
    tokens: int | None = 60,
    elapsed: float = 1.0,
    operation_fingerprint: str = "a" * 64,
    token_budget: int = 100,
    time_budget: float = 10,
    progress_alert: bool = False,
):
    return ledger.admit(
        idempotency_key=key,
        scope_id=scope,
        token_budget=token_budget,
        time_budget_seconds=time_budget,
        operation="write_text",
        operation_fingerprint=operation_fingerprint,
        risk="LOW",
        complexity=ComplexityResult(score=0, level="LOW", signals={}),
        progress_alert=progress_alert,
        tokens_used=tokens,
        elapsed_seconds=elapsed,
    )


def _insert_raw_admission(
    ledger,
    *,
    key: str,
    allowed: bool,
    reason: str,
    tokens_used: int | None,
    elapsed_seconds: float,
    token_status: str,
    generation: int,
    cumulative_tokens: int,
    cumulative_elapsed_seconds: float,
    token_budget: int = 100,
    time_budget: float = 10,
    progress_alert: bool = False,
    operation_fingerprint: str = "a" * 64,
    recorded_at: str = _NOW,
) -> dict:
    request = ledger._request(
        idempotency_key=key,
        scope_id="scope-1",
        token_budget=token_budget,
        time_budget_seconds=time_budget,
        operation="write_text",
        operation_fingerprint=operation_fingerprint,
        risk="LOW",
        complexity=ComplexityResult(score=0, level="LOW", signals={}),
        tokens_used=tokens_used,
        elapsed_seconds=elapsed_seconds,
    )
    digest = fingerprint(request)
    admission_id = hashlib.sha256(
        (digest + "\0" + recorded_at).encode("utf-8")
    ).hexdigest()
    record = {
        "admission_id": admission_id,
        **request,
        "request_digest": digest,
        "allowed": allowed,
        "reason": reason,
        "token_status": token_status,
        "progress_alert": progress_alert,
        "recorded_at": recorded_at,
        "generation": generation,
        "cumulative_tokens": cumulative_tokens,
        "cumulative_elapsed_seconds": cumulative_elapsed_seconds,
    }
    db = sqlite3.connect(ledger.path)
    try:
        db.execute(
            "INSERT INTO efficiency_admissions("
            "admission_id,idempotency_key,request_digest,scope_id,operation,"
            "operation_fingerprint,risk,allowed,reason,complexity,tokens_used,"
            "elapsed_seconds,token_status,progress_alert,recorded_at,generation,"
            "cumulative_tokens,cumulative_elapsed_seconds,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                admission_id,
                record["idempotency_key"],
                digest,
                record["scope_id"],
                record["operation"],
                record["operation_fingerprint"],
                record["risk"],
                int(allowed),
                reason,
                canonical(record["complexity"]),
                tokens_used,
                elapsed_seconds,
                token_status,
                int(progress_alert),
                recorded_at,
                generation,
                cumulative_tokens,
                cumulative_elapsed_seconds,
                canonical(record),
            ),
        )
        db.commit()
    finally:
        db.close()
    return record


def test_admission_is_persisted_and_replayed_without_double_consuming(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    first = _admit(ledger)
    assert first.allowed is True
    assert first.reason == "WITHIN_BUDGET"

    replayed = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _LATER)
    second = _admit(replayed)
    assert first == second

    state = replayed.budget_state("scope-1")
    assert state.tokens_consumed == 60
    assert state.elapsed_seconds == 1.0
    assert state.generation == 1


def test_relative_database_path_is_fixed_at_construction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ledger = EfficiencyAdmissionStore("relative.db", clock=lambda: _NOW)
    _admit(ledger, key="relative")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    admission = ledger.get("relative")
    assert admission is not None
    assert admission.allowed is True


def test_in_memory_database_is_rejected() -> None:
    with pytest.raises(ContractValidationError, match="persistent filesystem path"):
        EfficiencyAdmissionStore(":memory:")


def test_conflicting_admission_with_the_same_key_is_rejected(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger, tokens=60)
    with pytest.raises(ContractValidationError, match="CONFLICT"):
        _admit(ledger, tokens=70)


def test_budget_is_cumulative_across_separate_connections(tmp_path: Path) -> None:
    first = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    assert _admit(first, tokens=60).allowed is True

    second = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _LATER)
    denied = _admit(second, key="admission-2", tokens=50)
    assert denied.allowed is False
    assert denied.reason == "TOKEN_BUDGET_EXCEEDED"

    state = second.budget_state("scope-1")
    assert state.tokens_consumed == 60
    assert state.generation == 1


def _racing_admission(path: str, index: int, start, ready, queue) -> None:
    ledger = EfficiencyAdmissionStore(path, clock=lambda: _NOW)
    with ready.get_lock():
        ready.value += 1
    assert start.wait(5)
    admission = _admit(ledger, key=f"racing-{index}", tokens=60)
    queue.put({"allowed": admission.allowed, "reason": admission.reason})


def _racing_identical_admission(path: str, start, ready, queue) -> None:
    ledger = EfficiencyAdmissionStore(path, clock=lambda: _NOW)
    with ready.get_lock():
        ready.value += 1
    assert start.wait(5)
    admission = _admit(ledger, key="same-request", tokens=60)
    queue.put(
        {
            "admission_id": admission.admission_id,
            "allowed": admission.allowed,
            "reason": admission.reason,
        }
    )


def _racing_conflicting_admission(path: str, index: int, start, ready, queue) -> None:
    ledger = EfficiencyAdmissionStore(path, clock=lambda: _NOW)
    with ready.get_lock():
        ready.value += 1
    assert start.wait(5)
    try:
        admission = _admit(ledger, key="same-key", tokens=60 + index)
        queue.put({"allowed": admission.allowed, "error": None})
    except EfficiencyAdmissionError as exc:
        queue.put({"allowed": False, "error": exc.code})


def test_concurrent_processes_cannot_consume_the_same_budget_twice(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    ready = ctx.Value("i", 0)
    queue = ctx.Queue()
    path = str(tmp_path / "efficiency.db")
    processes = [
        ctx.Process(target=_racing_admission, args=(path, index, start, ready, queue))
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for _ in range(100):
        if ready.value == 2:
            break
        time.sleep(0.05)
    assert ready.value == 2
    start.set()
    for process in processes:
        process.join(20)
    assert all(process.exitcode == 0 for process in processes)
    results = [queue.get(timeout=5) for _ in processes]
    assert sorted(result["allowed"] for result in results) == [False, True]

    ledger = EfficiencyAdmissionStore(path, clock=lambda: _LATER)
    assert ledger.budget_state("scope-1").tokens_consumed == 60


def test_concurrent_identical_requests_replay_as_one_admission(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    ready = ctx.Value("i", 0)
    queue = ctx.Queue()
    path = str(tmp_path / "efficiency.db")
    processes = [
        ctx.Process(target=_racing_identical_admission, args=(path, start, ready, queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for _ in range(100):
        if ready.value == 2:
            break
        time.sleep(0.05)
    assert ready.value == 2
    start.set()
    for process in processes:
        process.join(20)
    assert all(process.exitcode == 0 for process in processes)
    results = [queue.get(timeout=5) for _ in processes]
    assert len({result["admission_id"] for result in results}) == 1
    assert [result["allowed"] for result in results] == [True, True]

    ledger = EfficiencyAdmissionStore(path, clock=lambda: _LATER)
    assert ledger.budget_state("scope-1").tokens_consumed == 60


def test_concurrent_same_key_with_different_input_has_one_conflict(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    ready = ctx.Value("i", 0)
    queue = ctx.Queue()
    path = str(tmp_path / "efficiency.db")
    processes = [
        ctx.Process(target=_racing_conflicting_admission, args=(path, index, start, ready, queue))
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for _ in range(100):
        if ready.value == 2:
            break
        time.sleep(0.05)
    assert ready.value == 2
    start.set()
    for process in processes:
        process.join(20)
    assert all(process.exitcode == 0 for process in processes)
    results = [queue.get(timeout=5) for _ in processes]
    assert sorted(result["allowed"] for result in results) == [False, True]
    assert sorted(result["error"] for result in results if result["error"] is not None) == ["CONFLICT"]


def test_admission_history_is_append_only(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger, key="one", tokens=10)
    _admit(ledger, key="two", tokens=20)
    history = ledger.history("scope-1")
    assert [item.idempotency_key for item in history] == ["one", "two"]

    db = sqlite3.connect(ledger.path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE efficiency_admissions SET reason='TOKEN_BUDGET_EXCEEDED'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM efficiency_admissions")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT OR REPLACE INTO efficiency_admissions "
                "SELECT * FROM efficiency_admissions LIMIT 1"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM efficiency_budget_heads")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT OR REPLACE INTO efficiency_budget_heads "
                "SELECT * FROM efficiency_budget_heads LIMIT 1"
            )
    finally:
        db.close()


def test_idempotency_key_cannot_be_reused_in_another_scope(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger, key="shared", scope="scope-1", tokens=10)

    with pytest.raises(EfficiencyAdmissionError) as caught:
        _admit(ledger, key="shared", scope="scope-2", tokens=10)
    assert caught.value.code == "CONFLICT"


def test_denied_attempt_key_is_terminal_until_a_new_attempt_key_is_used(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    denied = _admit(ledger, key="attempt-a", tokens=None)
    assert denied.allowed is False
    assert denied.reason == "UNKNOWN_USAGE"

    with pytest.raises(EfficiencyAdmissionError) as caught:
        _admit(ledger, key="attempt-a", tokens=10)
    assert caught.value.code == "CONFLICT"
    assert _admit(ledger, key="attempt-b", tokens=10).allowed is True


def test_token_denial_remains_valid_when_time_also_exceeds_budget(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    first = _admit(ledger, key="first", tokens=60, elapsed=9.0)
    assert first.allowed is True

    denied = _admit(ledger, key="both-over", tokens=50, elapsed=9.0)
    assert denied.allowed is False
    assert denied.reason == "TOKEN_BUDGET_EXCEEDED"
    assert ledger.get("both-over") == denied
    assert [item.reason for item in ledger.history("scope-1")] == [
        "WITHIN_BUDGET",
        "TOKEN_BUDGET_EXCEEDED",
    ]
    assert ledger.budget_state("scope-1").tokens_consumed == 60


def test_token_only_denial_stays_valid_when_time_budget_is_exhausted(
    tmp_path: Path,
) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    first = _admit(ledger, key="first", tokens=60, elapsed=5.0)
    assert first.allowed is True

    denied = _admit(ledger, key="over-token-only", tokens=50, elapsed=8.0)
    assert denied.allowed is False
    assert denied.reason == "TOKEN_BUDGET_EXCEEDED"
    assert ledger.get("over-token-only") == denied
    assert ledger.budget_state("scope-1").tokens_consumed == 60

    time_only = _admit(ledger, key="over-time-only", tokens=10, elapsed=9.0)
    assert time_only.allowed is False
    assert time_only.reason == "TIME_BUDGET_EXCEEDED"
    assert ledger.get("over-time-only") == time_only
    assert [item.reason for item in ledger.history("scope-1")] == [
        "WITHIN_BUDGET",
        "TOKEN_BUDGET_EXCEEDED",
        "TIME_BUDGET_EXCEEDED",
    ]


@pytest.mark.parametrize(
    ("token_budget", "time_budget"),
    [(200, 10.0), (100, 20.0)],
)
def test_denied_only_scope_already_locks_the_budget_policy(
    tmp_path: Path,
    token_budget: int,
    time_budget: float,
) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    denied = _admit(ledger, key="first-denial", tokens=None)
    assert denied.allowed is False

    with pytest.raises(EfficiencyAdmissionError) as caught:
        _admit(
            ledger,
            key="widened-after-denial",
            tokens=10,
            token_budget=token_budget,
            time_budget=time_budget,
        )
    assert caught.value.code == "UNTRUSTED"
    assert ledger.budget_state("scope-1") is None


def test_get_maps_malformed_evidence_to_untrusted(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    db = sqlite3.connect(ledger.path)
    try:
        db.execute(
            "INSERT INTO efficiency_admissions("
            "admission_id,idempotency_key,request_digest,scope_id,operation,"
            "operation_fingerprint,risk,allowed,reason,complexity,tokens_used,"
            "elapsed_seconds,token_status,progress_alert,recorded_at,generation,"
            "cumulative_tokens,cumulative_elapsed_seconds,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c" * 64,
                "malformed",
                "d" * 64,
                "scope-1",
                "write_text",
                "a" * 64,
                "LOW",
                0,
                "UNKNOWN_USAGE",
                canonical({"score": 0, "level": "LOW", "signals": {}}),
                None,
                0.0,
                "UNKNOWN",
                0,
                _NOW,
                1,
                0,
                0.0,
                '{"malformed":true}',
            ),
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(EfficiencyAdmissionError) as caught:
        ledger.get("malformed")
    assert caught.value.code == "UNTRUSTED"


def test_corrupted_admission_row_fails_closed(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger)
    db = sqlite3.connect(ledger.path)
    try:
        trigger_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='efficiency_admissions_no_update'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER efficiency_admissions_no_update")
        db.execute("UPDATE efficiency_admissions SET allowed=0 WHERE idempotency_key='admission-1'")
        db.execute(trigger_sql)
        db.commit()
    finally:
        db.close()
    with pytest.raises(EfficiencyAdmissionError) as caught:
        ledger.get("admission-1")
    assert caught.value.code == "UNTRUSTED"


def test_budget_head_cannot_change_scope(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger)
    _admit(ledger, key="denied", tokens=200)
    db = sqlite3.connect(ledger.path)
    try:
        denied_id = ledger.get("denied").admission_id
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE efficiency_budget_heads SET scope_id='foreign',generation=99,"
                "last_admission_id=?",
                (denied_id,),
            )
    finally:
        db.close()


def test_structurally_tampered_payload_fails_closed(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger)
    db = sqlite3.connect(ledger.path)
    try:
        trigger_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='efficiency_admissions_no_update'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER efficiency_admissions_no_update")
        db.execute(
            "UPDATE efficiency_admissions SET scope_id='foreign',request_digest=? "
            "WHERE idempotency_key='admission-1'",
            ("b" * 64,),
        )
        db.execute(
            "UPDATE efficiency_admissions SET payload=REPLACE(payload,'\"scope_id\":\"scope-1\"',"
            "'\"scope_id\":\"foreign\"') WHERE idempotency_key='admission-1'"
        )
        db.execute(trigger_sql)
        db.commit()
    finally:
        db.close()
    with pytest.raises(EfficiencyAdmissionError) as caught:
        ledger.get("admission-1")
    assert caught.value.code == "UNTRUSTED"


def test_missing_budget_head_with_history_fails_closed(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger)
    db = sqlite3.connect(ledger.path)
    try:
        trigger_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='efficiency_budget_heads_no_delete'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER efficiency_budget_heads_no_delete")
        db.execute("DELETE FROM efficiency_budget_heads")
        db.execute(trigger_sql)
        db.commit()
    finally:
        db.close()
    with pytest.raises(EfficiencyAdmissionError) as caught:
        _admit(ledger, key="after-head-loss", tokens=1)
    assert caught.value.code == "UNTRUSTED"


def test_replay_also_fails_closed_after_head_loss(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger)
    db = sqlite3.connect(ledger.path)
    try:
        trigger_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='efficiency_budget_heads_no_delete'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER efficiency_budget_heads_no_delete")
        db.execute("DELETE FROM efficiency_budget_heads")
        db.execute(trigger_sql)
        db.commit()
    finally:
        db.close()
    with pytest.raises(EfficiencyAdmissionError) as caught:
        _admit(ledger)
    assert caught.value.code == "UNTRUSTED"


def test_allowed_history_cannot_exceed_its_budget_policy(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    record = _insert_raw_admission(
        ledger,
        key="over-budget",
        allowed=True,
        reason="WITHIN_BUDGET",
        tokens_used=150,
        elapsed_seconds=1.0,
        token_status="MEASURED",
        generation=1,
        cumulative_tokens=150,
        cumulative_elapsed_seconds=1.0,
    )
    db = sqlite3.connect(ledger.path)
    try:
        db.execute(
            "INSERT INTO efficiency_budget_heads("
            "scope_id,generation,tokens_consumed,elapsed_seconds,token_budget,"
            "time_budget_seconds,last_admission_id,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("scope-1", 1, 150, 1.0, 100, 10.0, record["admission_id"], _NOW),
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(EfficiencyAdmissionError) as caught:
        ledger.budget_state("scope-1")
    assert caught.value.code == "UNTRUSTED"


def test_impossible_denied_reason_and_token_status_fail_closed(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _insert_raw_admission(
        ledger,
        key="impossible-denial",
        allowed=False,
        reason="TOKEN_BUDGET_EXCEEDED",
        tokens_used=None,
        elapsed_seconds=1.0,
        token_status="UNKNOWN",
        generation=1,
        cumulative_tokens=0,
        cumulative_elapsed_seconds=0.0,
    )

    with pytest.raises(EfficiencyAdmissionError) as caught:
        ledger.get("impossible-denial")
    assert caught.value.code == "UNTRUSTED"


def test_each_allowed_admission_advances_one_generation(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger, key="one", tokens=10)
    _admit(ledger, key="two", tokens=20)
    history = ledger.history("scope-1")
    assert [item.generation for item in history] == [1, 2]
    assert ledger.budget_state("scope-1").generation == 2


@pytest.mark.parametrize(
    "name",
    [
        "efficiency_admissions",
        "efficiency_budget_heads",
        "efficiency_admissions_no_update",
        "efficiency_admissions_no_delete",
        "efficiency_admissions_no_replace",
        "efficiency_budget_heads_no_delete",
        "efficiency_budget_heads_no_replace",
        "efficiency_budget_heads_monotonic",
    ],
)
def test_required_schema_objects_cannot_be_removed(tmp_path: Path, name: str) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger)
    db = sqlite3.connect(ledger.path)
    try:
        kind = "TABLE" if name in {"efficiency_admissions", "efficiency_budget_heads"} else "TRIGGER"
        db.execute(f"DROP {kind} {name}")
        db.commit()
    finally:
        db.close()

    with pytest.raises(EfficiencyAdmissionError) as caught:
        EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    assert caught.value.code == "UNTRUSTED"


def test_changed_append_only_trigger_is_rejected(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger)
    db = sqlite3.connect(ledger.path)
    try:
        db.execute("DROP TRIGGER efficiency_admissions_no_update")
        db.execute(
            "CREATE TRIGGER efficiency_admissions_no_update BEFORE UPDATE ON efficiency_admissions "
            "BEGIN SELECT 1; END"
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(EfficiencyAdmissionError) as caught:
        EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    assert caught.value.code == "UNTRUSTED"


def test_extra_schema_trigger_cannot_suppress_denied_inserts(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    db = sqlite3.connect(ledger.path)
    try:
        db.execute(
            "CREATE TRIGGER efficiency_admissions_extra_ignore "
            "BEFORE INSERT ON efficiency_admissions WHEN NEW.allowed=0 "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(EfficiencyAdmissionError) as caught:
        EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    assert caught.value.code == "UNTRUSTED"


def test_extra_schema_table_is_rejected(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    db = sqlite3.connect(ledger.path)
    try:
        db.execute("CREATE TABLE efficiency_admission_backup (id INTEGER PRIMARY KEY)")
        db.commit()
    finally:
        db.close()

    with pytest.raises(EfficiencyAdmissionError) as caught:
        EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    assert caught.value.code == "UNTRUSTED"


def test_existing_instance_detects_schema_removed_after_open(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger)
    db = sqlite3.connect(ledger.path)
    try:
        db.execute("DROP TRIGGER efficiency_budget_heads_monotonic")
        db.commit()
    finally:
        db.close()

    with pytest.raises(EfficiencyAdmissionError) as caught:
        ledger.get("admission-1")
    assert caught.value.code == "UNTRUSTED"


def test_head_must_equal_the_complete_allowed_cumulative_chain(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger, key="first", tokens=100, elapsed=1.0)
    second_request = ledger._request(
        idempotency_key="seeded-next-attempt",
        scope_id="scope-1",
        token_budget=100,
        time_budget_seconds=10,
        operation="write_text",
        operation_fingerprint="b" * 64,
        risk="LOW",
        complexity=ComplexityResult(score=0, level="LOW", signals={}),
        tokens_used=50,
        elapsed_seconds=0.0,
    )
    digest = fingerprint(second_request)
    admission_id = hashlib.sha256((digest + "\0" + _LATER).encode("utf-8")).hexdigest()
    record = {
        "admission_id": admission_id,
        **second_request,
        "request_digest": digest,
        "allowed": True,
        "reason": "WITHIN_BUDGET",
        "token_status": "MEASURED",
        "progress_alert": False,
        "recorded_at": _LATER,
        "generation": 2,
        # The seeded row keeps the old cumulative total even though its own
        # 50 tokens would make the real total 150.
        "cumulative_tokens": 100,
        "cumulative_elapsed_seconds": 1.0,
    }
    db = sqlite3.connect(ledger.path)
    try:
        db.execute(
            "INSERT INTO efficiency_admissions("
            "admission_id,idempotency_key,request_digest,scope_id,operation,"
            "operation_fingerprint,risk,allowed,reason,complexity,tokens_used,"
            "elapsed_seconds,token_status,progress_alert,recorded_at,generation,"
            "cumulative_tokens,cumulative_elapsed_seconds,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                admission_id,
                record["idempotency_key"],
                digest,
                record["scope_id"],
                record["operation"],
                record["operation_fingerprint"],
                record["risk"],
                1,
                record["reason"],
                canonical(record["complexity"]),
                record["tokens_used"],
                record["elapsed_seconds"],
                record["token_status"],
                0,
                _LATER,
                2,
                100,
                1.0,
                canonical(record),
            ),
        )
        db.execute(
            "UPDATE efficiency_budget_heads SET generation=2,tokens_consumed=100,"
            "elapsed_seconds=1.0,last_admission_id=?,updated_at=? WHERE scope_id='scope-1'",
            (admission_id, _LATER),
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(EfficiencyAdmissionError) as history_error:
        ledger.history("scope-1")
    assert history_error.value.code == "UNTRUSTED"
    with pytest.raises(EfficiencyAdmissionError) as get_error:
        ledger.get("seeded-next-attempt")
    assert get_error.value.code == "UNTRUSTED"
    with pytest.raises(EfficiencyAdmissionError) as caught:
        _admit(
            ledger,
            key="seeded-next-attempt",
            tokens=50,
            operation_fingerprint="b" * 64,
        )
    assert caught.value.code == "UNTRUSTED"


def test_budget_policy_cannot_widen_within_one_scope(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)
    _admit(ledger, key="first", tokens=10)

    with pytest.raises(EfficiencyAdmissionError) as caught:
        _admit(ledger, key="wider-policy", tokens=10, token_budget=200)
    assert caught.value.code == "UNTRUSTED"


def test_history_preserves_insertion_order_when_timestamps_tie(tmp_path: Path) -> None:
    ledger = EfficiencyAdmissionStore(tmp_path / "efficiency.db", clock=lambda: _NOW)

    def admission_id(key: str) -> str:
        request = ledger._request(
            idempotency_key=key,
            scope_id="scope-1",
            token_budget=100,
            time_budget_seconds=10,
            operation="write_text",
            operation_fingerprint="a" * 64,
            risk="LOW",
            complexity=ComplexityResult(score=0, level="LOW", signals={}),
            tokens_used=10,
            elapsed_seconds=1.0,
        )
        digest = fingerprint(request)
        return hashlib.sha256((digest + "\0" + _NOW).encode("utf-8")).hexdigest()

    candidates = [f"order-{index}" for index in range(20)]
    first, second = next(
        (left, right)
        for index, left in enumerate(candidates)
        for right in candidates[index + 1 :]
        if admission_id(left) > admission_id(right)
    )
    _admit(ledger, key=first, tokens=10)
    _admit(ledger, key=second, tokens=10)

    assert [item.idempotency_key for item in ledger.history("scope-1")] == [
        first,
        second,
    ]
