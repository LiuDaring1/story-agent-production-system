"""Deterministic Word-to-slide materials and confirmed packaging prompt binding."""
from pathlib import Path
import json
import math
import string
import zipfile
from xml.etree import ElementTree as ET
from story_production_v2 import binding, current, sha, write, protect_outputs


class MaterialsPreflightError(ValueError):
    def __init__(self, issues):
        self.issues = list(issues)
        super().__init__('Materials preflight failed: ' + '；'.join(self.issues))

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
    issues = []
    cursor = 0.0
    last = 0
    shots = {r['shot_id']: r for r in json.loads(Path(director).read_text())['shots']}
    for row in slides:
        sid = row['shot_id']
        try:
            duration = float(row['duration_seconds'])
        except (KeyError, TypeError, ValueError):
            duration = math.nan
        duration_valid = math.isfinite(duration) and duration > 0
        if not duration_valid:
            issues.append(f'{sid}: invalid authoritative duration')
        start_seconds = cursor
        if duration_valid:
            if sid in shots:
                shot = shots[sid]
                if abs(float(shot['source_start']) - cursor) > 0.002 or abs(float(shot['source_end']) - (cursor + duration)) > 0.002:
                    issues.append(f'{sid}: materials timing differs from authoritative director window')
            cursor += duration
        source = str(shots.get(sid, {}).get('story_text') or row.get('word_text') or '')
        if 'word_text' in row and row['word_text']:
            source = str(row['word_text'])
        if not source:
            issues.append(f'{sid}: missing exact Word text mapping')
            continue
        start = text.find(source, last)
        if start < 0:
            issues.append(f'{sid}: text does not map verbatim to Word')
            continue
        if text.find(source, start + 1) >= 0 and 'word_start' not in row:
            issues.append(f'{sid}: ambiguous Word text; provide word_start')
            continue
        if 'word_start' in row:
            start = int(row['word_start'])
            if start < last or text[start:start + len(source)] != source:
                issues.append(f'{sid}: invalid Word offset')
                continue
        last = start + len(source)
        if not duration_valid:
            continue
        result.append({'order': len(result) + 1, 'shot_id': sid, 'text': source, 'display_text': source[:-1] if source.endswith('。') else source, 'word_start': start, 'word_end': last, 'start_seconds': start_seconds, 'end_seconds': start_seconds + duration, 'image': binding(row['poster_path'])})
    if issues:
        raise MaterialsPreflightError(issues)
    return result


def materials_preflight(*, director, plan, compile_receipt, inputs):
    """Report compile/input/Word mapping defects in one read-only pass."""
    issues = []
    compile_data = None
    rows = None
    from shot_storyboard_pipeline import validate_compile_receipt
    try:
        compile_data = validate_compile_receipt(Path(compile_receipt), require_current_r2v_jobs=False)
        if compile_data['director_plan_sha256'] != sha(director):
            issues.append('compile_receipt: director plan binding changed')
        if compile_data['ppt_plan_sha256'] != sha(plan):
            issues.append('compile_receipt: PPT plan binding changed')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        issues.append(f'compile_receipt: {exc}')
    for role in ('final_word', 'audio', 'finished_music'):
        try:
            current(inputs[role])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            issues.append(f'{role}: {exc}')
    try:
        rows = material_rows(director, plan, inputs['final_word']['path'])
    except MaterialsPreflightError as exc:
        issues.extend(exc.issues)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        issues.append(f'word_mapping: {exc}')
    if issues:
        raise MaterialsPreflightError(issues)
    return compile_data, rows

def export_materials(*, director, plan, compile_receipt, inputs, output, receipt, archive_output=None):
    """Export a ready-to-use ordered folder; ZIP requires explicit opt-in."""
    from story_run import run_file_lock
    with run_file_lock(Path(receipt)):
        return _export_materials(director=director, plan=plan, compile_receipt=compile_receipt,
                                 inputs=inputs, output=output, receipt=receipt, archive_output=archive_output)


