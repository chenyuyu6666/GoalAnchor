"""Subprocess protocol simulations; these are not native Codex acceptance tests."""
import concurrent.futures
import queue
import sys
import time

import pytest

from goalanchor.codex_host import CodexClient, CodexTransportError, command_approval, user_question_payload

SERVER = r'''
import json, sys, time
def send(x):
 print(json.dumps(x), flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if 'method' not in r:
  send({'method':'responded','params':r}); continue
 m=r['method']
 if m=='initialized': continue
 if m=='hang': time.sleep(10)
 elif m=='eof': sys.exit(0)
 elif m=='bad': print('{"id":1,"id":2}',flush=True)
 elif m=='nan': print('{"result":NaN,"id":1}',flush=True)
 elif m=='overflow': print('{"result":1e999,"id":1}',flush=True)
 elif m=='long': print('x'*1100000,flush=True)
 elif m=='stderr':
  sys.stderr.write('SECRET_TOKEN'*100000); sys.stderr.flush()
  send({'id':r['id'],'result':sys.argv[1:]})
 elif m=='interleave':
  send({'method':'notice','params':{}})
  send({'method':'item/commandExecution/requestApproval','id':r['id'],'params':{'command':'sensitive'}})
  send({'id':r['id'],'result':r['params']})
 elif m=='error': send({'id':r['id'],'error':{'message':'SECRET_TOKEN'}})
 else: send({'id':r['id'],'result':r['params']})
'''


def client():
    return CodexClient([sys.executable, '-u', '-c', SERVER], timeout=2)


def test_interleaving_and_explicit_cancel():
    with client() as c:
        result = c.initialize()
        assert result['capabilities']['experimentalApi'] is True
        assert c.initialize() == result
        assert c.request('interleave', {'literal': '& " spaces'}) == {'literal': '& " spaces'}
        assert c.next_event()['method'] == 'notice'
        approval = c.next_event()
        assert approval['method'].endswith('requestApproval')
        with pytest.raises(CodexTransportError, match='timed out'):
            c.next_event(timeout=.05)
        c.respond(approval['id'], {'decision': 'cancel'})
        assert c.next_event()['params']['result'] == {'decision': 'cancel'}
        with pytest.raises(CodexTransportError):
            c.respond(approval['id'], {'decision': 'accept'})


@pytest.mark.parametrize('failure', ['nan', 'oversized', 'queue_full'])
def test_response_pre_enqueue_failure_allows_corrected_cancel(failure, monkeypatch):
    with client() as c:
        c.request('interleave', {})
        c.next_event()
        approval = c.next_event()
        result = {'bad': float('nan')} if failure == 'nan' else {'bad': 'x' * 1048576}
        with monkeypatch.context() as patch:
            if failure == 'queue_full':
                def full(item):
                    raise queue.Full
                patch.setattr(c._writes, 'put_nowait', full)
                result = {'decision': 'cancel'}
            with pytest.raises(CodexTransportError):
                c.respond(approval['id'], result)
        c.respond(approval['id'], {'decision': 'cancel'})
        assert c.next_event()['params']['result'] == {'decision': 'cancel'}
        with pytest.raises(CodexTransportError, match='already answered'):
            c.respond(approval['id'], {'decision': 'cancel'})


def test_concurrent_responses_send_exactly_once():
    with client() as c, concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        c.request('interleave', {})
        c.next_event()
        approval = c.next_event()
        futures = [pool.submit(c.respond, approval['id'], {'decision': 'cancel'}) for _ in range(8)]
        successes = 0
        for future in futures:
            try:
                future.result()
                successes += 1
            except CodexTransportError as exc:
                assert 'already answered' in str(exc)
        assert successes == 1
        assert c.next_event()['params']['result'] == {'decision': 'cancel'}
        with pytest.raises(CodexTransportError, match='timed out'):
            c.next_event(timeout=.05)


@pytest.mark.parametrize('method', ['eof', 'bad', 'nan', 'overflow', 'long', 'error'])
def test_failure_is_explicit_and_redacted(method):
    with client() as c:
        with pytest.raises(CodexTransportError) as caught:
            c.request(method, {})
        assert 'SECRET_TOKEN' not in str(caught.value)


def test_timeout_and_close_wake_pending():
    c = client()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(c.request, 'hang', {})
        time.sleep(.1)
        c.close()
        with pytest.raises(CodexTransportError):
            pending.result(timeout=3)
    c.close()
    with pytest.raises(CodexTransportError):
        c.request('hello', {})
    with client() as c:
        with pytest.raises(CodexTransportError, match='timed out'):
            c.request('hang', {}, timeout=.05)


def test_helpers_fail_closed_and_preserve_question_ids():
    p = dict(threadId='t', turnId='u', itemId='i', command='echo hi', cwd='C:/x')
    assert command_approval(p)['command'] == 'echo hi'
    for key in p:
        with pytest.raises(CodexTransportError):
            command_approval({k: v for k, v in p.items() if k != key})
    with pytest.raises(CodexTransportError):
        command_approval(dict(p, kind='writeStdin'))
    with pytest.raises(CodexTransportError):
        command_approval(dict(p, futurePermission='unknown'))
    q = dict(p, questions=[{'id': 'stable', 'header': 'H', 'question': '?',
                            'options': [{'label': 'Yes', 'description': 'D'}]}], autoResolutionMs=1)
    assert user_question_payload(q)['questions'] == q['questions']
    assert user_question_payload(q)['autoResolutionMs'] == 1


def test_concurrent_requests_keep_client_ids_separate():
    with client() as c, concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(c.request, 'echo', {'number': n}) for n in range(30)]
        assert [f.result()['number'] for f in futures] == list(range(30))


def test_stderr_is_drained_and_argv_boundaries_are_preserved():
    arguments = ['space value', '& echo injected', '"quoted"', '中文']
    with CodexClient([sys.executable, '-u', '-c', SERVER, *arguments], timeout=3) as c:
        assert c.request('stderr', {}) == arguments


def test_default_public_npm_launch_path(tmp_path, monkeypatch):
    from goalanchor.codex_host import _argv

    shim = tmp_path / 'codex.cmd'
    script = tmp_path / 'node_modules/@openai/codex/bin/codex.js'
    script.parent.mkdir(parents=True)
    script.write_text('// public entrypoint')
    monkeypatch.setattr('goalanchor.codex_host.shutil.which',
                        lambda name: str(shim) if name == 'codex' else 'node.exe')
    assert _argv() == ['node.exe', str(script), 'app-server']
    script.unlink()
    with pytest.raises(CodexTransportError):
        _argv()
    monkeypatch.setattr('goalanchor.codex_host.shutil.which', lambda name: None)
    with pytest.raises(CodexTransportError):
        _argv()


def test_local_environment_approval_is_preserved_without_remote_scope():
    params = {'threadId': 'T', 'turnId': 'R', 'itemId': 'I', 'command': 'echo marker',
              'cwd': '/workspace', 'environmentId': 'local'}
    assert command_approval(params) == params
    with pytest.raises(CodexTransportError):
        command_approval({**params, 'environmentId': 'remote'})


def test_proposed_execpolicy_is_only_preserved_not_applied():
    params = {'threadId': 'T', 'turnId': 'R', 'itemId': 'I', 'command': 'echo marker',
              'cwd': '/workspace', 'proposedExecpolicyAmendment': ['echo']}
    assert command_approval(params) == params
    with pytest.raises(CodexTransportError):
        command_approval({**params, 'proposedExecpolicyAmendment': [42]})
