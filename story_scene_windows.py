"""Deterministic ABC preparation and decoded-frame coverage; no visual approval."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path


def segments(duration, b_windows, c_windows):
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('ABC requires a finite positive duration')
    intervals = sorted((float(s), float(e), mode) for mode, ws in [('b', b_windows), ('c', c_windows)] for s, e in ws)
    last = 0.
    result = []
    for start, end, mode in intervals:
        if not all(math.isfinite(t) for t in (start, end)) or start < 0 or end <= start or end > duration + .001:
            raise ValueError('ABC window outside authoritative duration or invalid')
        if start < last:
            raise ValueError('ABC windows overlap/conflict')
        if start > last:
            result.append((last, start, 'a'))
        result.append((start, min(end, duration), mode)); last = min(end, duration)
    if last < duration:
        result.append((last, duration, 'a'))
    return result


def coverage_samples(duration, b_windows, c_windows, fps=25.):
    """Midpoint of every actual segment and both sides of every switch."""
    spans = segments(duration, b_windows, c_windows)
    values = {}
    def add(t, reason):
        t = round(max(0., min(duration - 1/fps, t)), 3)
        values.setdefault(t, []).append(reason)
    for start, end, mode in spans:
        add((start+end)/2, 'segment_'+mode)
    for (_, end, mode), (_, _, following) in zip(spans, spans[1:]):
        if mode != following:
            add(end - 2/fps, 'before_switch'); add(end + 2/fps, 'after_switch')
    return [{'time_seconds': t, 'expected_mode': mode_at(t, b_windows, c_windows), 'reasons': reasons}
            for t, reasons in sorted(values.items())]


def review_samples(duration, b_windows, c_windows, severe_windows, fps=25.):
    """Prepare every actual switch plus explicit presenter-risk and A/B evidence."""
    values = {row['time_seconds']: dict(row) for row in coverage_samples(duration, b_windows, c_windows, fps)}
    def add(timestamp, reason):
        timestamp = round(max(0., min(duration - 1 / fps, timestamp)), 3)
        row = values.setdefault(timestamp, {
            'time_seconds': timestamp,
            'expected_mode': mode_at(timestamp, b_windows, c_windows),
            'reasons': [],
        })
        if reason not in row['reasons']:
            row['reasons'].append(reason)
    for start, end in severe_windows:
        add(start + 2 / fps, 'presenter_risk_start')
        add((start + end) / 2, 'presenter_risk_midpoint')
        add(end - 2 / fps, 'presenter_risk_end')
    for mode in ('a', 'b'):
        candidate = next((row for row in values.values() if row['expected_mode'] == mode), None)
        if candidate is not None:
            candidate['reasons'].append('shared_frame_comparison_' + mode)
    return [values[key] for key in sorted(values)]


def mode_at(t, b_windows, c_windows):
    # FFmpeg between() is inclusive; C overlays B at a shared boundary.
    if any(s <= t <= e for s, e in c_windows): return 'c'
    if any(s <= t <= e for s, e in b_windows): return 'b'
    return 'a'


def current_rule(item):
    from story_production_v2 import current
    # Code may be restored into another checkout; require the exact same bytes.
    relative = item.get('source_relative_path')
    if relative:
        root = Path(__file__).resolve().parent
        path = (root / relative).resolve()
        if not path.is_relative_to(root): raise ValueError('ABC rule source escapes source root')
        return current({**item, 'path': str(path)})
    return current(item)


def validate_plan(path, *, ledger=None):
    from story_production_v2 import current
    from story_release_policy import WINDOWS_PLAN_SCHEMA, validate_presenter_scan_report
    from release_video import parse_b_windows
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if payload.get('schema_version') != WINDOWS_PLAN_SCHEMA:
        raise ValueError('v2 release windows plan lacks current presenter protection evidence')
    for item in payload.get('inputs', {}).values(): current(item)
    for item in payload.get('rules', []): current_rule(item)
    protection = payload.get('presenter_protection')
    if not isinstance(protection, dict) or protection.get('status') != 'all_risks_covered_by_b_or_c':
        raise ValueError('v2 release windows plan missing presenter protection result')
    report_path = current(protection.get('report') or {})
    report = validate_presenter_scan_report(report_path)
    foreground = current(payload['inputs']['presenter_foreground'])
    if (Path(report['source']['path']).resolve() != foreground.resolve()
            or report['source']['sha256'] != payload['inputs']['presenter_foreground']['sha256']):
        raise ValueError('v2 release windows plan scan binds another presenter foreground')
    if protection.get('cache_key') != report.get('cache_key'):
        raise ValueError('v2 release windows plan presenter scan parameters differ from evidence')
    severe = [tuple(map(float, row)) for row in report.get('severe_windows', [])]
    if protection.get('severe_windows') != [list(row) for row in severe]:
        raise ValueError('v2 release windows plan omits presenter risk intervals')
    timeline = current(payload['inputs']['authoritative_timeline_receipt'])
    try:
        duration = float(json.loads(timeline.read_text())['audio_duration_seconds'])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError('v2 release windows plan lacks a valid authoritative duration') from exc
    if abs(duration - float(payload.get('duration_seconds') or 0)) > .001:
        raise ValueError('v2 release windows plan duration differs from authoritative timeline')
    bw = parse_b_windows(str(payload.get('b_windows') or ''))
    cw = parse_b_windows(str(payload.get('c_windows') or ''))
    spans = segments(duration, bw, cw)
    for start, end in severe:
        uncovered = [(start, end)]
        for cover_start, cover_end in sorted([*bw, *cw]):
            next_rows = []
            for left, right in uncovered:
                if cover_end <= left or cover_start >= right:
                    next_rows.append((left, right))
                else:
                    if cover_start > left: next_rows.append((left, min(right, cover_start)))
                    if cover_end < right: next_rows.append((max(left, cover_end), right))
            uncovered = next_rows
        if any(right - left > .002 for left, right in uncovered):
            raise ValueError('v2 release windows plan leaves presenter torso risk in A')
    expected_segments = [{'start': s, 'end': e, 'mode': m} for s, e, m in spans]
    if payload.get('segments') != expected_segments:
        raise ValueError('v2 release windows plan segments do not match its B/C windows')
    expected_samples = review_samples(duration, bw, cw, severe)
    if payload.get('review_samples') != expected_samples:
        raise ValueError('v2 release windows plan review evidence points are incomplete')
    if ledger is not None:
        for role in ('subtitle_srt',):
            if current(payload['inputs'][role]).resolve() != current(ledger['inputs'][role]).resolve():
                raise ValueError('v2 release windows plan binds another subtitle input')
        expected_timeline = current(ledger['artifacts']['authoritative_timeline_receipt'])
        if timeline.resolve() != expected_timeline.resolve():
            raise ValueError('v2 release windows plan binds another timeline')
    return payload


def prepare_plan(
    run_file, output, *, presenter_foreground, fixed_anchor_x,
    canvas_width=1920, source_width=1920, source_height=1080,
    rendered_height=1080, fixed_anchor_y=0, person_crop=None,
    person_layout_policy='source-native-fixed-anchor/v2',
    b_windows=None, c_windows=None,
):
    from story_run import load_run
    from story_production_v2 import current, binding, write
    from story_timeline import validate_authoritative_timeline_receipt
    from story_workflow import build_abc_scene_windows
    from release_video import parse_b_windows
    from presenter_layout import scan_rvm_body_overflow
    from story_release_policy import (
        WINDOWS_PLAN_SCHEMA, cover_presenter_risks, format_windows,
        presenter_scan_cache_path,
    )
    run = load_run(Path(run_file))
    if run.get('production_contract') != 'story-production/v2':
        raise ValueError('ABC preparation requires story-production/v2')
    root = Path(run['project_dir']).resolve()
    output = Path(output).resolve()
    if not output.is_relative_to(root): raise ValueError('ABC plan output must be inside current project')
    timeline = current(run['artifacts']['authoritative_timeline_receipt'])
    validate_authoritative_timeline_receipt(timeline)
    srt = current(run['inputs']['subtitle_srt'])
    duration = json.loads(timeline.read_text())['audio_duration_seconds']
    if (b_windows is None) != (c_windows is None):
        raise ValueError('explicit ABC plan requires both b_windows and c_windows')
    if b_windows is None:
        base_b, base_c = build_abc_scene_windows(duration, srt)
        preparation = 'mature_auto_with_presenter_protection'
    else:
        base_b, base_c = str(b_windows), str(c_windows)
        preparation = 'explicit_with_presenter_protection'
    base_bw, cw = parse_b_windows(base_b), parse_b_windows(base_c)
    segments(duration, base_bw, cw)
    scan_parameters = dict(
        fixed_anchor_x=int(fixed_anchor_x), fixed_anchor_y=int(fixed_anchor_y),
        canvas_width=int(canvas_width), source_width=int(source_width),
        source_height=int(source_height), rendered_height=int(rendered_height),
        person_crop=person_crop, person_layout_policy=person_layout_policy,
    )
    report_path, expected_cache_key = presenter_scan_cache_path(
        root, Path(presenter_foreground), **scan_parameters,
    )
    overflow = scan_rvm_body_overflow(
        Path(presenter_foreground), report_path=report_path, **scan_parameters,
    )
    if overflow.get('cache_key') != expected_cache_key:
        raise ValueError('presenter overflow scan returned stale cache evidence')
    severe = [tuple(map(float, row)) for row in overflow.get('severe_windows', [])]
    bw, protection_coverage = cover_presenter_risks(base_bw, cw, severe, duration=duration)
    b, c = format_windows(bw), format_windows(cw)
    spans = segments(duration, bw, cw)
    if duration >= 30 and (not bw or not cw or not any(x[2] == 'a' for x in spans)):
        raise ValueError('mature auto plan cannot supply all A/B/C at this duration; explicit reviewed plan required')
    rules = [{**binding(Path(__file__).parent/relative), 'source_relative_path':relative} for relative in
             ['story_scene_windows.py','story_release_policy.py','presenter_layout.py','story_workflow.py',
              'skills/story-full-auto/references/video-invariants.md']]
    payload = {'schema_version': WINDOWS_PLAN_SCHEMA, 'preparation': preparation,
        'inputs': {'subtitle_srt': binding(srt), 'authoritative_timeline_receipt': binding(timeline),
                   'presenter_foreground': binding(Path(presenter_foreground))},
        'rules': rules, 'duration_seconds': duration, 'b_windows': b, 'c_windows': c,
        'base_windows': {'b_windows': base_b, 'c_windows': base_c},
        'presenter_protection': {
            'status': 'all_risks_covered_by_b_or_c',
            'report': binding(report_path),
            'cache_key': overflow['cache_key'],
            'severe_windows': [list(row) for row in severe],
            'coverage': protection_coverage,
        },
        'segments': [{'start': s, 'end': e, 'mode': m} for s,e,m in spans],
        'review_samples': review_samples(duration,bw,cw,severe), 'independent_review': 'merge_with_release_preview'}
    if output.exists():
        existing = json.loads(output.read_text())
        def identity(value):
            return {**value, 'rules':[{k:v for k,v in r.items() if k != 'path'} for r in value['rules']]}
        if identity(existing) != identity(payload):
            raise ValueError('existing ABC plan differs; use a new output path for changed inputs/rules')
        for rule in existing['rules']: current_rule(rule)
        validate_plan(output, ledger=run)
        return existing
    else: write(output, payload)
    validate_plan(output, ledger=run)
    return payload


def frame_templates(config, directory):
    from release_video import prepare_story_frame_assets
    from story_production_v2 import binding, write
    from story_release_policy import FRAME_DERIVATION_SCHEMA
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    if config.frame_image is None:
        raise ValueError('shared story-frame mother asset is missing')
    result = {}
    derivatives = {}
    for mode, box in [('a',config.story_box), ('b',config.b_story_box)]:
        path, _, _ = prepare_story_frame_assets(config.frame_image, box, directory/f'frame_{mode}.png', directory/f'mask_{mode}.png',config.story_bleed)
        result[mode] = binding(path)
        derivatives[mode] = {
            'source_sha256': binding(config.frame_image)['sha256'],
            'story_window': list(box),
            'method': 'deterministic_fit_to_story_window',
            'output': binding(path),
        }
    receipt = directory / 'frame_derivation_receipt.json'
    payload = {
        'schema_version': FRAME_DERIVATION_SCHEMA,
        'production_contract': 'story-production/v2',
        'mother_asset': binding(config.frame_image),
        'independent_b_asset': False,
        'derivatives': derivatives,
    }
    if receipt.exists() and json.loads(receipt.read_text()) != payload:
        raise ValueError('existing A/B frame derivation differs; use a new evidence directory')
    if not receipt.exists(): write(receipt, payload)
    result['derivation_receipt'] = binding(receipt)
    return result


def observed_frame_mode(image, templates, presenter_image=None):
    """Measure opaque frame pixels at actual A/B geometry, never trust scene labels.

    Conservative structural detector. Ambiguous frame designs fail closed for
    investigation; independent review still owns presenter and visual quality.
    """
    import numpy as np
    from PIL import Image, ImageFilter
    from story_production_v2 import current
    actual = np.asarray(image.convert('RGB').resize((1920,1080))).astype(float)
    scores = {}
    for mode, item in ((mode, templates[mode]) for mode in ('a', 'b')):
        frame = Image.open(current(item)).convert('RGBA').resize((1920,1080))
        # Erode opaque mask to omit antialiasing; right-hand presenter and bottom
        # subtitles are excluded from the invariant frame-color measurement.
        mask_image = frame.getchannel('A').point(lambda x: 255 if x >= 250 else 0).filter(ImageFilter.MinFilter(7))
        mask = np.asarray(mask_image) > 0
        mask[:,1280:] = False; mask[850:,:] = False
        if mask.sum() < 200: raise ValueError('insufficient opaque frame pixels for ABC geometry measurement')
        expected = np.asarray(frame.convert('RGB')).astype(float)
        distance = np.abs(actual-expected).mean(axis=2)[mask]
        scores[mode] = float((distance <= 28).mean())
    ranked = sorted(scores, key=scores.get, reverse=True)
    best, other = ranked
    if scores[best] >= .65 and scores[best]-scores[other] >= .15: observed = best
    elif max(scores.values()) < .25: observed = 'unknown'
    else: observed = 'unknown'
    person_match = None
    if observed == 'unknown' and max(scores.values()) < .25 and presenter_image is not None:
        person = np.asarray(presenter_image.convert('RGBA').resize((1920,1080)))
        person_mask = person[:,:,3] >= 240
        if person_mask.sum() >= 200:
            distances = np.abs(actual-person[:,:,:3].astype(float)).mean(axis=2)[person_mask]
            person_match = float((distances < 45).mean())
            if person_match >= .65: observed = 'c'
    return {'observed_mode': observed, 'presenter_match_fraction': person_match, 'frame_match_fraction': scores,
            'measurement': 'opaque_reviewed_frame_geometry/v1'}


def presenter_evidence(config, geometry):
    from story_production_v2 import binding
    return {'source':binding(config.person_greenscreen), 'geometry':geometry, 'decoder':'libvpx-vp9' if config.keyer == 'rvm' else None}


def decode_presenter(evidence, timestamp, path):
    import subprocess
    from PIL import Image
    from story_production_v2 import current
    source=current(evidence['source'])
    command=['ffmpeg','-v','error','-y','-ss',str(timestamp)]
    if evidence.get('decoder'): command += ['-c:v',evidence['decoder']]
    subprocess.run([*command,'-i',str(source),'-frames:v','1',str(path)],check=True,capture_output=True)
    g=evidence['geometry']; x,y,w,h=g['source_crop']
    with Image.open(path) as raw:
        person=raw.convert('RGBA').crop((x,y,x+w,y+h)).resize((g['rendered_width'],g['rendered_height']))
    canvas=Image.new('RGBA',(1920,1080));canvas.paste(person,(g['x'],g['y']))
    return canvas


def audit_video(video, plan, templates, output, *, video_box=None, presenter=None):
    import subprocess
    from PIL import Image
    from story_production_v2 import current, binding, write
    from release_video import parse_b_windows
    video = Path(video); plan = Path(plan); output = Path(output)
    before = binding(video); plan_bound = binding(plan)
    payload = validate_plan(plan)
    from story_release_policy import validate_frame_derivation
    derivation = validate_frame_derivation(current(templates.get('derivation_receipt') or {}))
    duration = json.loads(current(payload['inputs']['authoritative_timeline_receipt']).read_text())['audio_duration_seconds']
    bw, cw = parse_b_windows(payload['b_windows']), parse_b_windows(payload['c_windows'])
    spans = segments(duration,bw,cw)
    if duration >= 30 and {x[2] for x in spans} != {'a','b','c'}: raise ValueError('main ABC plan lacks required A/B/C')
    from release_video import validate_main_scene_ending
    validate_main_scene_ending(spans,duration,current(payload['inputs']['subtitle_srt']))
    samples = payload['review_samples']
    root = output.parent / (output.stem+'_frames'); root.mkdir(parents=True,exist_ok=True)
    errors = []
    for i, sample in enumerate(samples):
        frame = root/f'{i:04d}.png'
        subprocess.run(['ffmpeg','-v','error','-y','-ss',str(sample['time_seconds']),'-i',str(video),'-frames:v','1',str(frame)],check=True,capture_output=True)
        with Image.open(frame) as image:
            if video_box:
                x,y,w,h = video_box; image=image.crop((x,y,x+w,y+h))
            person_image = decode_presenter(presenter,sample['time_seconds'],root/f'{i:04d}_presenter.png') if presenter and sample['expected_mode']=='c' else None
            measurement = observed_frame_mode(image,templates,person_image)
        sample.update(measurement); sample['frame'] = binding(frame)
        if sample['expected_mode'] != sample['observed_mode']:
            errors.append(f"ABC actual mode mismatch at {sample['time_seconds']}: expected {sample['expected_mode']}, observed {sample['observed_mode']}")
    if binding(video) != before or binding(plan) != plan_bound: errors.append('ABC input changed during audit')
    plan_compliance = {'passed': True, 'presenter_protection': payload['presenter_protection']}
    execution_compliance = {'passed': not errors, 'critical_errors': list(errors)}
    report = {'schema_version':'story-abc-decoded-coverage/v2','video':before,'plan':plan_bound,
        'templates':templates,'video_box':video_box,'presenter':presenter,'samples':samples,'critical_errors':errors,'passed':not errors,
        'plan_compliance': plan_compliance, 'execution_compliance': execution_compliance,
        'frame_derivation': {'receipt': templates['derivation_receipt'], 'mother_asset': derivation['mother_asset']},
        'scope':'plan safety plus decoded frame execution; presenter composition requires merged independent review',
        'independent_visual_review':'required_separately'}
    write(output,report)
    return report


def validate_coverage_report(path, video):
    from story_production_v2 import current
    report = json.loads(Path(path).read_text())
    if report.get('schema_version') != 'story-abc-decoded-coverage/v2' or report.get('passed') is not True or report.get('critical_errors'):
        raise ValueError('ABC decoded coverage missing or failed')
    if current(report['video']).resolve() != Path(video).resolve(): raise ValueError('ABC coverage binds another video')
    plan_path = current(report['plan'])
    plan = validate_plan(plan_path)
    for item in report['templates'].values(): current(item)
    from story_release_policy import validate_frame_derivation
    validate_frame_derivation(current(report['frame_derivation']['receipt']))
    if report.get('presenter'): current(report['presenter']['source'])
    from release_video import parse_b_windows
    duration = json.loads(current(plan['inputs']['authoritative_timeline_receipt']).read_text())['audio_duration_seconds']
    expected = plan['review_samples']
    if len(report['samples']) != len(expected): raise ValueError('ABC coverage sample set incomplete')
    for actual, wanted in zip(report['samples'], expected):
        current(actual['frame'])
        if any(actual.get(k) != v for k,v in wanted.items()) or actual['observed_mode'] != wanted['expected_mode']:
            raise ValueError('ABC coverage sample mismatch')
    return report


def main():
    parser=argparse.ArgumentParser(description='Prepare mature auto ABC plan for the existing merged release preview review')
    parser.add_argument('--run-file',required=True,type=Path); parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--presenter-foreground',required=True,type=Path)
    parser.add_argument('--fixed-anchor-x',required=True,type=int)
    parser.add_argument('--fixed-anchor-y',default=0,type=int)
    parser.add_argument('--canvas-width',default=1920,type=int)
    parser.add_argument('--source-width',default=1920,type=int)
    parser.add_argument('--source-height',default=1080,type=int)
    parser.add_argument('--rendered-height',default=1080,type=int)
    parser.add_argument('--person-layout-policy',default='source-native-fixed-anchor/v2')
    parser.add_argument('--b-windows')
    parser.add_argument('--c-windows')
    args=parser.parse_args(); p=prepare_plan(
        args.run_file,args.output,presenter_foreground=args.presenter_foreground,
        fixed_anchor_x=args.fixed_anchor_x,fixed_anchor_y=args.fixed_anchor_y,
        canvas_width=args.canvas_width,source_width=args.source_width,
        source_height=args.source_height,rendered_height=args.rendered_height,
        person_layout_policy=args.person_layout_policy,
        b_windows=args.b_windows,c_windows=args.c_windows,
    )
    print(json.dumps({'plan':str(args.output.resolve()),'b_windows':p['b_windows'],'c_windows':p['c_windows'], 'sample_count':len(p['review_samples'])},ensure_ascii=False))

if __name__ == '__main__': main()
