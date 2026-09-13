"""Small local outbox for end facts when a project disk cannot be written."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path


def pending_directory(run_file):
    ledger = str(Path(run_file).expanduser().resolve())
    root = Path(os.environ.get('STORY_OPERATION_STATE_DIR', str(Path.home() / '.cache/story-operation-recovery')))
    return root / hashlib.sha256(ledger.encode()).hexdigest()


def _write_fact_marker(run_file, run_id, fact):
    from story_run import atomic_write_json
    from story_evidence_store import externalize_evidence
    if not run_id:
        raise ValueError('Cannot spool an unidentified operation')
    directory = pending_directory(run_file)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (fact['operation_id'] + '.json')
    value = {'schema_version': 'story-pending-operation-fact/v1', 'run_id': run_id,
             'owner_protocol': 'kernel-flock/v1',
             'ledger': str(Path(run_file).expanduser().resolve()),
             'fact': externalize_evidence(fact, directory / 'evidence_members')}
    atomic_write_json(path, value)
    return path


def spool_end_fact(run_file, run_id, fact):
    if fact.get('status') == 'running' or not fact.get('ended_at'):
        raise ValueError('Cannot spool an unfinished end fact')
    return _write_fact_marker(run_file, run_id, fact)


@contextmanager
def operation_owner(run_file, fact):
    """Persist intent before the ledger start, and hold ownership until finally.

    The lock is intentionally not inherited by unrelated subprocesses. Encoders
    separately hold their inherited output locks, also checked during replay.
    """
    from story_run import load_run
    run_id = load_run(Path(run_file)).get('run_id')
    directory = pending_directory(run_file)
    directory.mkdir(parents=True, exist_ok=True)
    owner_path = directory / (fact['operation_id'] + '.owner.lock')
    with owner_path.open('a+') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_fact_marker(run_file, run_id, fact)
        yield run_id


def replay_pending_facts(run_file):
    """Idempotently replay only the same run and an unowned operation tree.

    Completion facts remain observations, never independent QA. Current output
    hashes are checked again. A live encoder's kernel lock keeps its parent and
    child facts pending; PIDs and timeout guesses are deliberately unused.
    """
    from story_run import run_file_lock, load_run, atomic_write_json
    from story_production_v2 import current, binding
    from story_evidence_store import resolve_evidence, externalize_evidence
    from story_encode import state_directory
    ledger = str(Path(run_file).expanduser().resolve())
    directory = pending_directory(run_file)
    if not directory.is_dir():
        return []
    restored = []
    with run_file_lock(Path(run_file)):
        run = load_run(Path(run_file))
        events = run['observability']['events']
        latest = {x['operation_id']: x for x in events if x.get('event') == 'operation_fact'}
        for pending in sorted(directory.glob('*.json')):
            value = json.loads(pending.read_text())
            if value.get('schema_version') != 'story-pending-operation-fact/v1' or value.get('ledger') != ledger or value.get('run_id') != run.get('run_id'):
                continue  # same location can later contain an entirely different project
            fact = resolve_evidence(value['fact'])
            operation_id = fact.get('operation_id')
            old = latest.get(operation_id)
            if not old or old.get('started_at') != fact.get('started_at'):
                continue
            if old.get('status') != 'running':
                continue  # already applied or explicitly reconciled; no duplicate event
            interrupted_start = fact.get('status') == 'running' and value.get('owner_protocol') == 'kernel-flock/v1'
            if not interrupted_start and (fact.get('status') not in {'complete', 'failed', 'cancelled', 'deferred'} or not fact.get('ended_at')):
                raise ValueError('Invalid pending end fact')
            descendants = {operation_id}
            while True:
                expanded = descendants | {key for key, row in latest.items() if row.get('parent_operation_id') in descendants}
                if expanded == descendants:
                    break
                descendants = expanded
            try:
                with ExitStack() as locks:
                    # A live parent is protected even before its first encoder
                    # exists. Nested deterministic children use this same lock.
                    for child_id in sorted(descendants):
                        owner_path = directory / (child_id + '.owner.lock')
                        if owner_path.exists():
                            owner = locks.enter_context(owner_path.open('a+'))
                            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    for receipt in state_directory().glob('*.json'):
                        state = json.loads(receipt.read_text())
                        if state.get('ledger') == ledger and state.get('operation_id') in descendants:
                            owner = locks.enter_context(receipt.with_suffix('.lock').open('a+'))
                            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if json.loads(pending.read_text()) != value:
                        continue  # owner published its end between our read and lock acquisition
                    if interrupted_start:
                        recovered_at = datetime.now(timezone.utc)
                        try:
                            started = datetime.fromisoformat(fact['started_at'])
                            span = max(0.0, (recovered_at - started).total_seconds())
                        except (KeyError, TypeError, ValueError):
                            span = None
                        fact.update(status='failed', error_type='operation_owner_interrupted', artifacts=[],
                                    ended_at=None, duration_seconds=None,
                                    recovered_at=recovered_at.isoformat(), recovery_wallspan_seconds=span)
                    if fact['status'] == 'complete':
                        try:
                            for artifact in fact.get('artifacts', []):
                                if not Path(artifact['path']).parent.exists():
                                    raise FileNotFoundError('Output storage remains unavailable')
                                current(artifact)
                        except ValueError:
                            fact.update(status='failed', error_type='recovered_output_hash_changed', artifacts=[])
                    fact['recovery_evidence'] = {'pending_fact': binding(pending),
                        'replayed_at': datetime.now(timezone.utc).isoformat(),
                        'run_id_verified': True, 'managed_owner_locks_free': True,
                        'operation_owner_locks_free': True}
                    fact = externalize_evidence(fact, Path(run_file).parent / 'evidence_members')
                    event = {'sequence': len(events) + 1, 'observed_at': datetime.now(timezone.utc).isoformat(),
                             'event': 'operation_fact', **fact}
                    events.append(event)
                    try:
                        atomic_write_json(Path(run_file), run)
                    except OSError:
                        events.pop()
                        raise
                    latest[operation_id] = event
                    restored.append(operation_id)
            except OSError:
                continue  # output disk still absent or owner still alive: remain pending
    return restored
