"""One pending review packet shape; source adapters never claim a new review."""
from pathlib import Path
import json
from story_production_v2 import binding, review_provenance


def canonical_review_id(artifact_id):
    from story_artifact_validation import INDEPENDENT_REVIEW_TARGETS
    if artifact_id in INDEPENDENT_REVIEW_TARGETS:
        return artifact_id
    targets = {value: key for key, value in INDEPENDENT_REVIEW_TARGETS.items() if value}
    if artifact_id in targets:
        return targets[artifact_id]
    raise ValueError('Unknown independent review artifact ID: ' + str(artifact_id))


def create_review_request(*, artifact_id, artifact, producer_context, requirements, evidence=()):
    if not isinstance(producer_context, str) or not producer_context.strip():
        raise ValueError('Review request requires producer context')
    if not requirements:
        raise ValueError('Review request requires applicable rule sources')
    return {'schema_version': 'story-independent-review-request/v1',
            'artifact_id': canonical_review_id(artifact_id), 'artifact': binding(artifact),
            'producer_context': producer_context, 'requirements': [binding(p) for p in requirements],
            'evidence': [binding(p) for p in evidence], 'status': 'pending_independent_review',
            'reviewer_context': None, 'independent_context': None, 'score': None,
            'approved': None, 'critical_errors': None,
            'review_order': ['core_function_coverage', 'artifact_evidence', 'details']}


def source_review_provenance(path, *, allow_legacy_storyboard=False):
    """Read bytes unchanged and preserve their SHA even for nested v1 provenance."""
    source = binding(path)
    payload = json.loads(Path(path).read_text())
    return {**review_provenance(payload, allow_legacy_storyboard=allow_legacy_storyboard),
            'source_review': source, 'new_review_performed': False}
