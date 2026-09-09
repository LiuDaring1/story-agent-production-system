"""Local FFmpeg ownership, bounded waits and a shared configurable encode pool."""
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
def encode_slot(*, limit=None, wait_seconds=60, cancelled=lambda: False):
    """Capacity cannot change while any encoder or waiter uses the pool."""
    root = state_directory()
    count = limit if limit is not None else limit_default()
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError('Encode concurrency must be a positive integer')
    deadline = time.monotonic() + wait_seconds
    with (root / 'capacity.lock').open('a+') as configuration:
        # Hold a shared capacity lock throughout encoding; initialize/change only while idle.
        cap = root / 'capacity.json'
        while True:
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
                if time.monotonic() >= deadline:
                    raise TimeoutError('Change encode capacity only when the shared pool is idle')
                time.sleep(.1)
        slot = None
        try:
            while slot is None:
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
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Encode slots busy; bounded wait ended')
                    time.sleep(.1)
            yield (slot.fileno(), configuration.fileno())
        finally:
            if slot is not None:
                slot.close()


def run_encode(args, *, timeout=21600, wait_seconds=60, limit=None):
    output = Path(args[-1]).expanduser().resolve()
    inputs = [binding(args[i + 1]) for i, value in enumerate(args[:-1]) if value == '-i' and Path(args[i + 1]).is_file()]
    if any(output == Path(item['path']) or (output.exists() and os.path.samefile(output, item['path'])) for item in inputs):
        raise ValueError('Encode output must never replace an input')
    if Path(args[-1]).is_symlink():
        raise ValueError('Encode output cannot be a symlink')
    fingerprint = hashlib.sha256(json.dumps({'command': args, 'inputs': inputs}, sort_keys=True).encode()).hexdigest()
    root = state_directory()
    identity = hashlib.sha256(str(output).encode()).hexdigest()
    receipt = root / (identity + '.json')
    cancel = root / (identity + '.cancel')
    with (root / (identity + '.lock')).open('a+') as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f'Encode already active for {output}; do not restart')
        old = json.loads(receipt.read_text()) if receipt.exists() else {}
        if old.get('status') == 'completed' and old.get('fingerprint') == fingerprint and output.is_file() and sha(output) == old.get('output', {}).get('sha256'):
            return
        if output.exists() and (not old.get('output') or sha(output) != old['output']['sha256']):
            raise ValueError('Existing output is not the current managed artifact; preserve it')
        if cancel.exists():
            raise RuntimeError(f'Cancellation remains active: {cancel}; acknowledge before resume')
        with encode_slot(limit=limit, wait_seconds=wait_seconds, cancelled=cancel.exists) as slots:
            parent = output.parent
            while not parent.exists():
                parent = parent.parent
            if shutil.disk_usage(parent).free < 64 * 1024 * 1024:
                raise RuntimeError('Insufficient disk space')
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.encoding-', suffix=output.suffix, dir=output.parent)
            os.close(fd)
            state = {'schema_version': 'story-encode/v1', 'fingerprint': fingerprint, 'inputs': inputs, 'role': str(output), 'task': os.environ.get('STORY_TASK_ID', identity), 'parent_pid': os.getpid(), 'status': 'running', 'started_at': time.time(), 'heartbeat': time.time()}
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
                    check = subprocess.run(['ffmpeg', '-v', 'error', '-i', temporary, '-f', 'null', '-'], capture_output=True, timeout=timeout)
                    if check.returncode:
                        raise RuntimeError('Encoded output cannot decode')
                    for item in inputs:
                        if sha(item['path']) != item['sha256']:
                            raise RuntimeError('Input changed during encode')
                    os.replace(temporary, output)
                    state.update(status='completed', output=binding(output))
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
                    write(receipt, state)
                    raise
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)


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
        if state['status'] == 'running':
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
