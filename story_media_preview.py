"""Shared v2 Demo preview geometry and independent approval boundary."""
from pathlib import Path
import json
from story_production_v2 import binding, current, write, validate_independent_approval, protect_outputs
from release_geometry import demo_presenter_geometry_issues
from story_evidence import review_passes

def validate_preview(path, project):
    p = json.loads(Path(path).read_text())
    if p.get('schema_version') != 'story-media-preview/v2' or Path(p['project']).resolve() != Path(project).resolve():
        raise ValueError('Preview belongs to another project')
    for item in p['inputs'].values():
        current(item)
    for item in p['run_inputs'].values():
        current(item)
    frames = p.get('artifacts', [])
    if len(frames) < 5:
        raise ValueError('Preview needs first/middle/last and gesture samples')
    for item in frames:
        current(item)
    geometry = p['presenter_geometry']
    issues = demo_presenter_geometry_issues(geometry)
    if issues:
        raise ValueError('; '.join(issues))
    if geometry['keying_preset_sha256'] != p['inputs']['preset']['sha256']:
        raise ValueError('Preview preset changed')
    if geometry['source_greenscreen_sha256'] != p['inputs']['foreground']['sha256']:
        raise ValueError('Preview foreground changed')
    return p

def approve_preview(preview, review, output, project):
    protect_outputs([output],[preview,review])
    p = validate_preview(preview, project)
    r = json.loads(Path(review).read_text())
    validate_independent_approval(r, Path(preview), producer_context=p.get('producer_context'))
    if not review_passes(r, artifact=Path(preview)):
        raise ValueError('Independent preview review missing/stale/below 85')
    payload = {'schema_version': 'story-approved-demo/v2', 'preview': binding(preview), 'review': binding(review), 'presenter_geometry': p['presenter_geometry']}
    write(output, payload)
    return payload

def load_approved(path, project):
    p = json.loads(Path(path).read_text())
    if p.get('schema_version') != 'story-approved-demo/v2':
        raise ValueError('Approved Demo receipt version invalid')
    preview = current(p['preview'])
    r = json.loads(current(p['review']).read_text())
    raw = validate_preview(preview, project)
    validate_independent_approval(r, preview, producer_context=raw.get('producer_context'))
    if not review_passes(r, artifact=preview):
        raise ValueError('Demo approval is stale')
    if p['presenter_geometry'] != raw['presenter_geometry']:
        raise ValueError('Approved geometry changed')
    return (p, p['presenter_geometry'])
