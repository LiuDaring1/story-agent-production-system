"""Deterministic candidate operations dispatched by story_pipeline.py."""
from pathlib import Path
import argparse
import json
from story_production_v2 import VERSION, binding, current, write

def load_candidate(path):
    from story_run import load_run
    run = load_run(path)
    if run.get('production_contract') != VERSION:
        raise ValueError('This operation only accepts an explicit v2 run; no migration')
    return run

def render_media(run, request, *, preview=False):
    from product_package import load_keying_preset, render_demo_video, render_a_only_background_video, probe_video_size, validate_formal_demo_logo
    from keying_quality import keying_preset_lock_issues
    from release_geometry import compile_demo_presenter_geometry
    from keying_quality import production_keying_fingerprint
    from story_timeline import validate_authoritative_timeline_receipt
    from story_customer_media import write_customer_media_receipt
    inputs = run['inputs']
    root = Path(run['project_dir'])
    required = ('preset', 'background_image', 'story_frame', 'logo', 'background_with_subtitles', 'background_without_subtitles', 'body_srt', 'timeline_receipt')
    paths = {k: current(request['inputs'][k]) for k in required}
    timeline = validate_authoritative_timeline_receipt(paths['timeline_receipt'], expected_inputs=inputs)
    for role in ('greenscreen_video', 'audio', 'finished_music', 'subtitle_srt', 'subtitle_txt'):
        current(inputs[role])
    from release_video import parse_srt
    from story_timeline import _load_timing_rows
    rows = _load_timing_rows(Path(timeline['timings']['path']))
    cues = parse_srt(Path(inputs['subtitle_srt']['path']))
    if len(cues) != len(rows) or any((t != r['line'] or abs(a - r['source_start']) > 0.05 or abs(b - r['source_end']) > 0.05 for (a, b, t), r in zip(cues, rows))):
        raise ValueError('Full SRT differs from authoritative timeline')
    issues = keying_preset_lock_issues(paths['preset'])
    if issues:
        raise ValueError('Keying lock invalid: ' + '; '.join(issues))
    lock = json.loads(paths['preset'].with_name('keying_preset.lock.json').read_text())
    if Path(lock['source_video']).resolve() != Path(inputs['greenscreen_video']['path']).resolve() or lock['source_sha256'] != inputs['greenscreen_video']['sha256']:
        raise ValueError('Keying lock belongs to a different source input')
    preset = load_keying_preset(paths['preset'])
    if preset.keyer != 'rvm':
        raise ValueError('Formal media requires full RVM alpha')
    foreground = Path(preset.rvm_foreground_video)
    sw, sh = probe_video_size(foreground)
    validate_formal_demo_logo(paths['logo'], formal=True)
    geometry = compile_demo_presenter_geometry(sw, sh, 1920, 1080, person_crop=preset.person_crop, detected_bbox=preset.detected_person_bbox, person_height_ratio=preset.person_height_ratio, crop_mode='source-native', crop_bottom_ratio=0, vertical_alignment='center', keying_preset_sha256=binding(paths['preset'])['sha256'], keying_lock_sha256=binding(paths['preset'].with_name('keying_preset.lock.json'))['sha256'], source_greenscreen_sha256=binding(foreground)['sha256'], production_keying_filter_fingerprint=production_keying_fingerprint(preset))
    from story_video_synthesizer.media import probe_duration
    duration = probe_duration(Path(inputs['audio']['path']))
    from story_requirements import validate_projection
    projection = current(request['requirements_projection'])
    validate_projection(projection, registered_inputs=inputs, registered_artifacts=run['artifacts'])
    if preview:
        from product_package import render_demo_preview_frames
        frames = render_demo_preview_frames(person_video=foreground, background_image=paths['background_image'], subtitles=Path(inputs['subtitle_srt']['path']), output_dir=root / '99_项目状态' / 'media_preview', preset=preset, width=1920, height=1080, crop_bottom_ratio=0, crop_mode='source-native', vertical_align='center', times=[0, duration * 0.25, duration * 0.5, duration * 0.75, max(0, duration - 0.1)], logo_path=paths['logo'], logo_width=150, logo_x=24, logo_y=20, presenter_geometry=geometry)
        payload = {'schema_version': 'story-media-preview/v2', 'producer_context': request['producer_context'], 'project': str(root.resolve()), 'presenter_geometry': geometry, 'inputs': {**request['inputs'], 'foreground': binding(foreground), 'requirements_projection': binding(projection)}, 'run_inputs': {k: inputs[k] for k in ('greenscreen_video', 'audio', 'finished_music', 'subtitle_srt', 'subtitle_txt')}, 'artifacts': [binding(p) for p in frames]}
        target = root / '99_项目状态' / 'media_preview.json'
        write(target, payload)
        return payload
    from story_media_preview import load_approved, validate_preview
    approved, approved_geometry = load_approved(current(request['approved_demo']), root)
    previous = validate_preview(approved['preview']['path'], root)
    if previous['inputs'] != {**request['inputs'], 'foreground': binding(foreground), 'requirements_projection': binding(projection)} or previous['run_inputs'] != {k: inputs[k] for k in ('greenscreen_video', 'audio', 'finished_music', 'subtitle_srt', 'subtitle_txt')} or geometry != approved_geometry:
        raise ValueError('Formal media inputs/geometry differ from reviewed preview')
    work = root / '03_产品素材' / 'media'
    work.mkdir(parents=True, exist_ok=True)
    demo = work / 'demo.mp4'
    aonly = work / 'a_only.mp4'
    render_demo_video(person_video=foreground, background_image=paths['background_image'], narration=Path(inputs['audio']['path']), music=Path(inputs['finished_music']['path']), subtitles=Path(inputs['subtitle_srt']['path']), output_path=demo, preset=preset, width=1920, height=1080, crf=20, x264_preset='medium', music_volume=0.22, narration_volume=1.0, crop_bottom_ratio=0, crop_mode='source-native', logo_path=paths['logo'], presenter_geometry=geometry)
    render_a_only_background_video(bg_video=paths['background_without_subtitles'], background_image=paths['background_image'], frame_image=paths['story_frame'], output_path=aonly, keying_preset=paths['preset'], width=1920, height=1080, crf=20, x264_preset='medium')
    receipt = root / '99_项目状态' / 'customer_media_receipt.json'
    write_customer_media_receipt(output_path=receipt, music=Path(inputs['finished_music']['path']), authoritative_timeline_receipt=paths['timeline_receipt'], subtitle_srt=paths['body_srt'], background_with_subtitles=paths['background_with_subtitles'], background_without_subtitles=paths['background_without_subtitles'], a_only_video=aonly, demo_video=demo)
    return {'demo': binding(demo), 'a_only_video': binding(aonly), 'customer_media_receipt': binding(receipt), 'presenter_geometry': geometry}

