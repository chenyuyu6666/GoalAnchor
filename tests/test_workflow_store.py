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


def test_commit_many_events_and_records_is_atomic_and_idempotent(tmp_path):
    store = WorkflowStore(tmp_path / 'state.db')
    initial_goal = {'goal_id': 'goal', 'state': 'VERIFY', 'generation': 0}
    store.commit(
        [('goal', 'goal', initial_goal)], {('goal', 'goal'): None}, 'seed-goal', {'kind': 'seed'}
    )
    goal = store.get('goal', 'goal')
    generation_before = goal['generation']
    changes = [
        ('strict_question', 'q-1', {'goal_id': 'goal', 'state': 'ANSWERED'}),
        ('strict_grant', 'g-1', {'goal_id': 'goal', 'state': 'ACTIVE'}),
    ]
    expected = {
        ('goal', 'goal'): store.fingerprint(goal),
        ('strict_question', 'q-1'): None,
        ('strict_grant', 'g-1'): None,
    }
    events = [
        ('answer-event-1', {'kind': 'user.answer', 'question_id': 'q-1'}),
        ('grant-event-1', {'kind': 'controller.strict_grant', 'question_id': 'q-1', 'grant_id': 'g-1'}),
    ]

    # This store test verifies atomic persistence; caller-side code verifies event-to-record meaning.
    assert store.commit_many(changes, expected, events)
    assert store.get('strict_question', 'q-1') == changes[0][2]
    assert store.get('strict_grant', 'g-1') == changes[1][2]
    assert store.get('goal', 'goal')['generation'] == generation_before + 1
    assert [item['event_id'] for item in store.audit()] == [
        'seed-goal', 'answer-event-1', 'grant-event-1'
    ]
    assert not store.commit_many(changes, expected, events)
    reopened = WorkflowStore(tmp_path / 'state.db')
    assert reopened.get('strict_question', 'q-1') == changes[0][2]
    assert reopened.get('strict_grant', 'g-1') == changes[1][2]
    assert reopened.get('goal', 'goal')['generation'] == generation_before + 1

    stale_changes = [
        ('strict_question', 'q-2', {'goal_id': 'goal', 'state': 'ANSWERED'}),
        ('strict_grant', 'g-2', {'goal_id': 'goal', 'state': 'ACTIVE'}),
    ]
    stale_expected = {
        ('goal', 'goal'): store.fingerprint(store.get('goal', 'goal')),
        ('strict_question', 'q-2'): None,
        ('strict_grant', 'g-2'): 'stale',
    }
    stale_events = [
        ('answer-event-3', {'kind': 'user.answer', 'question_id': 'q-2'}),
        ('grant-event-2', {'kind': 'controller.strict_grant', 'question_id': 'q-2', 'grant_id': 'g-2'}),
    ]
    with pytest.raises(ContractValidationError):
        store.commit_many(stale_changes, stale_expected, stale_events)

    assert store.get('strict_question', 'q-2') is None
    assert store.get('strict_grant', 'g-2') is None
    assert store.get_event('answer-event-3') is None
    assert store.get_event('grant-event-2') is None
    assert store.get('goal', 'goal')['generation'] == generation_before + 1
    assert [item['event_id'] for item in store.audit()] == [
        'seed-goal', 'answer-event-1', 'grant-event-1'
    ]

    invalid_changes = [
        ('strict_question', 'q-3', {'goal_id': 'goal', 'state': 'ANSWERED'}),
        ('strict_grant', 'g-3', {'goal_id': 'goal', 'expires_at': float('inf')}),
    ]
    invalid_expected = {
        ('goal', 'goal'): store.fingerprint(store.get('goal', 'goal')),
        ('strict_question', 'q-3'): None,
        ('strict_grant', 'g-3'): None,
    }
    invalid_events = [
        ('answer-event-5', {'kind': 'user.answer', 'question_id': 'q-3'}),
        ('grant-event-3', {'kind': 'controller.strict_grant', 'question_id': 'q-3', 'grant_id': 'g-3'}),
    ]
    with pytest.raises(ContractValidationError):
        store.commit_many(invalid_changes, invalid_expected, invalid_events)

    assert store.get('strict_question', 'q-3') is None
    assert store.get('strict_grant', 'g-3') is None
    assert store.get_event('answer-event-5') is None
    assert store.get_event('grant-event-3') is None
    assert store.get('goal', 'goal')['generation'] == generation_before + 1
    assert [item['event_id'] for item in store.audit()] == [
        'seed-goal', 'answer-event-1', 'grant-event-1'
    ]


