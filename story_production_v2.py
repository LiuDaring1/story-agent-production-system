"""Versioned, foreground-only video production contract. No external generation."""
from __future__ import annotations
import hashlib
import json
import os
import tempfile
from pathlib import Path
VERSION = 'story-production/v2'
PACKAGES = ('director_plan', 'r2v_visuals', 'presenter_keying', 'media_render', 'product_assets', 'delivery')
INPUTS = ('confirmed_text', 'subtitle_txt', 'subtitle_srt', 'greenscreen_video', 'audio', 'final_word', 'finished_music', 'story_requirements', 'packaging_reference', 'packaging_prompt')
EXCLUDED = ('qa_music_report', 'static_ppt_delivery_receipt', 'qa_publish_report')
REQUIRED_ADDITIONS = ('ppt_materials_receipt', 'managed_package_receipt', 'packaging_prompt_receipt')
BASE_ROLES = ('customer_manuscript', 'music', 'demo', 'background_image')
ADVANCED_ROLES = (*BASE_ROLES, 'background_video_with_subtitles', 'background_video_without_subtitles', 'a_only_video', 'ppt_materials')
DELIVERY_ROLES = ('main_release_video', 'library_release_video', *(f'base:{r}' for r in BASE_ROLES), *(f'advanced:{r}' for r in ADVANCED_ROLES))

