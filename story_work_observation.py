"""Minimal automatic local-operation facts in the existing native run ledger."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid
from story_production_v2 import binding


def append_fact(run_file, fact):
    from story_run import run_file_lock, load_run, atomic_write_json, ensure_observability
    with run_file_lock(Path(run_file)):
        run = load_run(Path(run_file))
        events = ensure_observability(run)['events']
        events.append({'sequence': len(events) + 1, 'observed_at': datetime.now(timezone.utc).isoformat(),
                       'event': 'operation_fact', **fact})
        atomic_write_json(Path(run_file), run)


@contextmanager
def operation_observation(run_file, operation, kind, *, artifacts=(), execution_mode='first_execution', rework_reason=None, rework_classification=None):
    if kind not in {'generate', 'edit', 'encode', 'review', 'deterministic'}:
        raise ValueError('Unknown operation kind')
    if execution_mode not in {'first_execution', 'resume_reuse', 'rework'}:
        raise ValueError('Unknown execution mode')
    if rework_classification not in {None, 'valid', 'invalid', 'unknown'}:
        raise ValueError('Unknown rework classification')
    now = datetime.now(timezone.utc).isoformat()
    fact = {'operation_id': uuid.uuid4().hex, 'operation': operation, 'kind': kind,
            'execution_mode': execution_mode, 'rework_reason': rework_reason,
            'rework_classification': rework_classification, 'started_at': now, 'ended_at': None,
            'duration_seconds': None, 'wait_seconds': None, 'model': None, 'reasoning_effort': None,
            'input_tokens': None, 'output_tokens': None, 'total_tokens': None,
            'provider': 'local', 'request_id': None, 'status': 'running', 'error_type': None,
            'artifacts': [], 'configuration_evidence': {'expected': 'inherit current task settings', 'actual': None}}
    start = time.monotonic()
    append_fact(run_file, fact)
    try:
        yield fact
    except BaseException as exc:
        if fact['status'] not in {'deferred', 'cancelled'}:
            fact['status'] = 'failed'
        fact['error_type'] = type(exc).__name__
        raise
    else:
        fact['status'] = 'complete'
    finally:
        fact['ended_at'] = datetime.now(timezone.utc).isoformat()
        fact['duration_seconds'] = time.monotonic() - start
        fact['artifacts'] = [binding(p) for p in artifacts if Path(p).exists()]
        append_fact(run_file, fact)


def summarize(run):
    """Never interpret an empty startup observation as complete production coverage."""
    obs = run.get('observability', {})
    latest = {}
    for event in obs.get('events', []):
        if event.get('event') == 'operation_fact': latest[event['operation_id']] = event
    requests = list(obs.get('requests', {}).values())
    counts = {}
    for event in latest.values():
        key = event['execution_mode']; counts[key] = counts.get(key, 0) + 1
    return {'schema_version': 'story-work-summary/v1', 'generated_from': 'current observability.events + requests',
            'observed_operations': len(latest), 'observed_requests': len(requests),
            'execution_modes': counts, 'operations': list(latest.values()),
            'coverage': 'Instrumented local operations and explicitly registered provider requests only; not total task/model usage',
            'total_model_requests': None, 'actual_task_model': None, 'actual_reasoning_effort': None,
            'unknown_token_request_count': sum(x.get('total_tokens') is None for x in requests),
            'no_records_means': 'no observed records, not verified zero calls'}