def _export_materials(*, director, plan, compile_receipt, inputs, output, receipt, archive_output):
    import shutil, tempfile, os
    targets = [output, receipt] + ([archive_output] if archive_output else [])
    protect_outputs(targets, [director, plan, compile_receipt, *(v['path'] for v in inputs.values())])
    if Path(receipt).resolve().is_relative_to(Path(output).resolve()):
        raise ValueError('Receipt must be outside materials directory')
    if archive_output and (Path(archive_output).resolve().is_relative_to(Path(output).resolve()) or Path(archive_output).resolve() == Path(receipt).resolve()):
        raise ValueError('Archive must be outside materials directory and receipt')
    _compile_data, rows = materials_preflight(
        director=director,
        plan=plan,
        compile_receipt=compile_receipt,
        inputs=inputs,
    )
    from story_video_synthesizer.media import probe_duration
    if abs(rows[-1]['end_seconds'] - probe_duration(Path(inputs['audio']['path']))) > 0.2:
        raise ValueError('PPT materials do not cover authority audio')
    payload = {'schema_version': 'story-ppt-materials/v3', 'rows': rows,
               'inputs': {k: inputs[k] for k in ('final_word', 'audio', 'finished_music')},
               'director': binding(director), 'plan': binding(plan), 'compile_receipt': binding(compile_receipt)}
    for row in rows:
        if '/' in row['shot_id'] or chr(92) in row['shot_id'] or row['shot_id'] in {'.', '..'}:
            raise ValueError('Invalid shot ID')
        row['archive_path'] = f"images/{row['order']:03d}_{row['shot_id']}{Path(row['image']['path']).suffix}"
    out = Path(output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Validate ownership before modifying a previous export. Unchanged exports reuse bytes.
    old = None
    if out.exists():
        if not Path(receipt).is_file():
            if not (out / 'manifest.json').is_file() or json.loads((out / 'manifest.json').read_text()) != payload:
                raise ValueError('Unmanaged materials destination collision')
            recovered = {**payload, 'directory': binding(out)}
            write(receipt, recovered)
            validate_materials(receipt, inputs)
        old = json.loads(Path(receipt).read_text())
        if old.get('directory', {}).get('path') != str(out):
            raise ValueError('Cannot overwrite legacy or unmanaged materials')
        current(old['directory'])
        if {k: v for k, v in old.items() if k not in {'directory', 'archive'}} == payload:
            validate_materials(receipt, inputs)
            _write_optional_archive(old, archive_output, receipt)
            return {**old, 'reused': True}
        # Changed input export needs a fresh destination; preserve previous recovery evidence.
        raise ValueError('Materials changed; export to a new versioned directory')
    tmp = Path(tempfile.mkdtemp(prefix='.materials-', dir=out.parent))
    try:
        for row in rows:
            dest = tmp / row['archive_path']; dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current(row['image']), dest)
        for role in ('audio', 'finished_music'):
            item = inputs[role]
            shutil.copy2(current(item), tmp / (role + Path(item['path']).suffix))
        write(tmp / 'manifest.json', payload)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    payload['directory'] = binding(out)
    # Persist directory ownership first, including if optional ZIP creation is interrupted.
    write(receipt, payload)
    _write_optional_archive(payload, archive_output, receipt)
    validate_materials(receipt, inputs)
    return payload


def _write_optional_archive(payload, archive_output, receipt):
    import os, tempfile
    if not archive_output:
        return
    out = current(payload['directory'])
    archive = Path(archive_output).resolve()
    if payload.get('archive', {}).get('path') == str(archive):
        current(payload['archive'])
        return
    if archive.exists():
        # A crash after ZIP replacement is recoverable only if every entry matches.
        with zipfile.ZipFile(archive) as z:
            expected = {m['relative_path'] for m in payload['directory']['members']}
            if set(z.namelist()) != expected or len(z.namelist()) != len(expected):
                raise ValueError('Optional archive destination collision')
            if any(z.read(name) != (out / name).read_bytes() for name in expected):
                raise ValueError('Optional archive destination collision')
    else:
        archive.parent.mkdir(parents=True, exist_ok=True)
        fd, tmpzip = tempfile.mkstemp(suffix='.zip', dir=archive.parent); os.close(fd)
        try:
            with zipfile.ZipFile(tmpzip, 'w', compression=zipfile.ZIP_STORED) as z:
                for member in payload['directory']['members']:
                    z.write(out / member['relative_path'], member['relative_path'])
            os.replace(tmpzip, archive)
        finally:
            if os.path.exists(tmpzip): os.unlink(tmpzip)
    payload['archive'] = binding(archive)
    write(receipt, payload)


def validate_materials(path, inputs=None):
    p = json.loads(Path(path).read_text())
    if p.get('schema_version') not in {'story-ppt-materials/v2', 'story-ppt-materials/v3'}:
        raise ValueError('Wrong materials version')
    for role, item in p['inputs'].items():
        current(item)
        if inputs and item != inputs[role]:
            raise ValueError('Materials input binding changed')
    for key in ('director', 'plan', 'compile_receipt'):
        current(p[key])
    from shot_storyboard_pipeline import validate_compile_receipt
    compile_data = validate_compile_receipt(Path(p['compile_receipt']['path']), require_current_r2v_jobs=False)
    if compile_data['director_plan_sha256'] != p['director']['sha256'] or compile_data['ppt_plan_sha256'] != p['plan']['sha256']:
        raise ValueError('Materials compile binding changed')
    expected = material_rows(p['director']['path'], p['plan']['path'], p['inputs']['final_word']['path'])
    if [{k: v for k, v in r.items() if k != 'archive_path'} for r in p['rows']] != expected:
        raise ValueError('Materials order/text/timing mismatch')
    def check_contents(read):
        archived = json.loads(read('manifest.json'))
        if archived != {k: v for k, v in p.items() if k not in {'directory', 'archive'}}:
            raise ValueError('Packaged manifest differs from reviewed materials')
        import hashlib
        for row in p['rows']:
            if hashlib.sha256(read(row['archive_path'])).hexdigest() != row['image']['sha256']:
                raise ValueError('Packaged image drift')
        for role in ('audio', 'finished_music'):
            item = p['inputs'][role]
            if hashlib.sha256(read(role + Path(item['path']).suffix)).hexdigest() != item['sha256']:
                raise ValueError('Packaged audio edited')
    if p['schema_version'] == 'story-ppt-materials/v3':
        root = current(p['directory'])
        check_contents(lambda name: (root / name).read_bytes())
    if 'archive' in p:
        with zipfile.ZipFile(current(p['archive'])) as z:
            check_contents(z.read)
    elif p['schema_version'] == 'story-ppt-materials/v2':
        raise ValueError('Legacy materials archive missing')
    return p