def sha(path):
    p = Path(path)
    if p.is_dir():
        return directory_binding(p)['sha256']
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def directory_binding(path):
    """Content-address a tree; symbolic links are never managed deliverables."""
    p = Path(path).resolve()
    members = []
    for child in sorted(p.rglob('*')):
        if child.is_symlink():
            raise ValueError(f'Symlink in managed directory: {child}')
        if child.is_file():
            members.append({'relative_path': child.relative_to(p).as_posix(),
                            'sha256': sha(child), 'bytes': child.stat().st_size})
    if not members:
        raise ValueError('Empty managed directory')
    digest = hashlib.sha256(json.dumps(members, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    return {'path': str(p), 'sha256': digest, 'bytes': sum(x['bytes'] for x in members),
            'kind': 'directory', 'members': members}


def binding(path):
    if path is None:
        raise ValueError('Missing explicit input path')
    p = Path(path).expanduser()
    if p.is_symlink():
        raise ValueError(f'Symlink bound input: {p}')
    p = p.resolve()
    if p.is_dir():
        return directory_binding(p)
    if not p.is_file():
        raise ValueError(f'Missing bound input: {p}')
    return {'path': str(p), 'sha256': sha(p), 'bytes': p.stat().st_size}


def current(item):
    p = Path(item['path'])
    if p.is_symlink():
        raise ValueError(f'Changed binding: {p}')
    if item.get('kind') == 'directory':
        if not p.is_dir() or any(item.get(k) != v for k, v in directory_binding(p).items()):
            raise ValueError(f'Changed directory binding: {p}')
    elif not p.is_file() or sha(p) != item['sha256']:
        raise ValueError(f'Changed binding: {p}')
    return p

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def is_v2(run):
    return run.get('production_contract') == VERSION

def bind_inputs(paths):
    if set(paths) != set(INPUTS):
        raise ValueError(f'Explicit inputs required: {sorted(set(INPUTS) - set(paths))}')
    result = {k: binding(v) for k, v in paths.items()}
    for role, ext in [('subtitle_txt', '.txt'), ('subtitle_srt', '.srt'), ('final_word', '.docx')]:
        if Path(result[role]['path']).suffix.lower() != ext:
            raise ValueError(f'{role} requires {ext}')
    return result

def validate_managed_receipt(path, inputs=None):
    p = json.loads(Path(path).read_text())
    if p.get('status') != 'complete':
        raise ValueError('Managed package copy incomplete')
    if p.get('schema_version') != 'story-managed-package/v2':
        raise ValueError('Invalid managed package version')
    items = p.get('artifacts', [])
    roles = [i['role'] for i in items]
    expected = set(DELIVERY_ROLES) - {'main_release_video', 'library_release_video'}
    if len(set(roles)) != len(roles) or set(roles) != expected:
        raise ValueError('Incomplete managed package roles')
    for item in items:
        current(item)
        source = current(item['source'])
        if item['sha256'] != sha(source):
            raise ValueError('Copy is not byte-identical')
        if inputs and item['role'].split(':')[1] in {'music', 'customer_manuscript'}:
            role = 'finished_music' if item['role'].endswith(':music') else 'final_word'
            if item['sha256'] != inputs[role]['sha256']:
                raise ValueError(f'Copy does not bind {role}')
    return p

def validate_checklist(path):
    p = json.loads(Path(path).read_text())
    if p.get('production_contract') != VERSION:
        raise ValueError('Wrong delivery contract')
    if p.get('missing') != [] or p.get('status') not in {'complete', 'complete_pending_independent_final_review'}:
        raise ValueError('Incomplete checklist')
    items = p.get('artifacts', [])
    roles = [x.get('role') for x in items]
    if set(roles) != set(DELIVERY_ROLES) or len(roles) != len(set(roles)):
        raise ValueError('Delivery roles incomplete/duplicated')
    for item in items:
        current(item)
    return p

def validate_special(artifact_id, path, inputs):
    if artifact_id == 'managed_package_receipt':
        return validate_managed_receipt(path, inputs)
    if artifact_id == 'ppt_materials_receipt':
        from story_materials import validate_materials
        return validate_materials(path, inputs)
    if artifact_id == 'packaging_prompt_receipt':
        from story_materials import validate_packaging
        return validate_packaging(path, inputs)
    return None

def validate_delivery_links(run):
    artifacts = run['artifacts']
    materials = json.loads(Path(artifacts['ppt_materials_receipt']['path']).read_text())
    package = validate_managed_receipt(artifacts['managed_package_receipt']['path'], run['inputs'])
    ppt = next((item for item in package['artifacts'] if item['role'] == 'advanced:ppt_materials'))
    if ppt['sha256'] != materials.get('directory', materials.get('archive', {}))['sha256']:
        raise ValueError('Delivered PPT materials are not the reviewed materials')
    if materials['compile_receipt']['sha256'] != artifacts['shot_storyboard_compile_receipt']['sha256']:
        raise ValueError('PPT materials use another storyboard compilation')
    release = json.loads(Path(artifacts['release_package_receipt']['path']).read_text())
    geometry = release.get('actual_geometry', {})
    panels = geometry.get('main_package_spec', {})
    if panels.get('main_package_spec_sha256') != artifacts['packaging_prompt_receipt']['sha256']:
        raise ValueError('Release packaging does not bind current confirmed template')

def validate_independent_approval(review, artifact, *, producer_context):
    """Require explicit independent-context provenance, never infer it from a score."""
    import math
    score = review.get('score')
    reviewer = review.get('reviewer_context')
    if (not producer_context or not isinstance(reviewer, str) or not reviewer.strip()
            or reviewer == producer_context or review.get('independent_context') is not True):
        raise ValueError('Independent review context provenance is missing or is the producer')
    if review.get('approved') is not True or review.get('critical_errors') != []:
        raise ValueError('Independent review is not approved with an empty critical_errors list')
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 85:
        raise ValueError('Independent review score is below 85 or invalid')
    if review.get('artifact_sha256') != sha(artifact):
        raise ValueError('Independent review does not bind current artifact')

def protect_outputs(outputs, protected):
    """All deterministic writers protect caller inputs, including aliases."""
    paths = [Path(p).expanduser().resolve() for p in protected]
    for raw in outputs:
        destination = Path(raw).expanduser()
        if destination.is_symlink():
            raise ValueError('Output must not be a symlink')
        resolved = destination.resolve()
        for source in paths:
            if resolved == source or (destination.exists() and source.exists() and os.path.samefile(destination, source)):
                raise ValueError(f'Output would overwrite a protected input: {destination}')
            if destination.is_dir() and resolved in source.parents:
                raise ValueError(f'Output directory contains a protected input: {destination}')