from story_render_task import render_entry

@render_entry
def main(argv):
    p = argparse.ArgumentParser(description='Candidate deterministic media, packaging and materials operations')
    p.add_argument('operation', choices=['media', 'media-preview', 'media-approve', 'pack', 'materials', 'packaging', 'checklist', 'encode-control'])
    p.add_argument('--run-file', type=Path, required=True)
    p.add_argument('--request', type=Path, required=True, help='Explicit paths and operation parameters JSON')
    args = p.parse_args(argv)
    run = load_candidate(args.run_file)
    r = json.loads(args.request.read_text())
    from story_production_v2 import protect_outputs
    protected=[args.request,args.run_file,*(v['path'] for v in run['inputs'].values())]
    destinations=[r[k] for k in ('output','receipt','output_root') if k in r]
    if args.operation in {'media','media-preview'}:
        destinations.extend([Path(run['project_dir'])/'03_产品素材'/'media', Path(run['project_dir'])/'99_项目状态'/'media_preview', Path(run['project_dir'])/'99_项目状态'/'media_preview.json',Path(run['project_dir'])/'99_项目状态'/'customer_media_receipt.json'])
    protect_outputs(destinations,protected)
    from story_render_task import bind_render_task
    bind_render_task(args.run_file, outputs=destinations)
    if args.operation == 'encode-control':
        from story_encode import control_encode
        if Path(run['project_dir']).resolve() not in Path(r['output']).resolve().parents:
            raise ValueError('Encode output belongs to another project')
        result = control_encode(**r, expected_task=run['run_id'])
    elif args.operation == 'media-approve':
        from story_media_preview import approve_preview
        result = approve_preview(**r, project=run['project_dir'])
    elif args.operation in {'media', 'media-preview'}:
        result = render_media(run, r, preview=args.operation == 'media-preview')
    elif args.operation == 'pack':
        from story_managed_package import package
        result = package(**r, inputs=run['inputs'])
    elif args.operation == 'materials':
        from story_materials import export_materials
        result = export_materials(**r, inputs=run['inputs'])
    elif args.operation == 'packaging':
        from story_materials import bind_packaging
        result = bind_packaging(**r, inputs=run['inputs'])
    else:
        from story_production_v2 import validate_managed_receipt, validate_checklist
        pack = validate_managed_receipt(r['managed_package_receipt'], run['inputs'])
        result = {'production_contract': VERSION, 'status': 'complete_pending_independent_final_review', 'missing': [], 'artifacts': [*pack['artifacts'], *({'role': role, **binding(r[role])} for role in ('main_release_video', 'library_release_video'))]}
        write(r['output'], result)
        validate_checklist(r['output'])
    print(json.dumps(result, ensure_ascii=False, indent=2))
