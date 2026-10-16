import multiprocessing
import sqlite3

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.workflow_store import WorkflowStore

AT = '2026-10-16T00:00:00+00:00'
LATER = '2026-10-17T00:00:00+00:00'


def seed(store, expires=None):
    grant = dict(authorization_id='g', goal_id='goal', plan_revision=1,
                 operation_digest='op', state='ACTIVE', expires_at=expires, source_event_id='user')
    store.commit([('grant', 'g', grant)], {('grant', 'g'): None}, 'seed', {'type': 'seed'})
    return grant


def compete(path, attempt, ready, start, out):
    store = WorkflowStore(path)
    ready.put(True)
    start.wait(10)
    try:
        won, _ = store.reserve('g', attempt, 'op', AT)
        if won:
            store.consume(attempt, AT)
        out.put(won)
    except ContractValidationError:
        out.put(False)


def test_commit_atomic_cas_events_and_reopen(tmp_path):
    store = WorkflowStore(tmp_path / 'state.db')
    value = {'goal_id': 'goal', 'nested': {'x': [1]}}
    changes = [('baseline', "x'; DROP TABLE records;--", value)]
    expected = {('baseline', changes[0][1]): None}
    assert store.commit(changes, expected, 'e', {'x': 1})
    assert not store.commit(changes, expected, 'e', {'x': 1})
    with pytest.raises(ContractValidationError):
        store.commit([], {}, 'e', {'x': 2})
    with pytest.raises(ContractValidationError):
        store.commit([('baseline', 'new', {}), ('baseline', changes[0][1], {})],
                     {('baseline', 'new'): None, **expected}, 'bad', {})
    assert store.get('baseline', 'new') is None
    assert store.get_event('bad') is None
    read = store.get('baseline', changes[0][1])
    read['nested']['x'].append(2)
    reopened = WorkflowStore(tmp_path / 'state.db')
    assert reopened.list('baseline', 'goal') == (value,)
    assert reopened.audit() == ({'seq': 1, 'event_id': 'e', 'payload': {'x': 1}},)
    assert reopened.get_event('e') == {'x': 1}


def test_fingerprint_strict_and_deterministic():
    assert WorkflowStore.fingerprint({'b': 2, 'a': 1}) == WorkflowStore.fingerprint({'a': 1, 'b': 2})
    for value in ({1: 'bad'}, {'x': float('nan')}, {'x': {2: 'bad'}}):
        with pytest.raises(ContractValidationError):
            WorkflowStore.fingerprint(value)


def test_reserved_consumed_recovered_and_terminal(tmp_path):
    store = WorkflowStore(tmp_path / 'state.db')
    seed(store)
    assert store.reserve('g', 'a', 'op', AT)[0]
    assert not store.reserve('g', 'a', 'op', AT)[0]
    with pytest.raises(ContractValidationError):
        store.reserve('g', 'a', 'other', AT)
    receipt = store.consume('a', AT)
    assert receipt['dispatch_right'] and receipt['state'] == 'NOT_STARTED'
    assert not store.reserve('g', 'a', 'op', AT)[0]
    recovered = WorkflowStore(tmp_path / 'state.db').recover(at=LATER)
    assert recovered[0]['state'] == 'UNKNOWN'
    assert not store.recover(at=LATER)
    done = store.finish('a', 'SUCCEEDED', {'ok': True}, LATER)
    assert store.finish('a', 'SUCCEEDED', {'ok': True}, LATER) == done
    with pytest.raises(ContractValidationError):
        store.finish('a', 'FAILED', {}, LATER)


@pytest.mark.parametrize('reason', ['expiry', 'revoke', 'cas'])
def test_consume_checks_revocation_expiry_and_dependencies(tmp_path, reason):
    store = WorkflowStore(tmp_path / 'state.db')
    seed(store, LATER if reason == 'expiry' else None)
    store.reserve('g', 'a', 'op', AT)
    if reason == 'revoke':
        store.revoke('g', AT)
    with pytest.raises(ContractValidationError):
        store.consume('a', LATER if reason == 'expiry' else AT,
                      expected={('baseline', 'missing'): 'old'} if reason == 'cas' else {})
    assert not store.get('receipt', 'a')['dispatch_right']
    assert not store.recover(at=LATER)


def test_two_processes_only_one_dispatch(tmp_path):
    path = str(tmp_path / 'state.db')
    seed(WorkflowStore(path))
    ctx = multiprocessing.get_context('spawn')
    ready, out, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    children = [ctx.Process(target=compete, args=(path, str(i), ready, start, out)) for i in range(2)]
    try:
        for child in children:
            child.start()
        for _ in children:
            ready.get(timeout=15)
        start.set()
        assert sum(out.get(timeout=15) for _ in children) == 1
        for child in children:
            child.join(15)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(5)
        ready.close()
        out.close()
    assert sum(r['dispatch_right'] for r in WorkflowStore(path).list('receipt')) == 1


def test_unknown_schema_and_non_sqlite_preserved(tmp_path):
    path = tmp_path / 'old.db'
    path.write_bytes(b'not sqlite')
    with pytest.raises(ContractValidationError):
        WorkflowStore(path)
    assert path.read_bytes() == b'not sqlite'
    path.unlink()
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=99')
    with pytest.raises(ContractValidationError):
        WorkflowStore(path)


def test_lifecycle_audited_and_reserved_cannot_finish(tmp_path):
    store = WorkflowStore(tmp_path / 'state.db')
    seed(store)
    store.reserve('g', 'a', 'op', AT)
    with pytest.raises(ContractValidationError):
        store.finish('a', 'RUNNING', {}, AT)
    store.consume('a', AT)
    store.finish('a', 'RUNNING', {}, AT)
    store.recover(at=LATER)
    store.revoke('g', LATER)
    assert store.get('grant', 'g')['state'] == 'CONSUMED'
    assert [e['payload'].get('action') for e in store.audit()[1:]] == [
        'reserve', 'consume', 'finish', 'recover', 'revoke']


def test_reserved_audit_namespace_and_rollback_invalid_json(tmp_path):
    store = WorkflowStore(tmp_path / 'state.db')
    with pytest.raises(ContractValidationError):
        store.commit([], {}, 'store:forged', {})
    with pytest.raises(ContractValidationError):
        store.commit([('x', 'first', {}), ('x', 'bad', {'n': float('inf')})],
                     {('x', 'first'): None, ('x', 'bad'): None}, 'bad', {})
    assert store.get('x', 'first') is None
    assert store.audit() == ()


def test_stale_dependency_reserve_and_duplicate_consume(tmp_path):
    store = WorkflowStore(tmp_path / 'state.db')
    seed(store)
    with pytest.raises(ContractValidationError):
        store.reserve('g', 'a', 'op', AT, {('baseline', 'b'): 'stale'})
    assert store.get('grant', 'g')['state'] == 'ACTIVE'
    store.reserve('g', 'a', 'op', AT)
    first = store.consume('a', AT)
    count = len(store.audit())
    assert store.consume('a', LATER) == first
    assert len(store.audit()) == count