def bind_packaging(*, inputs, output, receipt, fields=None, timeline_receipt=None):
    story_fields = None
    if timeline_receipt is not None:
        from story_packaging_defaults import packaging_fields
        story_fields = packaging_fields(inputs, timeline_receipt)
        if fields is not None and fields != story_fields:
            raise ValueError("Packaging fields differ from project story information/timeline")
        fields = story_fields
    if fields is None:
        raise ValueError("Packaging requires current authoritative timeline receipt")
    scope_paths = {'main': Path(output), 'library': Path(output).with_name(Path(output).stem + '.library.txt'), 'frame': Path(output).with_name(Path(output).stem + '.frame.txt')}
    protect_outputs([*scope_paths.values(), receipt], [v['path'] for v in inputs.values()])
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
    if story_fields is not None:
        payload['story_requirements'] = inputs['story_requirements']
        payload['timeline_receipt'] = timeline_receipt
    from story_visual_contracts import compile_visual_scopes
    payload['visual_scopes'] = compile_visual_scopes(inputs, fields)
    for scope, scope_path in scope_paths.items():
        scope_path.write_text(payload['visual_scopes'][scope]['prompt'])
    payload['output_scope'] = 'main'
    payload['scope_prompt_outputs'] = {scope: binding(scope_path) for scope, scope_path in scope_paths.items()}
    payload['scope_inputs'] = {k: inputs[k] for k in ('story_requirements',) if k in inputs}
    write(receipt, payload)
    return payload

def validate_packaging(path, inputs=None):
    p = json.loads(Path(path).read_text())
    if p.get('schema_version') != 'story-confirmed-packaging/v2':
        raise ValueError('Invalid packaging version')
    if 'timeline_receipt' in p:
        from story_packaging_defaults import packaging_fields
        if inputs is None:
            from story_timeline import validate_authoritative_timeline_receipt
            t = validate_authoritative_timeline_receipt(current(p['timeline_receipt']))
            field_inputs = {'story_requirements': p['story_requirements'], 'audio': t['authoritative_audio'], 'subtitle_txt': t['subtitle_txt']}
            if 'subtitle_srt' in t:
                field_inputs['subtitle_srt'] = t['subtitle_srt']
        else:
            field_inputs = inputs
            if p['story_requirements'] != inputs['story_requirements']:
                raise ValueError('Story information changed')
        if p['fields'] != packaging_fields(field_inputs, p['timeline_receipt']):
            raise ValueError('Packaging story information/timeline changed')
    for role, item in p['inputs'].items():
        current(item)
        if inputs and item != inputs[role]:
            raise ValueError('Packaging input changed')
    if current(p['output']).read_text() != current(p['inputs']['packaging_prompt']).read_text().format(**p['fields']):
        raise ValueError('Packaging prompt diverged from confirmed template')
    if 'visual_scopes' in p:
        from story_visual_contracts import compile_visual_scopes
        scope_inputs = {**p['inputs'], **p.get('scope_inputs', {})}
        if inputs and any(inputs.get(k) != v for k, v in p.get('scope_inputs', {}).items()):
            raise ValueError('Visual scope inputs changed')
        if p['visual_scopes'] != compile_visual_scopes(scope_inputs, p['fields']):
            raise ValueError('Visual scope rules or references changed')
        scope_outputs = p.get('scope_prompt_outputs', {})
        if p.get('output_scope') != 'main' or set(scope_outputs) != {'main', 'library', 'frame'} or scope_outputs['main'] != p['output']:
            raise ValueError('Packaging prompt output scope is missing or ambiguous')
        for scope, item in scope_outputs.items():
            if current(item).read_text() != p['visual_scopes'][scope]['prompt']:
                raise ValueError('Scoped prompt output differs: ' + scope)
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
    if 'visual_scopes' in spec:
        scoped = generation.get('scope_checks', {})
        for scope in ('main', 'library'):
            for check in spec['visual_scopes'][scope]['review_checks']:
                if scoped.get(scope, {}).get(check) is not True:
                    raise ValueError(f'Scoped panel validation missing: {scope}.{check}')
    else:
        # Read-only compatibility for pre-scope receipts. Never apply these
        # aggregate historical checks to newly compiled scoped packaging.
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
    if 'visual_scopes' in spec and sha(spec_path) not in hashes:
        raise ValueError('Panel review bundle must include current scoped prompt receipt')
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
