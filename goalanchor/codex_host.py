"""Bounded public Codex App Server stdio transport, without approval policy.

Server requests are delivered verbatim to the controlling application. Receiving
one never grants authority, starts a turn, or approves an operation.
"""
from __future__ import annotations

import json
import math
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from typing import Any

from ._version import __version__


class CodexTransportError(RuntimeError):
    """Transport failure; messages deliberately exclude remote payloads."""


def _argv() -> list[str]:
    executable = shutil.which('codex')
    if not executable:
        raise CodexTransportError('Codex executable was not found')
    path = Path(executable)
    if path.suffix.lower() in {'.cmd', '.ps1'}:
        node = shutil.which('node')
        script = path.parent / 'node_modules/@openai/codex/bin/codex.js'
        if not node or not script.is_file():
            raise CodexTransportError('Codex public npm entrypoint or Node was not found')
        return [node, str(script), 'app-server']
    return [executable, 'app-server']


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError('non-finite JSON')


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite JSON')
    return result


class CodexClient:
    """One subprocess; concurrent requests and explicit server-request responses."""

    def __init__(self, argv: Sequence[str] | None = None, cwd: str | Path | None = None,
                 timeout: float = 30, env: Mapping[str, str] | None = None):
        self.timeout = self._duration(timeout)
        command = list(argv) if argv is not None else _argv()
        if not command or any(not isinstance(x, str) or not x or '\0' in x for x in command):
            raise CodexTransportError('Invalid subprocess argv')
        self._lock = threading.RLock()
        self._initialize_lock = threading.Lock()
        self._pending: dict[int, Future[Any]] = {}
        self._writing: set[Future[None]] = set()
        self._server_ids: set[int | str] = set()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1024)
        self._writes: queue.Queue[tuple[bytes, Future[None]] | None] = queue.Queue(maxsize=128)
        self._next_id = 0
        self._failure: CodexTransportError | None = None
        self._initialized: dict[str, Any] | None = None
        self._closed = False
        try:
            self._process = subprocess.Popen(
                command, cwd=cwd, env=dict(env) if env is not None else None,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except (OSError, ValueError):
            raise CodexTransportError('Could not start Codex subprocess') from None
        self._threads = [threading.Thread(target=target, daemon=True)
                         for target in (self._read, self._drain, self._write)]
        for thread in self._threads:
            thread.start()

    @staticmethod
    def _duration(value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise CodexTransportError('Timeout must be finite and positive')
        return value

    def __enter__(self) -> CodexClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _fail(self, message: str) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = CodexTransportError(message)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(self._failure)
            self._pending.clear()
            for writing in self._writing:
                if not writing.done():
                    writing.set_exception(self._failure)
            self._writing.clear()

    def _check(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _send(self, payload: dict[str, Any], timeout: float,
              *, server_request_id: int | str | None = None) -> None:
        try:
            raw = (json.dumps(payload, allow_nan=False, ensure_ascii=False) + '\n').encode('utf-8')
        except (ValueError, TypeError, UnicodeError):
            raise CodexTransportError('Invalid outgoing JSON') from None
        if len(raw) > 1048576:
            raise CodexTransportError('Outgoing message exceeds size limit')
        future: Future[None] = Future()
        with self._lock:
            self._check()
            if server_request_id is not None:
                if type(server_request_id) not in (int, str) or server_request_id not in self._server_ids:
                    raise CodexTransportError('Unknown or already answered server request')
            try:
                self._writes.put_nowait((raw, future))
                self._writing.add(future)
            except queue.Full:
                raise CodexTransportError('Outgoing queue is full') from None
            if server_request_id is not None:
                self._server_ids.remove(server_request_id)
        try:
            future.result(timeout)
        except TimeoutError:
            self._fail('Codex write timed out')
            raise CodexTransportError('Codex write timed out') from None
        finally:
            with self._lock:
                self._writing.discard(future)

    def _write(self) -> None:
        assert self._process.stdin is not None
        while True:
            item = self._writes.get()
            if item is None:
                return
            raw, future = item
            try:
                with self._lock:
                    self._check()
                self._process.stdin.write(raw)
                self._process.stdin.flush()
                with self._lock:
                    if not future.done():
                        future.set_result(None)
            except (OSError, ValueError, CodexTransportError):
                with self._lock:
                    if not future.done():
                        future.set_exception(CodexTransportError('Codex stdin failed'))
                self._fail('Codex stdin failed')

    def _drain(self) -> None:
        assert self._process.stderr is not None
        try:
            while self._process.stderr.read(8192):
                pass
        except (OSError, ValueError):
            pass

    def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            while True:
                raw = self._process.stdout.readline(1048577)
                if not raw:
                    self._fail('Codex stdout reached EOF')
                    return
                if len(raw) > 1048576 or not raw.endswith(b'\n'):
                    raise ValueError('oversized or incomplete line')
                message = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                                     parse_constant=_constant, parse_float=_float)
                if not isinstance(message, dict):
                    raise ValueError('non-object message')
                with self._lock:
                    if 'method' in message:
                        if not isinstance(message['method'], str):
                            raise ValueError('invalid method')
                        if 'id' in message:
                            ident = message['id']
                            if (type(ident) not in (int, str) or ident in self._server_ids
                                    or len(self._server_ids) >= 1024):
                                raise ValueError('invalid server request ID')
                            self._server_ids.add(ident)
                        self._events.put_nowait(message)
                    else:
                        ident = message.get('id')
                        if type(ident) is not int or ('result' in message) == ('error' in message):
                            raise ValueError('invalid response')
                        future = self._pending.pop(ident, None)
                        if future is not None:
                            if 'error' in message:
                                future.set_exception(CodexTransportError('Codex returned a request error'))
                            else:
                                future.set_result(message['result'])
        except (ValueError, OSError, UnicodeError, queue.Full, RecursionError):
            self._fail('Invalid Codex stdout protocol')

    def initialize(self) -> dict[str, Any]:
        with self._initialize_lock:
            if self._initialized is None:
                result = self.request('initialize', {
                    'clientInfo': {'name': 'goalanchor', 'version': __version__},
                    'capabilities': {'experimentalApi': True},
                })
                if not isinstance(result, dict):
                    raise CodexTransportError('Invalid initialize result')
                self._send({'method': 'initialized'}, self.timeout)
                self._initialized = result
            return self._initialized

    def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        duration = self._duration(self.timeout if timeout is None else timeout)
        deadline = time.monotonic() + duration
        future: Future[Any] = Future()
        with self._lock:
            self._check()
            self._next_id += 1
            ident = self._next_id
            self._pending[ident] = future
        try:
            self._send({'id': ident, 'method': method, 'params': params}, duration)
            return future.result(max(.001, deadline - time.monotonic()))
        except TimeoutError:
            raise CodexTransportError('Codex request timed out') from None
        finally:
            with self._lock:
                self._pending.pop(ident, None)

    def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + self._duration(self.timeout if timeout is None else timeout)
        while True:
            try:
                return self._events.get_nowait()
            except queue.Empty:
                self._check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexTransportError('Codex event wait timed out')
            try:
                return self._events.get(timeout=min(.05, remaining))
            except queue.Empty:
                pass

    def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        if type(request_id) not in (int, str):
            raise CodexTransportError('Unknown or already answered server request')
        self._send({'id': request_id, 'result': result}, self.timeout, server_request_id=request_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._fail('Codex transport closed')
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        try:
            self._writes.put_nowait(None)
        except queue.Full:
            pass
        for thread in self._threads:
            thread.join(timeout=1)
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None:
                stream.close()


def command_approval(params: dict[str, Any]) -> dict[str, Any]:
    """Validate a command callback without granting approval or broadening scope.

    Callers retain the complete server request (including its ID) for correlation.
    Rich permissions, stdin, and file-change approval are deliberately unsupported.
    A proposed execpolicy amendment is diagnostic; callers must never apply it.
    """
    known = {'threadId', 'turnId', 'itemId', 'command', 'cwd', 'kind', 'approvalId',
             'startedAtMs', 'reason', 'commandActions', 'availableDecisions',
             'additionalPermissions', 'networkApprovalContext', 'proposedExecpolicyAmendment',
             'proposedNetworkPolicyAmendments', 'environmentId'}
    if params.keys() - known:
        raise CodexTransportError('Unknown command approval fields')
    for key in ('threadId', 'turnId', 'itemId', 'command', 'cwd'):
        if not isinstance(params.get(key), str) or not params[key].strip():
            raise CodexTransportError('Incomplete command approval')
    if params.get('kind', 'command') != 'command':
        raise CodexTransportError('Unsupported approval kind')
    for key in ('additionalPermissions', 'networkApprovalContext', 'proposedNetworkPolicyAmendments'):
        if params.get(key) is not None:
            raise CodexTransportError('Unsupported command permission scope')
    if params.get('environmentId') not in (None, 'local'):
        raise CodexTransportError('Unsupported command environment')
    proposal = params.get('proposedExecpolicyAmendment')
    if proposal is not None and (not isinstance(proposal, list)
                                 or any(not isinstance(item, str) or not item for item in proposal)):
        raise CodexTransportError('Invalid proposed execpolicy amendment')
    return json.loads(json.dumps(params, allow_nan=False))  # type: ignore[no-any-return]


def user_question_payload(params: dict[str, Any]) -> dict[str, Any]:
    """Preserve question identities and labels; autoResolutionMs is diagnostic only."""
    for key in ('threadId', 'turnId', 'itemId'):
        if not isinstance(params.get(key), str) or not params[key]:
            raise CodexTransportError('Incomplete question correlation')
    questions = params.get('questions')
    if not isinstance(questions, list) or not questions:
        raise CodexTransportError('Missing questions')
    ids: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            raise CodexTransportError('Invalid question')
        for key in ('id', 'header', 'question'):
            if not isinstance(question.get(key), str) or not question[key]:
                raise CodexTransportError('Incomplete question')
        if question['id'] in ids:
            raise CodexTransportError('Duplicate question identity')
        ids.add(question['id'])
    return json.loads(json.dumps(params, allow_nan=False))  # type: ignore[no-any-return]
