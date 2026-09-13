"""Content fingerprints and conservative classification of observed requests."""
import hashlib
import json
from story_production_v2 import binding


def request_fingerprint(*, provider, model, prompt, input_paths=(), parameters=None):
    """Hash actual request inputs; locations and transport IDs are not content.

    This records a digest of parameters, not their raw values (which can contain
    private transport metadata). Call before submission, using adapter arguments.
    """
    inputs = [{k: item[k] for k in ('sha256', 'bytes')}
              for item in (binding(p) for p in input_paths)]
    parameters = dict(parameters or {})
    extra = dict(parameters.get('extra_body') or {})
    extra.pop('client_business_id', None)  # idempotency identity, not generated content
    parameters['extra_body'] = extra
    prompt_sha = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    payload = {'schema_version': 'story-request-fingerprint/v1', 'provider': provider,
               'model': model, 'prompt_sha256': prompt_sha, 'inputs': inputs,
               'parameters_sha256': hashlib.sha256(json.dumps(parameters, sort_keys=True,
                   ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()}
    payload['request_sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return payload


def classify_requests(requests):
    """A matching prompt or an unclassified repeat never proves wasted work."""
    rows = sorted(requests, key=lambda x: str(x.get('started_at') or x.get('observed_at') or ''))
    prior = {}
    counts = {}
    for row in rows:
        fingerprint = row.get('request_sha256') if row.get('request_hash_kind') == 'full_request' else None
        key = (row.get('provider'), fingerprint)
        previous = prior.get(key, []) if fingerprint else []
        explicit = row.get('request_relation')
        if explicit == 'true_duplicate':
            kind = 'true_duplicate' if previous else 'unknown'
        elif row.get('execution_mode') == 'rework' and row.get('rework_classification') == 'valid':
            kind = 'valid_rework'
        elif explicit == 'retry' or (row.get('retry_index') or 0) > 0:
            kind = 'failed_retry'
        elif explicit in {'parallel_required', 'cache_reuse'}:
            kind = explicit
        elif row.get('execution_mode') == 'first_execution' and not previous:
            kind = 'first_generation'
        else:
            kind = 'unknown'
        counts[kind] = counts.get(kind, 0) + 1
        if fingerprint:
            prior.setdefault(key, []).append(row)
    return {'classification': counts,
            'full_request_fingerprint_count': sum(x.get('request_hash_kind') == 'full_request' for x in rows),
            'unknown_fingerprint_scope_count': sum(x.get('request_hash_kind') != 'full_request' for x in rows),
            'coverage': 'Explicitly registered requests only; missing scope and classification remain unknown.'}


def observe_submission(run_file, *, row, scope, provider, model, fingerprint, invoke):
    """Persist transport attempts even when no provider task ID is returned.

    A local attempt ID is never presented as a provider request ID. Uncertain
    acceptance blocks an identical resubmission; a known returned task can be
    restored before the secondary jobs CSV write after a process interruption.
    """
    if run_file is None:
        return invoke()
    from datetime import datetime, timezone
    from pathlib import Path
    from types import SimpleNamespace
    import time
    import uuid
    from story_run import run_file_lock, load_run, atomic_write_json
    ledger = Path(run_file)
    scope_hash = hashlib.sha256(json.dumps({'provider': provider, 'scope': scope,
        'fingerprint': fingerprint['request_sha256']},sort_keys=True).encode()).hexdigest()
    with run_file_lock(ledger):
        run = load_run(ledger)
        attempts = run['observability'].setdefault('submission_attempts', {})
        prior = [x for x in attempts.values() if x.get('scope_sha256') == scope_hash]
        old = max(prior, key=lambda x: x['sequence']) if prior else None
        if old and old['status'] != 'rejected':
            if old['status'] == 'submitted' and old.get('request_id'):
                row['provider_started_at'] = old['started_at']
                return SimpleNamespace(task_id=old['request_id'], raw={
                    'recovered_from_submission_attempt': old['attempt_id'], 'request_id': old['request_id']})
            raise ValueError('Unresolved provider submission attempt; reconcile acceptance before resubmission: '+old['attempt_id'])
        started = datetime.now(timezone.utc).isoformat()
        fact = {'attempt_id': uuid.uuid4().hex, 'sequence': len(attempts) + 1, 'scope': scope, 'scope_sha256': scope_hash, 'provider': provider,
            'model': model, 'request_id': None, 'fingerprint': fingerprint,
            'request_hash_kind': 'full_request', 'started_at': started, 'ended_at': None,
            'duration_seconds': None, 'wait_seconds': None, 'retry_index': int(row.get('provider_attempt') or 0),
            'input_tokens': None, 'output_tokens': None, 'total_tokens': None,
            'status': 'submitting', 'provider_acceptance': 'unknown', 'error_type': None}
        attempts[fact['attempt_id']] = fact
        atomic_write_json(ledger,run)
    row['provider_started_at'] = started
    began = time.monotonic()
    try:
        created = invoke()
    except BaseException as exc:
        fact.update(status='failed', error_type=type(exc).__name__,
            ended_at=datetime.now(timezone.utc).isoformat(), duration_seconds=time.monotonic()-began)
        if getattr(exc, 'rejection_evidence', None):
            fact.update(status='rejected', provider_acceptance='rejected', rejection_evidence=exc.rejection_evidence)
        # The provider may have accepted a timed-out request; do not label it
        # rejected, or issue an automatic retry merely because no ID arrived.
        try:
            _store_submission_end(ledger,fact['attempt_id'],fact)
        except OSError:
            pass  # durable 'submitting' remains unknown and blocks resubmission
        raise
    fact.update(status='submitted', provider_acceptance='accepted', request_id=created.task_id,
        ended_at=datetime.now(timezone.utc).isoformat(), duration_seconds=time.monotonic()-began)
    _store_submission_end(ledger,fact['attempt_id'],fact)
    return created


def _store_submission_end(ledger, key, fact):
    from story_run import run_file_lock, load_run, atomic_write_json
    with run_file_lock(ledger):
        run=load_run(ledger)
        old=run['observability'].setdefault('submission_attempts',{}).get(key)
        if not old or old['attempt_id'] != fact['attempt_id']:
            raise ValueError('Submission attempt identity changed')
        run['observability']['submission_attempts'][key]=fact
        atomic_write_json(ledger,run)


def submission_summary(run):
    rows=list(run.get('observability',{}).get('submission_attempts',{}).values())
    statuses={}
    for row in rows:statuses[row['status']]=statuses.get(row['status'],0)+1
    return {'observed_submission_attempts':len(rows),'statuses':statuses,
        'unknown_acceptance_count':sum(x.get('provider_acceptance')=='unknown' for x in rows),
        'coverage':'Instrumented R2V/I2V create calls only. Submission attempts overlap provider request records and must not be added to request totals.'}
