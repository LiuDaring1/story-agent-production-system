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


def prepare_plan(run_file, output):
    from story_run import load_run
    from story_production_v2 import current, binding, write
    from story_timeline import validate_authoritative_timeline_receipt
    from story_workflow import build_abc_scene_windows
    from release_video import parse_b_windows
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
    b, c = build_abc_scene_windows(duration, srt)
    bw, cw = parse_b_windows(b), parse_b_windows(c)
    spans = segments(duration, bw, cw)
    if duration >= 30 and (not bw or not cw or not any(x[2] == 'a' for x in spans)):
        raise ValueError('mature auto plan cannot supply all A/B/C at this duration; explicit reviewed plan required')
    rules = [{**binding(Path(__file__).parent/relative), 'source_relative_path':relative} for relative in
             ['story_scene_windows.py','story_workflow.py','skills/story-full-auto/references/video-invariants.md']]
    payload = {'schema_version': 'story-project-release-windows-plan/v1', 'preparation': 'mature_auto',
        'inputs': {'subtitle_srt': binding(srt), 'authoritative_timeline_receipt': binding(timeline)},
        'rules': rules, 'duration_seconds': duration, 'b_windows': b, 'c_windows': c,
        'segments': [{'start': s, 'end': e, 'mode': m} for s,e,m in spans],
        'review_samples': coverage_samples(duration,bw,cw), 'independent_review': 'merge_with_release_preview'}
    if output.exists():
        existing = json.loads(output.read_text())
        def identity(value):
            return {**value, 'rules':[{k:v for k,v in r.items() if k != 'path'} for r in value['rules']]}
        if identity(existing) != identity(payload):
            raise ValueError('existing ABC plan differs; use a new output path for changed inputs/rules')
        for rule in existing['rules']: current_rule(rule)
        return existing
    else: write(output, payload)
    return payload


def frame_templates(config, directory):
    from release_video import prepare_story_frame_assets
    from story_production_v2 import binding
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    result = {}
    for mode, source, box in [('a',config.frame_image, config.story_box), ('b',config.frame_image_b or config.frame_image, config.b_story_box)]:
        path, _, _ = prepare_story_frame_assets(source, box, directory/f'frame_{mode}.png', directory/f'mask_{mode}.png',config.story_bleed)
        result[mode] = binding(path)
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
    for mode, item in templates.items():
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
    payload = json.loads(plan.read_text())
    for item in payload['inputs'].values(): current(item)
    for item in payload['rules']: current_rule(item)
    duration = json.loads(current(payload['inputs']['authoritative_timeline_receipt']).read_text())['audio_duration_seconds']
    bw, cw = parse_b_windows(payload['b_windows']), parse_b_windows(payload['c_windows'])
    spans = segments(duration,bw,cw)
    if duration >= 30 and {x[2] for x in spans} != {'a','b','c'}: raise ValueError('main ABC plan lacks required A/B/C')
    from release_video import validate_main_scene_ending
    validate_main_scene_ending(spans,duration,current(payload['inputs']['subtitle_srt']))
    samples = coverage_samples(duration,bw,cw)
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
    report = {'schema_version':'story-abc-decoded-coverage/v1','video':before,'plan':plan_bound,
        'templates':templates,'video_box':video_box,'presenter':presenter,'samples':samples,'critical_errors':errors,'passed':not errors,
        'scope':'frame geometry presence; presenter and composition require merged independent review',
        'independent_visual_review':'required_separately'}
    write(output,report)
    return report


def validate_coverage_report(path, video):
    from story_production_v2 import current
    report = json.loads(Path(path).read_text())
    if report.get('schema_version') != 'story-abc-decoded-coverage/v1' or report.get('passed') is not True or report.get('critical_errors'):
        raise ValueError('ABC decoded coverage missing or failed')
    if current(report['video']).resolve() != Path(video).resolve(): raise ValueError('ABC coverage binds another video')
    plan = json.loads(current(report['plan']).read_text())
    for item in [*plan['inputs'].values(), *report['templates'].values()]: current(item)
    for rule in plan['rules']: current_rule(rule)
    if report.get('presenter'): current(report['presenter']['source'])
    from release_video import parse_b_windows
    duration = json.loads(current(plan['inputs']['authoritative_timeline_receipt']).read_text())['audio_duration_seconds']
    expected = coverage_samples(duration, parse_b_windows(plan['b_windows']), parse_b_windows(plan['c_windows']))
    if len(report['samples']) != len(expected): raise ValueError('ABC coverage sample set incomplete')
    for actual, wanted in zip(report['samples'], expected):
        current(actual['frame'])
        if any(actual.get(k) != v for k,v in wanted.items()) or actual['observed_mode'] != wanted['expected_mode']:
            raise ValueError('ABC coverage sample mismatch')
    return report


def main():
    parser=argparse.ArgumentParser(description='Prepare mature auto ABC plan for the existing merged release preview review')
    parser.add_argument('--run-file',required=True,type=Path); parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(); p=prepare_plan(args.run_file,args.output)
    print(json.dumps({'plan':str(args.output.resolve()),'b_windows':p['b_windows'],'c_windows':p['c_windows'], 'sample_count':len(p['review_samples'])},ensure_ascii=False))

if __name__ == '__main__': main()
