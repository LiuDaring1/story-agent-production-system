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


def _run_encode(args, *, timeout=21600, wait_seconds=None, limit=None, fact=None, ledger=None, code_version=None):
    output = Path(args[-1]).expanduser().resolve()
    from story_encode_dependencies import snapshot
    dependencies = snapshot(args)
    inputs = dependencies['files']
    if any(output == Path(item['path']) or (output.exists() and os.path.samefile(output, item['path'])) for item in inputs):
        raise ValueError('Encode output must never replace an input')
    if Path(args[-1]).is_symlink():
        raise ValueError('Encode output cannot be a symlink')
    fingerprint = hashlib.sha256(json.dumps({
        'command': args,
        'dependencies': dependencies,
        'render_code_version': code_version,
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
        old = json.loads(receipt.read_text()) if receipt.exists() else {}
        if old and old.get('task') != task:
            raise ValueError('Encode output is owned by another task')
        if dependencies['reusable'] and old.get('status') == 'completed' and old.get('fingerprint') == fingerprint and output.is_file() and sha(output) == old.get('output', {}).get('sha256'):
            if fact is not None:
                fact.update(execution_mode='resume_reuse', wait_seconds=0.0)
            return
        if old and fact is not None:
            fact.update(execution_mode='rework', rework_reason='previous output/dependencies not reusable', rework_classification='unknown')
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
        if ledger is not None and fact is not None:
            queued.update(ledger=str(Path(ledger).resolve()), operation_id=fact['operation_id'])
        if old.get('output'):
            queued['output'] = old['output']
        write(receipt, queued)
        def heartbeat_wait():
            if time.time() - queued['heartbeat'] >= 1:
                queued['heartbeat'] = time.time()
                write(receipt, queued)
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
                queued.update(status='cancelled' if cancel.exists() else 'waiting', heartbeat=time.time())
                if fact is not None:
                    fact.update(status='cancelled' if cancel.exists() else 'deferred', wait_seconds=time.time() - queued_at)
                write(receipt, queued)
                raise
        with acquire() as slots:
            parent = output.parent
            while not parent.exists():
                parent = parent.parent
            if shutil.disk_usage(parent).free < 64 * 1024 * 1024:
                raise RuntimeError('Insufficient disk space')
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.encoding-', suffix=output.suffix, dir=output.parent)
            os.close(fd)
            state = {'schema_version': 'story-encode/v1', 'fingerprint': fingerprint, 'inputs': inputs, 'dependencies': dependencies, 'role': str(output), 'task': task, 'parent_pid': os.getpid(), 'status': 'running', 'started_at': time.time(), 'heartbeat': time.time()}
            state['render_code_version'] = code_version
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
                    process = subprocess.Popen([*args[:-1], temporary], stdout=log, stderr=log, pass_fds=(owner.fileno(), *slots))
                    state['pid'] = process.pid
                    write(receipt, state)
                    while process.poll() is None:
                        if cancel.exists():
                            raise InterruptedError('Cancellation requested')
                        if time.monotonic() - started > timeout:
                            raise TimeoutError('Encode timed out')
                        state['heartbeat'] = time.time()
                        write(receipt, state)
                        time.sleep(.2)
                    if process.returncode:
                        log.seek(0)
                        raise RuntimeError(log.read()[-8000:])
                    process = subprocess.Popen(['ffmpeg', '-v', 'error', '-i', temporary, '-f', 'null', '-'], stdout=log, stderr=log, pass_fds=(owner.fileno(), *slots))
                    state.update(pid=process.pid, phase='validating')
                    write(receipt, state)
                    while process.poll() is None:
                        if cancel.exists():
                            raise InterruptedError('Cancellation requested during decode validation')
                        if time.monotonic() - started > timeout:
                            raise TimeoutError('Encode/decode validation timed out')
                        state['heartbeat'] = time.time()
                        write(receipt, state)
                        time.sleep(.1)
                    if process.returncode:
                        raise RuntimeError('Encoded output cannot decode')
                    if snapshot(args) != dependencies:
                        raise RuntimeError('Input dependencies changed during encode')
                    if cancel.exists():
                        raise InterruptedError('Cancellation requested before publication')
                    os.replace(temporary, output)
                    state.update(status='completed', output=binding(output), ended_at=time.time())
                    write(receipt, state)
                except BaseException:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    state['status'] = 'cancelled' if cancel.exists() else 'failed'
                    state['ended_at'] = time.time()
                    state['error_type'] = 'cancelled' if cancel.exists() else 'encode_exception'
                    write(receipt, state)
                    raise
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)


def reconcile_encode_operations(run_file):
    """Close encode facts whose durable owner lock no longer has a live holder."""
    ledger = str(Path(run_file).expanduser().resolve())
    recovered = []
    for receipt in state_directory().glob('*.json'):
        if receipt.name == 'capacity.json':
            continue
        try:
            state = json.loads(receipt.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if state.get('ledger') != ledger or state.get('status') != 'running' or not state.get('operation_id'):
            continue
        lock_path = receipt.with_suffix('.lock')
        with lock_path.open('a+') as owner:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            original = dict(state)
            state.update(
                status='failed',
                error_type='managed_process_interrupted',
                ended_at=time.time(),
                recovered_at=time.time(),
            )
            write(receipt, state)
        from story_work_observation import recover_operation
        try:
            closed = recover_operation(
                ledger,
                state['operation_id'],
                error_type='managed_process_interrupted',
                recovery_evidence={
                    'encode_receipt': binding(receipt),
                    'owner_lock_was_free': True,
                    'previous_parent_pid': state.get('parent_pid'),
                },
            )
        except OSError:
            # The project volume may still be unavailable.  Preserve the
            # recoverable running state so the next invocation can close both
            # records together after remount.
            write(receipt, original)
            continue
        if closed:
            recovered.append(state['operation_id'])
    return recovered


def control_encode(output, *, action, expected_fingerprint, expected_task):
    """Cancel via an owned request flag; never signal a PID supplied by a ledger."""
    output = Path(output).resolve()
    root = state_directory()
    identity = hashlib.sha256(str(output).encode()).hexdigest()
    path = root / (identity + '.json')
    state = json.loads(path.read_text())
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
