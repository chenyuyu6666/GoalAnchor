"""Controller-side workflow inspection and an isolated local demonstration.

No command accepts model-supplied approval identities or exposes signing keys.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .core import ContractValidationError
from .host_contract import PROTOCOL, decode_envelope, envelope
from .workflow_protocol import canonical, strict_loads, utc_now
from .workflow_store import WorkflowStore

KINDS = frozenset({'status', 'audit', 'recover', 'capabilities', 'demo'})


def parse_envelope(raw: str) -> dict[str, Any]:
    kind, data = decode_envelope(raw)
    if kind not in KINDS:
        raise ContractValidationError('Unsupported workflow envelope kind')
    return {'protocol': PROTOCOL, 'kind': kind, 'data': data}


def _existing_database(raw: str) -> Path:
    path = Path(raw).resolve(strict=True)
    if not path.is_file():
        raise ContractValidationError('Database must be an existing file')
    return path


def _read_database(path: Path, command: str, *, allow_wal: bool = False) -> dict[str, Any]:
    # WAL readonly connections can create -wal/-shm. Do not open one for inspection.
    with path.open('rb') as source:
        header = source.read(100)
    if len(header) < 100 or header[:16] != b'SQLite format 3\x00':
        raise ContractValidationError('Invalid workflow database header')
    if not allow_wal and (header[18] == 2 or header[19] == 2):
        raise ContractValidationError(
            'WAL databases are unsupported by zero-write inspection; use an offline '
            'checkpointed database explicitly switched to journal_mode=DELETE')
    uri = path.as_uri() + ('?mode=ro' if allow_wal else '?mode=ro&immutable=1')
    with sqlite3.connect(uri, uri=True) as db:
        if db.execute('PRAGMA user_version').fetchone()[0] not in (1, 2):
            raise ContractValidationError('Unsupported workflow schema')
        if command == 'audit':
            rows = db.execute('SELECT seq,event_id,payload FROM events ORDER BY seq').fetchall()
            return {'events': [{'seq': seq, 'event_id': event_id, 'payload': strict_loads(payload)}
                               for seq, event_id, payload in rows]}
        rows = db.execute("SELECT payload FROM records WHERE kind='goal' ORDER BY key").fetchall()
        receipts = db.execute("SELECT payload FROM records WHERE kind='receipt' ORDER BY key").fetchall()
        return {'goals': [strict_loads(row[0]) for row in rows],
                'receipts': [strict_loads(row[0]) for row in receipts]}


def run_demo(directory: Path) -> dict[str, Any]:
    """Synthetic local integration, never a claim of independent model review."""
    from .review_delivery import build_packets
    from .review_service import ArbiterResult, MetaReviewResult, ReviewerResult, ReviewVerdict
    from .workflow import Workflow, WorkflowPolicy
    from .workflow_protocol import Baseline, Criterion, EventAuthority, EvidenceSnapshot, Operation, fingerprint

    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    user = EventAuthority(secrets.token_bytes(32), 'demo-trusted-controller')
    observer = EventAuthority(secrets.token_bytes(32), 'demo-local-observer')
    runtime = Workflow(WorkflowStore(directory / 'state.db'), user, observer,
                       WorkflowPolicy(str(directory), ('write_text',)))
    base = Baseline('demo', 1, 'Write a verified local artifact', 'Write once, read back, compare',
                    (Criterion('content', 'Artifact contains done', canonical('done'), 'artifact'),),
                    required_operations=('write',))
    runtime.start_baseline(base, user.sign('user.baseline', base.fingerprint, base.to_dict()))
    snapshot = EvidenceSnapshot.capture('facts', 'demo-plan', b'isolated local demo', source='demo')
    runtime.add_evidence('demo', snapshot, observer.sign('observer.evidence', snapshot.fingerprint,
                         {'goal_id': 'demo', 'snapshot': snapshot.to_dict()}))
    ctx = runtime.review_context('demo')
    reviewers = [ReviewerResult(name, 1, ReviewVerdict.PASS, (), True, ctx.fingerprint)
                 for name in ('reviewer-a', 'reviewer-b')]
    arbiter = ArbiterResult('arbiter', 1, ReviewVerdict.PASS, 'Synthetic demo', 'Frozen demo facts',
                            ('reviewer-a', 'reviewer-b'), ctx.fingerprint, False)
    meta = MetaReviewResult('meta', 1, 'arbiter', ReviewVerdict.PASS, 'Synthetic demo', (), ctx.fingerprint, True)
    bundle: dict[str, Any] = {'context': ctx.to_dict(), 'risk': 'HIGH', 'round_number': 1, 'max_rounds': 2,
              'reviewer_results': [item.to_dict() for item in reviewers], 'arbiter': arbiter.to_dict(),
              'meta_review': meta.to_dict(), 'dispositions': {},
              'bindings': [{'agent_id': name, 'session_id': 'demo-' + name, 'model': 'synthetic-demo'}
                           for name in ('reviewer-a', 'reviewer-b', 'arbiter', 'meta')]}
    packets = build_packets(ctx, bundle, [])
    bundle['delivery_receipts'] = [
        {'agent_id': binding['agent_id'], 'session_id': binding['session_id'],
         'input_digest': fingerprint(packets[binding['agent_id']]),
         'transport_receipt_id': 'synthetic-' + binding['session_id'], 'status': 'DELIVERED'}
        for binding in bundle['bindings']]
    runtime.submit_review('demo', bundle, observer.sign('observer.review', ctx.fingerprint, bundle))
    operation = Operation('write', 'demo', base.fingerprint, 'write_text', canonical({'text': 'done'}),
                          str(directory), 'write-item')
    runtime.register_operation(operation)
    question = runtime.request_authorization('write').question
    runtime.answer(question.question_id, user.sign('user.answer', question.fingerprint,
                   {'answer_id': 'demo-answer', 'selected': ['approve'], 'custom': None}))

    def execute(_: Operation) -> dict[str, Any]:
        (directory / 'artifact.txt').write_text('done', encoding='utf-8')
        return {'exit_code': 0, 'object_ref': 'artifact'}

    runtime.execute('write', 'demo-attempt', execute)
    observation = {'goal_id': 'demo', 'baseline_digest': base.fingerprint, 'criterion_id': 'content',
                   'object_ref': 'artifact', 'actual': (directory / 'artifact.txt').read_text(encoding='utf-8'),
                   'attempt_id': 'demo-attempt'}
    runtime.observe('demo', observer.sign('observer.observation', base.fingerprint, observation))
    result = runtime.close('demo')
    return {'state': result['state'], 'directory': str(directory), 'review_mode': 'synthetic-demo',
            'host_execution_verified': False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='goalanchor workflow')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'audit', 'recover'):
        commands.add_parser(name).add_argument('--database', required=True)
    commands.add_parser('capabilities')
    commands.add_parser('demo').add_argument('--directory', required=True)
    args = parser.parse_args(argv)
    try:
        data: dict[str, Any]
        if args.command == 'capabilities':
            data = {'model_can_sign': False, 'host_execution_verified': False,
                    'local_demo': True, 'file_change_approval': False, 'protocol': PROTOCOL,
                    'zero_write_inspection': 'OFFLINE_DELETE_JOURNAL_ONLY'}
        elif args.command == 'demo':
            data = run_demo(Path(args.directory))
        else:
            path = _existing_database(args.database)
            if args.command == 'recover':
                _read_database(path, 'status', allow_wal=True)  # recovery is an explicit writer
                data = {'recovered': list(WorkflowStore(path).recover(at=utc_now())), 'dispatched': False}
            else:
                data = _read_database(path, args.command)
        print(envelope(args.command, data))
        return 0
    except (ContractValidationError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
