"""Minimal automatic local-operation facts in the existing native run ledger."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid
from story_production_v2 import binding

_ACTIVE_OPERATION = ContextVar("story_active_operation", default=None)


def append_fact(run_file, fact):
    from story_run import run_file_lock, load_run, atomic_write_json, ensure_observability
    with run_file_lock(Path(run_file)):
        run = load_run(Path(run_file))
        events = ensure_observability(run)['events']
        from story_evidence_store import externalize_evidence
        fact = externalize_evidence(fact, Path(run_file).parent / 'evidence_members')
        events.append({'sequence': len(events) + 1, 'observed_at': datetime.now(timezone.utc).isoformat(),
                       'event': 'operation_fact', **fact})
        atomic_write_json(Path(run_file), run)
        return run.get("run_id")


def recover_operation(run_file, operation_id, *, error_type, recovery_evidence, status="failed", artifacts=()):
    """Close one durable orphan fact without inventing model or Token data."""
    from story_run import load_run
    run = load_run(Path(run_file))
    latest = None
    for event in run.get('observability', {}).get('events', []):
        if event.get('event') == 'operation_fact' and event.get('operation_id') == operation_id:
            latest = event
    if not latest or latest.get('status') != 'running':
        return False
    ended_at = datetime.now(timezone.utc).isoformat()
    recovered = {key: value for key, value in latest.items() if key not in {'sequence', 'observed_at', 'event'}}
    recovered.update(
        status=status,
        error_type=error_type,
        artifacts=list(artifacts),
        ended_at=ended_at,
        recovery_evidence=recovery_evidence,
    )
    try:
        started = datetime.fromisoformat(str(recovered.get('started_at') or ''))
        recovered['recovery_wallspan_seconds'] = max(0.0, (datetime.fromisoformat(ended_at) - started).total_seconds())
    except (TypeError, ValueError):
        recovered['recovery_wallspan_seconds'] = None
    recovered.setdefault('duration_seconds', None)
    append_fact(run_file, recovered)
    return True


@contextmanager
def operation_observation(run_file, operation, kind, *, artifacts=(), execution_mode='first_execution', rework_reason=None, rework_classification=None):
    if kind not in {'generate', 'edit', 'encode', 'review', 'deterministic'}:
        raise ValueError('Unknown operation kind')
    if execution_mode not in {'first_execution', 'resume_reuse', 'rework'}:
        raise ValueError('Unknown execution mode')
    if rework_classification not in {None, 'valid', 'invalid', 'unknown'}:
        raise ValueError('Unknown rework classification')
    now = datetime.now(timezone.utc).isoformat()
    fact = {'parent_operation_id': _ACTIVE_OPERATION.get(), 'operation_id': uuid.uuid4().hex, 'operation': operation, 'kind': kind,
            'execution_mode': execution_mode, 'rework_reason': rework_reason,
            'rework_classification': rework_classification, 'started_at': now, 'ended_at': None,
            'duration_seconds': None, 'wait_seconds': None, 'model': None, 'reasoning_effort': None,
            'input_tokens': None, 'output_tokens': None, 'total_tokens': None,
            'provider': 'local', 'request_id': None, 'status': 'running', 'error_type': None,
            'artifacts': [], 'configuration_evidence': {'expected': 'inherit current task settings', 'actual': None}}
    start = time.monotonic()
    from story_operation_recovery import operation_owner
    with operation_owner(run_file, fact) as run_id:
        append_fact(run_file, fact)
        def finish_fact():
            from story_operation_recovery import spool_end_fact
            # Save the terminal local fact before touching the project disk. A kill
            # during that write can safely replay the same measured end later.
            spool_end_fact(run_file, run_id, fact)
            append_fact(run_file, fact)
        try:
            token = _ACTIVE_OPERATION.set(fact['operation_id'])
            try:
                yield fact
            finally:
                _ACTIVE_OPERATION.reset(token)
        except BaseException as exc:
            if fact['status'] not in {'deferred', 'cancelled'}:
                fact['status'] = 'cancelled' if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else 'failed'
            fact['error_type'] = type(exc).__name__
            raise
        else:
            fact['status'] = 'complete'
        finally:
            fact['ended_at'] = datetime.now(timezone.utc).isoformat()
            fact['duration_seconds'] = time.monotonic() - start
            import sys
            already_failing = sys.exc_info()[0] is not None
            try:
                # An old managed output surviving a failed rework is not a new result.
                fact['artifacts'] = [binding(p) for p in artifacts if Path(p).exists()] if fact['status'] == 'complete' else []
            except (OSError, ValueError) as exc:
                fact.update(status='failed', error_type=type(exc).__name__, artifacts=[])
                if not already_failing:
                    try:
                        finish_fact()
                    except OSError:
                        pass
                    raise
            try:
                finish_fact()
            except OSError:
                if not already_failing:
                    raise
                # Preserve cancellation/encode failure; durable owner receipts are
                # reconciled when the project volume becomes available again.



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
            'timing': summarize_timing(list(latest.values())),
            'coverage': 'Instrumented local operations and explicitly registered provider requests only; not total task/model usage',
            'total_model_requests': None, 'actual_task_model': None, 'actual_reasoning_effort': None,
            'unknown_token_request_count': sum(x.get('total_tokens') is None for x in requests),
            'no_records_means': 'no observed records, not verified zero calls'}


def summarize_timing(operations):
    """Report observed parent/leaf service times separately from wall-clock span.

    Historical rows without a parent field are deliberately left unclassified;
    temporal containment alone does not establish that one operation spawned another.
    """
    parents = {x.get('parent_operation_id') for x in operations if x.get('parent_operation_id')}
    parent_rows = [x for x in operations if x.get('operation_id') in parents]
    child_rows = [x for x in operations if x.get('parent_operation_id')]
    leaf_rows = [x for x in operations if 'parent_operation_id' in x and x.get('operation_id') not in parents]
    def total(rows):
        values = [x.get('duration_seconds') for x in rows]
        return sum(values) if values and all(isinstance(v, (float, int)) and not isinstance(v, bool) for v in values) else None
    spans = []
    for row in operations:
        try:
            start = datetime.fromisoformat(row['started_at'])
            end = datetime.fromisoformat(row['ended_at'])
            if start.tzinfo is None or end.tzinfo is None or end < start:
                continue
            spans.append((start.timestamp(), end.timestamp()))
        except (KeyError, TypeError, ValueError):
            continue
    return {'parent_inclusive_seconds': total(parent_rows), 'child_inclusive_seconds': total(child_rows),
            'leaf_operation_seconds': total(leaf_rows),
            'observed_wall_clock_span_seconds': max(x[1] for x in spans) - min(x[0] for x in spans) if spans else None,
            'unclassified_historical_operation_count': sum('parent_operation_id' not in x for x in operations),
            'duration_unknown_count': sum(x.get('duration_seconds') is None for x in operations),
            'warning': 'Do not add parent and child inclusive durations; wall-clock span includes idle gaps. Leaf sum is observed service time, not CPU time.'}