def test_commit_many_rejects_partial_event_replay_without_partial_writes(tmp_path):
    store = WorkflowStore(tmp_path / 'state.db')
    store.commit([], {}, 'already-recorded', {'kind': 'user.answer', 'question_id': 'q-2'})
    changes = [('strict_question', 'q-2', {'goal_id': 'goal', 'state': 'ANSWERED'})]
    events = [
        ('already-recorded', {'kind': 'user.answer', 'question_id': 'q-2'}),
        ('not-yet-recorded', {'kind': 'user.answer', 'question_id': 'q-2'}),
    ]

    with pytest.raises(ContractValidationError):
        store.commit_many(changes, {('strict_question', 'q-2'): None}, events)

    assert store.get('strict_question', 'q-2') is None
    assert store.get_event('not-yet-recorded') is None
    assert [item['event_id'] for item in store.audit()] == ['already-recorded']


def test_commit_many_rechecks_valid_until_after_record_writes(monkeypatch, tmp_path):
    store = WorkflowStore(tmp_path / 'expiry-before-commit.db')
    clock_value = [AT]
    original_put = store._put

    def expire_after_write(db, kind, key, payload):
        original_put(db, kind, key, payload)
        if kind == 'strict_operation':
            clock_value[0] = LATER

    monkeypatch.setattr(store, '_put', expire_after_write)

    with pytest.raises(ContractValidationError, match='expired before commit'):
        store.commit(
            [('strict_operation', 'op-1', {'goal_id': 'goal', 'operation_digest': 'digest'})],
            {('strict_operation', 'op-1'): None},
            'operation-event',
            {'kind': 'controller.operation'},
            valid_until=LATER,
            clock=lambda: clock_value[0],
        )

    assert store.get('strict_operation', 'op-1') is None
    assert store.get_event('operation-event') is None
    assert store.audit() == ()


def test_commit_many_checks_valid_until_after_slow_pre_commit_callback(tmp_path):
    store = WorkflowStore(tmp_path / 'expiry-in-pre-commit-callback.db')
    initial_goal = {'goal_id': 'goal', 'generation': 0}
    store.commit([('goal', 'goal', initial_goal)], {('goal', 'goal'): None}, 'seed-goal', {'kind': 'seed'})
    goal_before = store.get('goal', 'goal')
    clock_value = [AT]
    callback_count = 0

    def expire_during_final_callback():
        nonlocal callback_count
        callback_count += 1
        if callback_count == 2:
            clock_value[0] = LATER

    with pytest.raises(ContractValidationError, match='expired before commit'):
        store.commit_many(
            [('strict_operation', 'op-1', {'goal_id': 'goal', 'operation_digest': 'digest'})],
            {
                ('goal', 'goal'): store.fingerprint(goal_before),
                ('strict_operation', 'op-1'): None,
            },
            [('operation-event', {'kind': 'controller.operation'})],
            valid_until=LATER,
            clock=lambda: clock_value[0],
            pre_commit_check=expire_during_final_callback,
        )

    assert callback_count == 2
    assert store.get('strict_operation', 'op-1') is None
    assert store.get_event('operation-event') is None
    assert store.get('goal', 'goal') == goal_before
    assert [item['event_id'] for item in store.audit()] == ['seed-goal']


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
