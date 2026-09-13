"""Bind formal v2 renderers to a verified ledger, never a hand-set environment ID."""
from contextvars import ContextVar
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from story_production_v2 import VERSION, protect_outputs

_task = ContextVar('verified_story_render_task', default=None)
_code_version = ContextVar('verified_story_render_code_version', default=None)


def bind_render_task(run_file=None, *, outputs=()):
    from story_run import load_run
    explicit = run_file is not None
    if run_file is None:
        discovered = set()
        for output in outputs:
            start = Path(output).expanduser().resolve()
            for parent in (start, *start.parents):
                candidate = parent / '99_项目状态' / 'story_run.json'
                if candidate.is_file():
                    discovered.add(candidate)
                    break
        if len(discovered) > 1:
            raise ValueError('Render outputs belong to different ledgers')
        run_file = next(iter(discovered), None)
    _task.set(None)
    if run_file is None:
        return None  # legacy unbound callers retain their output-based identity
    run_file = Path(run_file).expanduser().resolve()
    run = load_run(run_file)
    if run.get('production_contract') != VERSION:
        return None
    if not explicit:
        raise ValueError('v2 formal rendering requires explicit --run-file')
    task = str(run.get('run_id') or '').strip()
    if not task:
        raise ValueError('v2 render ledger lacks run_id')
    project = Path(run['project_dir']).expanduser().resolve()
    if project not in run_file.parents:
        raise ValueError('Render ledger is outside its project')
    protect_outputs(outputs, [run_file, *(v['path'] for v in run['inputs'].values())])
    for output in outputs:
        if project not in Path(output).expanduser().resolve().parents:
            raise ValueError('Render output belongs to another project')
    value = {'task': task, 'project': project, 'ledger': run_file,
             'protected': [run_file, *(v['path'] for v in run['inputs'].values())]}
    _task.set(value)
    return run


def encode_task(output):
    value = _task.get()
    if value is None:
        return None
    output = Path(output).resolve()
    if value['project'] not in output.parents:
        raise ValueError('Encode output belongs to another project')
    protect_outputs([output], value['protected'])
    return value['task']


def render_entry(function):
    """Keep task binding local when a CLI is invoked within a test/host process."""
    from functools import wraps
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _task.set(None)
        try:
            return function(*args, **kwargs)
        finally:
            _task.reset(token)
    return wrapped


def current_render_task():
    return _task.get()


@contextmanager
def render_code_scope(paths):
    """Bind only renderer-relevant source bytes into managed encode reuse."""
    from story_hash_cache import sha256_file
    bindings = [
        {'path': str(Path(path).resolve()), 'sha256': sha256_file(path)}
        for path in paths
    ]
    value = hashlib.sha256(
        json.dumps(bindings, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    token = _code_version.set(value)
    try:
        yield value
    finally:
        _code_version.reset(token)


def current_render_code_version():
    return _code_version.get()
