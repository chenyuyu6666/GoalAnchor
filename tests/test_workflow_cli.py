from __future__ import annotations

import json
from pathlib import Path

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.workflow_cli import main, parse_envelope
from goalanchor.workflow_store import WorkflowStore


def test_read_commands_do_not_create_missing_database(tmp_path: Path, capsys) -> None:
    database = tmp_path / 'missing.db'
    for command in ('status', 'audit', 'recover'):
        assert main([command, '--database', str(database)]) == 2
        assert not database.exists()
    assert 'error' in capsys.readouterr().err


def test_read_status_and_audit_preserve_database(tmp_path: Path, capsys) -> None:
    database = tmp_path / 'state.db'
    store = WorkflowStore(database)
    store.commit([('goal', 'G1', {'goal_id': 'G1', 'state': 'PLAN'})],
                 expected={('goal', 'G1'): None}, event_id='e1', event={'kind': 'test'})
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA journal_mode=DELETE')
    before = database.read_bytes()
    assert main(['status', '--database', str(database)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['data']['goals'][0]['state'] == 'PLAN'
    assert database.read_bytes() == before
    assert main(['audit', '--database', str(database)]) == 0
    assert json.loads(capsys.readouterr().out)['data']['events'][0]['event_id'] == 'e1'


@pytest.mark.parametrize('raw', [
    '{"protocol":"other","kind":"status","data":{}}',
    '{"protocol":"goalanchor.workflow.v1","kind":"approve","data":{}}',
    '{"protocol":"goalanchor.workflow.v1","kind":"status","data":{},"actor":"USER"}',
    '{"protocol":"goalanchor.workflow.v1","kind":"status","data":{},"data":{}}',
    '{"protocol":"goalanchor.workflow.v1","kind":"status","data":{"x":NaN}}',
])
def test_strict_envelope_rejects_untrusted_or_unknown_input(raw: str) -> None:
    with pytest.raises(ContractValidationError):
        parse_envelope(raw)


def test_capabilities_do_not_claim_native_host_verification(capsys) -> None:
    assert main(['capabilities']) == 0
    data = json.loads(capsys.readouterr().out)['data']
    assert data['model_can_sign'] is False
    assert data['host_execution_verified'] is False


def test_demo_delivers_and_refuses_existing_directory(tmp_path: Path, capsys) -> None:
    target = tmp_path / 'demo'
    assert main(['demo', '--directory', str(target)]) == 0
    assert json.loads(capsys.readouterr().out)['data']['state'] == 'CLOSED'
    assert (target / 'artifact.txt').read_text(encoding='utf-8') == 'done'
    assert main(['demo', '--directory', str(target)]) == 2
    assert (target / 'artifact.txt').read_text(encoding='utf-8') == 'done'


def test_recover_marks_ambiguous_attempt_unknown_without_dispatch(tmp_path: Path, capsys) -> None:
    database = tmp_path / 'state.db'
    store = WorkflowStore(database)
    receipt = {'attempt_id': 'a', 'goal_id': 'G1', 'dispatch_right': True, 'state': 'RUNNING'}
    store.commit([('receipt', 'a', receipt)], expected={('receipt', 'a'): None},
                 event_id='setup', event={'kind': 'fixture'})
    assert main(['recover', '--database', str(database)]) == 0
    data = json.loads(capsys.readouterr().out)['data']
    assert data['dispatched'] is False
    assert data['recovered'][0]['state'] == 'UNKNOWN'
    assert main(['recover', '--database', str(database)]) == 0
    assert json.loads(capsys.readouterr().out)['data']['recovered'] == []


def test_read_rejects_unknown_schema_without_migrating(tmp_path: Path, capsys) -> None:
    import sqlite3

    database = tmp_path / 'future.db'
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA user_version=99')
    before = database.read_bytes()
    assert main(['status', '--database', str(database)]) == 2
    assert database.read_bytes() == before
    assert 'Unsupported' in capsys.readouterr().err


def test_wal_read_commands_fail_closed_without_side_files(tmp_path: Path, capsys) -> None:
    database = tmp_path / 'wal.db'
    WorkflowStore(database)
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    for command in ('status', 'audit'):
        assert main([command, '--database', str(database)]) == 2
        assert 'WAL' in capsys.readouterr().err
        assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before


def test_live_wal_is_never_ignored_by_read_commands(tmp_path: Path, capsys) -> None:
    import sqlite3

    database = tmp_path / 'live.db'
    WorkflowStore(database)
    with sqlite3.connect(database) as writer:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO records VALUES ('goal','live','{\"state\":\"PLAN\"}')")
        writer.commit()
        before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
        assert main(['status', '--database', str(database)]) == 2
        assert 'WAL' in capsys.readouterr().err
        assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before
