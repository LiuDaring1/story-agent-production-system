"""Local FFmpeg ownership, cancellable resource queues and a shared encode pool."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from story_production_v2 import binding, sha, write


def _write_receipt(path, state):
    from story_evidence_store import externalize_evidence
    value = dict(state)
    if len(value.get('inputs', [])) >= 64:
        value['inputs'] = {'schema_version': 'story-encode-file-list/v1', 'members': value['inputs']}
    if isinstance(value.get('dependencies'), dict):
        value['dependencies'] = dict(value['dependencies'])
        files = value['dependencies'].get('files', [])
        if len(files) >= 64:
            value['dependencies']['files'] = {'schema_version': 'story-encode-file-list/v1', 'members': files}
    from story_hash_cache import hash_cache_scope
    with hash_cache_scope():
        write(path, externalize_evidence(value, Path(path).parent / 'evidence_members'))


def _read_receipt(path):
    from story_evidence_store import resolve_evidence
    from story_hash_cache import hash_cache_scope
    with hash_cache_scope():
        value = resolve_evidence(json.loads(Path(path).read_text()))
    if isinstance(value.get('inputs'), dict) and value['inputs'].get('schema_version') == 'story-encode-file-list/v1':
        value['inputs'] = value['inputs']['members']
    files = value.get('dependencies', {}).get('files')
    if isinstance(files, dict) and files.get('schema_version') == 'story-encode-file-list/v1':
        value['dependencies']['files'] = files['members']
    return value


def state_directory():
    path = Path(os.environ.get('STORY_ENCODE_STATE_DIR', str(Path(tempfile.gettempdir()) / f'story-encodes-{os.getuid()}')))
    path.mkdir(parents=True, exist_ok=True)
    return path


def limit_default():
    config = json.loads(Path(__file__).with_name('pipeline_config.json').read_text())
    return int(os.environ.get('STORY_ENCODE_CONCURRENCY', config.get('resource_limits', {}).get('formal_encode_concurrency', 1)))


@contextmanager
def encode_slot(*, limit=None, wait_seconds=None, cancelled=lambda: False, waiting=lambda: None):
    """Capacity cannot change while any encoder or waiter uses the pool."""
    root = state_directory()
    count = limit if limit is not None else limit_default()
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError('Encode concurrency must be a positive integer')
    deadline = None if wait_seconds is None else time.monotonic() + wait_seconds
    with (root / 'capacity.lock').open('a+') as configuration:
        # Hold a shared capacity lock throughout encoding; initialize/change only while idle.
        cap = root / 'capacity.json'
        while True:
            if cancelled():
                raise InterruptedError("Encode cancelled while queued")
            waiting()
            try:
                fcntl.flock(configuration, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if cap.exists() and json.loads(cap.read_text())['limit'] == count:
                    break
                fcntl.flock(configuration, fcntl.LOCK_UN)
                fcntl.flock(configuration, fcntl.LOCK_EX | fcntl.LOCK_NB)
                write(cap, {'limit': count})
                fcntl.flock(configuration, fcntl.LOCK_SH)
                break
            except BlockingIOError:
                if cancelled():
                    raise InterruptedError('Encode cancelled while waiting for pool configuration')
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('Change encode capacity only when the shared pool is idle')
                time.sleep(.1)
        slot = None
        try:
            while slot is None:
                if cancelled():
                    raise InterruptedError("Encode cancelled while queued")
                waiting()
                for index in range(count):
                    candidate = (root / f'slot-{index}.lock').open('a+')
                    try:
                        fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        slot = candidate
                        break
                    except BlockingIOError:
                        candidate.close()
                if slot is None:
                    if cancelled():
                        raise InterruptedError('Encode cancelled while waiting')
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError('Encode slots busy; bounded wait ended')
                    time.sleep(.1)
            yield (slot.fileno(), configuration.fileno())
        finally:
            if slot is not None:
                slot.close()


def run_encode(args, *, timeout=21600, wait_seconds=None, limit=None, code_version=None):
    from story_render_task import current_render_task
    task = current_render_task()
    if task:
        from story_work_observation import operation_observation
        from story_operation_recovery import replay_pending_facts
        replay_pending_facts(task['ledger'])
        reconcile_encode_operations(task['ledger'])
        with operation_observation(task['ledger'], 'ffmpeg', 'encode', artifacts=[args[-1]]) as fact:
            return _run_encode(
                args, timeout=timeout, wait_seconds=wait_seconds, limit=limit,
                fact=fact, ledger=task['ledger'], code_version=code_version,
            )
    return _run_encode(
        args, timeout=timeout, wait_seconds=wait_seconds, limit=limit,
        code_version=code_version,
    )


def _run_encode(args, **kwargs):
    """Close preparation/queue faults as well as failures inside FFmpeg."""
    try:
        return _run_encode_impl(args, **kwargs)
    except BaseException as exc:
        output = Path(args[-1]).expanduser().resolve()
        root = state_directory()
        receipt = root / (hashlib.sha256(str(output).encode()).hexdigest() + '.json')
        try:
            with receipt.with_suffix('.lock').open('a+') as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = _read_receipt(receipt)
                # Another owner or a failure before we wrote our state is not ours.
                fact = kwargs.get('fact')
                if state.get('parent_pid') == os.getpid() and (not fact or state.get('operation_id') == fact['operation_id']):
                    deferred = isinstance(exc, TimeoutError) and state.get('status') == 'waiting'
                    if state.get('status') in {'waiting', 'running'} and not deferred:
                        cancelled = isinstance(exc, (InterruptedError, KeyboardInterrupt)) or receipt.with_suffix('.cancel').exists()
                        state.update(status='cancelled' if cancelled else 'failed', ended_at=time.time(), error_type=type(exc).__name__)
                        _write_receipt(receipt, state)
                        if fact is not None and cancelled:
                            fact['status'] = 'cancelled'
        except (OSError, ValueError):
            pass  # Preserve original fault; the durable receipt is reconciled on resume.
        raise


def _run_encode_impl(args, *, timeout=21600, wait_seconds=None, limit=None, fact=None, ledger=None, code_version=None):
    output = Path(args[-1]).expanduser().resolve()
    storage_anchor = output.parent
    while not storage_anchor.exists():
        storage_anchor = storage_anchor.parent
    anchor_stat = storage_anchor.stat()
    anchor_identity = (anchor_stat.st_dev, anchor_stat.st_ino)
    def check_storage():
        current = storage_anchor.stat()
        if (current.st_dev, current.st_ino) != anchor_identity:
            raise OSError('Output volume/directory identity changed during managed encode')
    from story_encode_dependencies import snapshot
    dependencies = snapshot(args)
    executable = shutil.which(str(args[0]))
    if not executable:
        raise FileNotFoundError(f'Encode executable missing: {args[0]}')
    executable = str(Path(executable).resolve())
    tool = binding(executable)
    inputs = dependencies['files']
    if any(output == Path(item['path']) or (output.exists() and os.path.samefile(output, item['path'])) for item in inputs):
        raise ValueError('Encode output must never replace an input')
    if Path(args[-1]).is_symlink():
        raise ValueError('Encode output cannot be a symlink')
    fingerprint = hashlib.sha256(json.dumps({
        'command': args,
        'dependencies': dependencies,
        'render_code_version': code_version,
        'encoder_build_sha256': tool['sha256'],
    }, sort_keys=True).encode()).hexdigest()
    root = state_directory()
    identity = hashlib.sha256(str(output).encode()).hexdigest()
    from story_render_task import encode_task
    task = encode_task(output) or os.environ.get("STORY_TASK_ID", identity)
    receipt = root / (identity + '.json')
    cancel = root / (identity + '.cancel')
    with (root / (identity + '.lock')).open('a+') as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f'Encode already active for {output}; do not restart')
        old = _read_receipt(receipt) if receipt.exists() else {}
        if old and old.get('task') != task:
            raise ValueError('Encode output is owned by another task')
        pending = old.get('pending_output')
        if old.get('status') != 'completed' and old.get('phase') == 'publishing' and pending and output.is_file() and sha(output) == pending.get('sha256'):
            old.update(status='completed', output=pending, recovered_at=time.time())
            _write_receipt(receipt, old)
        residue = Path(old['temporary_path']) if old.get('temporary_path') else None
        if residue is not None and residue.parent == output.parent and residue.name.startswith('.encoding-') and residue != output:
            residue.unlink(missing_ok=True)  # free owner lock proves no surviving child owns it

        if dependencies['reusable'] and old.get('status') == 'completed' and old.get('fingerprint') == fingerprint and output.is_file() and sha(output) == old.get('output', {}).get('sha256'):
            if fact is not None:
                fact.update(execution_mode='resume_reuse', attempt_classification='cache_reuse', wait_seconds=0.0)
            return
        if fact is not None:
            fact['attempt_classification'] = 'first_generation'
        if old and fact is not None:
            retry = old.get('status') in {'failed', 'cancelled', 'waiting', 'running', 'deferred'}
            fact.update(execution_mode='rework', rework_reason='previous failed attempt' if retry else 'changed verified dependencies/parameters',
                        rework_classification='valid', attempt_classification='failed_retry' if retry else 'valid_rework')
        if output.exists() and (not old.get('output') or sha(output) != old['output']['sha256']):
            raise ValueError('Existing output is not the current managed artifact; preserve it')
        if cancel.exists():
            raise RuntimeError(f'Cancellation remains active: {cancel}; acknowledge before resume')
        queued_at = time.time()
        queued = {'schema_version': 'story-encode/v1', 'fingerprint': fingerprint,
                  'inputs': inputs, 'dependencies': dependencies, 'role': str(output),
                  'task': task, 'parent_pid': os.getpid(), 'status': 'waiting',
                  'queued_at': queued_at, 'heartbeat': queued_at}
        queued['render_code_version'] = code_version
        queued['encoder_tool'] = tool
        if ledger is not None and fact is not None:
            queued.update(ledger=str(Path(ledger).resolve()), operation_id=fact['operation_id'])
        if old.get('output'):
            queued['output'] = old['output']
        _write_receipt(receipt, queued)
        def heartbeat_wait():
            if time.time() - queued['heartbeat'] >= 1:
                queued['heartbeat'] = time.time()
                _write_receipt(receipt, queued)
        @contextmanager
        def acquire():
            acquired = False
            try:
                with encode_slot(limit=limit, wait_seconds=wait_seconds, cancelled=cancel.exists,
                                 waiting=heartbeat_wait) as slots:
                    acquired = True
                    yield slots
            except (InterruptedError, TimeoutError):
                if acquired:
                    raise
                queued.update(status='cancelled' if cancel.exists() else 'waiting', heartbeat=time.time(), ended_at=time.time())
                if fact is not None:
                    fact.update(status='cancelled' if cancel.exists() else 'deferred', wait_seconds=time.time() - queued_at)
                _write_receipt(receipt, queued)
                raise
        with acquire() as slots:
            check_storage()
            parent = output.parent
            while not parent.exists():
                parent = parent.parent
            if shutil.disk_usage(parent).free < 64 * 1024 * 1024:
                raise RuntimeError('Insufficient disk space')
            version = subprocess.run([executable, '-version'], capture_output=True, text=True, check=True)
            tool['version_output'] = version.stdout.strip()
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.encoding-', suffix=output.suffix, dir=output.parent)
            os.close(fd)
            state = {'schema_version': 'story-encode/v1', 'fingerprint': fingerprint, 'inputs': inputs, 'dependencies': dependencies, 'role': str(output), 'task': task, 'parent_pid': os.getpid(), 'status': 'running', 'started_at': time.time(), 'heartbeat': time.time()}
            state['render_code_version'] = code_version
            state['encoder_tool'] = tool
            if ledger is not None and fact is not None:
                state.update(ledger=str(Path(ledger).resolve()), operation_id=fact['operation_id'])
            state.update(queued_at=queued_at, wait_seconds=time.time() - queued_at)
            if fact is not None:
                fact['wait_seconds'] = state['wait_seconds']
            if old.get('output'):
                state['output'] = old['output']
            started = time.monotonic()
            with tempfile.TemporaryFile(mode='w+') as log:
                process = None
                try:
                    # Inherited kernel locks outlive an interrupted parent until its own child exits.
                    process = subprocess.Popen([executable, *args[1:-1], temporary], stdout=log, stderr=log, pass_fds=(owner.fileno(), *slots))
                    state['pid'] = process.pid
                    state['temporary_path'] = temporary
                    _write_receipt(receipt, state)
                    while process.poll() is None:
                        check_storage()
                        if cancel.exists():
                            raise InterruptedError('Cancellation requested')
                        if time.monotonic() - started > timeout:
                            state['wait_deadline_exceeded'] = True  # owned process remains active until completion/cancel
                        state['heartbeat'] = time.time()
                        _write_receipt(receipt, state)
                        time.sleep(.2)
                    if process.returncode:
                        log.seek(0)
                        raise RuntimeError(log.read()[-8000:])
                    process = subprocess.Popen([executable, '-v', 'error', '-i', temporary, '-f', 'null', '-'], stdout=log, stderr=log, pass_fds=(owner.fileno(), *slots))
                    state.update(pid=process.pid, phase='validating')
                    _write_receipt(receipt, state)
                    while process.poll() is None:
                        check_storage()
                        if cancel.exists():
                            raise InterruptedError('Cancellation requested during decode validation')
                        if time.monotonic() - started > timeout:
                            state['wait_deadline_exceeded'] = True
                        state['heartbeat'] = time.time()
                        _write_receipt(receipt, state)
                        time.sleep(.1)
                    if process.returncode:
                        raise RuntimeError('Encoded output cannot decode')
                    if snapshot(args) != dependencies:
                        raise RuntimeError('Input dependencies changed during encode')
                    if sha(executable) != tool['sha256']:
                        raise RuntimeError('Encoder build changed during encode')
                    if cancel.exists():
                        raise InterruptedError('Cancellation requested before publication')
                    check_storage()
                    verified = binding(temporary)
                    state['pending_output'] = {**verified, 'path': str(output)}
                    state['phase'] = 'publishing'
                    _write_receipt(receipt, state)
                    os.replace(temporary, output)
                    state.update(status='completed', output=binding(output), ended_at=time.time())
                    _write_receipt(receipt, state)
                except BaseException as exc:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    cancelled = cancel.exists() or isinstance(exc, (InterruptedError, KeyboardInterrupt))
                    state['status'] = 'cancelled' if cancelled else 'failed'
                    if fact is not None and cancelled:
                        fact['status'] = 'cancelled'
                    state['ended_at'] = time.time()
                    state['error_type'] = 'cancelled' if cancelled else type(exc).__name__
                    try:
                        _write_receipt(receipt, state)
                    except OSError:
                        pass  # receipt/volume failure must not mask the original exception
                    raise
                finally:
                    try:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                    except OSError:
                        pass  # tracked residue remains non-deliverable and is cleaned on recovery


def reconcile_encode_operations(run_file):
    """Reconcile receipts only after the kernel owner lock is free.

    No PID signalling: a surviving child retains that same inherited lock. A
    completed decode/publication can close an orphan observation as complete,
    only after re-hashing its output. Missing volumes leave recovery pending.
    """
    ledger = str(Path(run_file).expanduser().resolve())
    recovered = []
    for receipt in state_directory().glob('*.json'):
        if receipt.name == 'capacity.json':
            continue
        try:
            state = _read_receipt(receipt)
        except (OSError, json.JSONDecodeError):
            continue
        if state.get('ledger') != ledger or not state.get('operation_id'):
            continue
        try:
            with receipt.with_suffix('.lock').open('a+') as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Re-read after acquiring ownership: the writer may have just completed.
                state = _read_receipt(receipt)
                output = state.get('output') if state.get('status') == 'completed' else state.get('pending_output')
                verified = False
                if output:
                    target = Path(output['path'])
                    if not target.parent.exists():
                        continue  # unavailable output volume is unknown, not a failed hash
                    verified = target.is_file() and sha(target) == output.get('sha256')
                if verified:
                    status, error = 'complete', None
                elif state.get('status') == 'cancelled' or receipt.with_suffix('.cancel').exists():
                    status, error = 'cancelled', state.get('error_type') or 'cancelled'
                elif state.get('status') == 'waiting' and state.get('ended_at'):
                    status, error = 'deferred', 'queue_wait_deadline'
                else:
                    status, error = 'failed', state.get('error_type') or 'managed_process_interrupted'
                from story_work_observation import recover_operation
                closed = recover_operation(
                    ledger, state['operation_id'], error_type=error,
                    status=status,
                    artifacts=[output] if verified else [],
                    recovery_evidence={'encode_receipt': binding(receipt), 'owner_lock_was_free': True,
                                       'output_hash_verified': verified, 'previous_parent_pid': state.get('parent_pid')},
                )
                if state.get('status') in {'waiting', 'running'} or (verified and state.get('status') != 'completed') or (not verified and state.get('status') == 'completed'):
                    state.update(status='completed' if verified else status, error_type=error,
                                 ended_at=time.time(), recovered_at=time.time())
                    if verified:
                        state['output'] = output
                    _write_receipt(receipt, state)
                temporary = state.get('temporary_path')
                if temporary:
                    residue = Path(temporary)
                    role = Path(state.get('role', ''))
                    if residue.parent == role.parent and residue.name.startswith('.encoding-') and residue != role:
                        residue.unlink(missing_ok=True)
                if closed:
                    recovered.append(state['operation_id'])
        except (OSError, ValueError):
            continue  # locked owner or unmounted project: next explicit resume retries
    return recovered


def control_encode(output, *, action, expected_fingerprint, expected_task):
    """Cancel via an owned request flag; never signal a PID supplied by a ledger."""
    output = Path(output).resolve()
    root = state_directory()
    identity = hashlib.sha256(str(output).encode()).hexdigest()
    path = root / (identity + '.json')
    state = _read_receipt(path)
    if state.get('fingerprint') != expected_fingerprint or state.get('task') != expected_task or state.get('role') != str(output):
        raise ValueError('Encode identity/task/input fingerprint does not match')
    cancel = path.with_suffix('.cancel')
    if action == 'cancel':
        if state['status'] in {'running', 'waiting'}:
            write(cancel, {'fingerprint': expected_fingerprint, 'task': expected_task})
    elif action == 'resume':
        with path.with_suffix('.lock').open('a+') as owner:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Cannot resume while encoder is active')
            if cancel.exists():
                cancel.unlink()
    elif action != 'status':
        raise ValueError('Unknown encode action')
    return state
