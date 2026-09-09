"""Deterministic Word-to-slide materials and confirmed packaging prompt binding."""
from pathlib import Path
import json
import math
import string
import zipfile
from xml.etree import ElementTree as ET
from story_production_v2 import binding, current, sha, write, protect_outputs

def word_text(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read('word/document.xml'))
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    return '\n'.join((''.join((n.text or '' for n in p.findall('.//w:t', ns))) for p in root.findall('.//w:p', ns)))

def material_rows(director, plan, word):
    from static_ppt_contract import validate_plan
    ids, slides = validate_plan(Path(director), Path(plan))
    text = word_text(word)
    result = []
    cursor = 0.0
    last = 0
    shots = {r['shot_id']: r for r in json.loads(Path(director).read_text())['shots']}
    for row in slides:
        sid = row['shot_id']
        source = str(shots.get(sid, {}).get('story_text') or row.get('word_text') or '')
        if 'word_text' in row and row['word_text']:
            source = str(row['word_text'])
        if not source:
            raise ValueError(f'{sid}: missing exact Word text mapping')
        start = text.find(source, last)
        if start < 0:
            raise ValueError(f'{sid}: text does not map verbatim to Word')
        if text.find(source, start + 1) >= 0 and 'word_start' not in row:
            raise ValueError(f'{sid}: ambiguous Word text; provide word_start')
        if 'word_start' in row:
            start = int(row['word_start'])
            if start < last or text[start:start + len(source)] != source:
                raise ValueError(f'{sid}: invalid Word offset')
        last = start + len(source)
        duration = float(row['duration_seconds'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('Invalid authoritative duration')
        if sid in shots:
            shot = shots[sid]
            if abs(float(shot['source_start']) - cursor) > 0.002 or abs(float(shot['source_end']) - (cursor + duration)) > 0.002:
                raise ValueError(f'{sid}: materials timing differs from authoritative director window')
        result.append({'order': len(result) + 1, 'shot_id': sid, 'text': source, 'display_text': source[:-1] if source.endswith('。') else source, 'word_start': start, 'word_end': last, 'start_seconds': cursor, 'end_seconds': cursor + duration, 'image': binding(row['poster_path'])})
        cursor += duration
    return result

def export_materials(*, director, plan, compile_receipt, inputs, output, receipt):
    protect_outputs([output, receipt], [director, plan, compile_receipt, *(v['path'] for v in inputs.values())])
    from shot_storyboard_pipeline import validate_compile_receipt
    compile_data = validate_compile_receipt(Path(compile_receipt), require_current_r2v_jobs=False)
    if compile_data['director_plan_sha256'] != sha(director) or compile_data['ppt_plan_sha256'] != sha(plan):
        raise ValueError('Compile receipt binding changed')
    for role in ('final_word', 'audio', 'finished_music'):
        current(inputs[role])
    rows = material_rows(director, plan, inputs['final_word']['path'])
    from story_video_synthesizer.media import probe_duration
    if abs(rows[-1]['end_seconds'] - probe_duration(Path(inputs['audio']['path']))) > 0.2:
        raise ValueError('PPT materials do not cover authority audio')
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {'schema_version': 'story-ppt-materials/v2', 'rows': rows, 'inputs': {k: inputs[k] for k in ('final_word', 'audio', 'finished_music')}, 'director': binding(director), 'plan': binding(plan), 'compile_receipt': binding(compile_receipt)}
    import tempfile, os
    fd, tmp = tempfile.mkstemp(suffix='.zip', dir=out.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_STORED) as z:
            for row in rows:
                name = f"images/{row['order']:03d}_{row['shot_id']}{Path(row['image']['path']).suffix}"
                if '/' in row['shot_id'] or '\\' in row['shot_id']:
                    raise ValueError('Invalid shot ID')
                row['archive_path'] = name
                z.write(current(row['image']), name)
            for role in ('audio', 'finished_music'):
                item = inputs[role]
                z.write(current(item), role + Path(item['path']).suffix)
            z.writestr('manifest.json', json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    payload['archive'] = binding(out)
    write(receipt, payload)
    validate_materials(receipt, inputs)
    return payload

def validate_materials(path, inputs=None):
    p = json.loads(Path(path).read_text())
    if p.get('schema_version') != 'story-ppt-materials/v2':
        raise ValueError('Wrong materials version')
    for role, item in p['inputs'].items():
        current(item)
        if inputs and item != inputs[role]:
            raise ValueError('Materials input binding changed')
    for key in ('director', 'plan', 'compile_receipt', 'archive'):
        current(p[key])
    from shot_storyboard_pipeline import validate_compile_receipt
    compile_data = validate_compile_receipt(Path(p['compile_receipt']['path']), require_current_r2v_jobs=False)
    if compile_data['director_plan_sha256'] != p['director']['sha256'] or compile_data['ppt_plan_sha256'] != p['plan']['sha256']:
        raise ValueError('Materials compile binding changed')
    expected = material_rows(p['director']['path'], p['plan']['path'], p['inputs']['final_word']['path'])
    if [{k: v for k, v in r.items() if k != 'archive_path'} for r in p['rows']] != expected:
        raise ValueError('Materials order/text/timing mismatch')
    with zipfile.ZipFile(p['archive']['path']) as z:
        archived = json.loads(z.read('manifest.json'))
        if archived != {k: v for k, v in p.items() if k != 'archive'}:
            raise ValueError('Packaged manifest differs from reviewed materials')
        import hashlib
        for row in p['rows']:
            if hashlib.sha256(z.read(row['archive_path'])).hexdigest() != row['image']['sha256']:
                raise ValueError('Packaged image drift')
        for role in ('audio', 'finished_music'):
            item = p['inputs'][role]
            if hashlib.sha256(z.read(role + Path(item['path']).suffix)).hexdigest() != item['sha256']:
                raise ValueError('Packaged audio edited')
    return p

def bind_packaging(*, inputs, fields, output, receipt):
    protect_outputs([output, receipt], [v['path'] for v in inputs.values()])
    allowed = {'story_name', 'story_type', 'age_range', 'duration_text', 'theme_style'}
    if set(fields) != allowed:
        raise ValueError('Only story information, duration and theme fields are permitted')
    from story_delivery_policy import normalize_duration_label
    fields = {**fields, 'duration_text': normalize_duration_label(fields['duration_text'])}
    template = current(inputs['packaging_prompt']).read_text()
    names = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    if not names <= allowed:
        raise ValueError('Unapproved packaging template field')
    current(inputs['packaging_reference'])
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(template.format(**fields))
    payload = {'schema_version': 'story-confirmed-packaging/v2', 'inputs': {k: inputs[k] for k in ('packaging_prompt', 'packaging_reference')}, 'fields': fields, 'output': binding(output), 'generated': False}
    write(receipt, payload)
    return payload

def validate_packaging(path, inputs=None):
    p = json.loads(Path(path).read_text())
    if p.get('schema_version') != 'story-confirmed-packaging/v2':
        raise ValueError('Invalid packaging version')
    for role, item in p['inputs'].items():
        current(item)
        if inputs and item != inputs[role]:
            raise ValueError('Packaging input changed')
    if current(p['output']).read_text() != current(p['inputs']['packaging_prompt']).read_text().format(**p['fields']):
        raise ValueError('Packaging prompt diverged from confirmed template')
    return p

def validate_panel_binding(config, spec_path, generation_path):
    """Bind approved native panels to the current confirmed prompt and reference."""
    from story_production_v2 import current
    spec = validate_packaging(spec_path)
    generation = json.loads(Path(generation_path).read_text())
    if generation.get('schema_version') != 'story-confirmed-panels/v2':
        raise ValueError('Invalid panel generation receipt')
    if generation.get('prompt_receipt_sha256') != sha(spec_path):
        raise ValueError('Panels are bound to another confirmed prompt')
    if generation.get('reference_attached') is not True or generation.get('imagegen_native') is not True:
        raise ValueError('Panel reference/native generation evidence missing')
    if not generation.get('request_id'):
        raise ValueError('Panel request ID missing')
    checks = generation.get('checks', {})
    for key in ('text_matches_confirmed_prompt', 'no_reference_story_leak', 'top_bottom_coherent', 'simple_layout'):
        if checks.get(key) is not True:
            raise ValueError(f'Panel validation missing: {key}')
    from story_evidence import review_passes, review_bundle_is_current
    review_path = current(generation['review'])
    review = json.loads(review_path.read_text())
    bundle = current(generation['review_bundle'])
    from story_production_v2 import validate_independent_approval
    validate_independent_approval(review, bundle, producer_context=generation.get('producer_context'))
    if not review_bundle_is_current(bundle) or not review_passes(review, artifact=bundle):
        raise ValueError('Panel independent review invalid')
    members = json.loads(bundle.read_text())['artifacts']
    if isinstance(members, dict):
        members = members.values()
    hashes = {x['sha256'] for x in members}
    for name, p in [('top_plate', config.main_top_panel), ('bottom_plate', config.main_bottom_panel), ('library_top_plate', config.library_top_panel), ('library_bottom_plate', config.library_bottom_panel)]:
        if p is None:
            continue
        item = generation['outputs'][name]
        if current(item) != Path(p).resolve() or item['sha256'] not in hashes:
            raise ValueError('Panel is not reviewed current output')
    if spec['fields']['duration_text'] != config.duration_text:
        raise ValueError('Packaging duration does not match actual render')
    if spec['fields']['story_name'].strip('《》') != config.story_name.strip('《》'):
        raise ValueError('Packaging belongs to another story')
    return {'reference_asset': spec['inputs']['packaging_reference']['path'], 'reference_sha256': spec['inputs']['packaging_reference']['sha256'], 'reference_role': 'main_vertical_package', 'main_package_spec_path': str(Path(spec_path).resolve()), 'main_package_spec_sha256': sha(spec_path), 'main_package_receipt_path': str(Path(generation_path).resolve()), 'main_package_receipt_sha256': sha(generation_path), 'panel_sha256': {k: sha(p) for k, p in [('main_top_panel', config.main_top_panel), ('main_bottom_panel', config.main_bottom_panel), ('library_top_panel', config.library_top_panel), ('library_bottom_panel', config.library_bottom_panel)] if p is not None}}
