"""Deterministic final release QA, formerly a project-local adapter.

Machine evidence only: this does not grant independent visual approval.
"""
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
from story_production_v2 import binding, current, write, protect_outputs


def audit_releases(*, inputs, releases, output, evidence_dir, video_boxes=None, demo=None, logo=None, gesture_times=None, abc_coverage=None):
    from story_customer_media import decode_audio, narration_music_fit, RELEASE_AUDIO_DURATION_TOLERANCE_SECONDS
    from story_project import probe_av_alignment
    from story_video_synthesizer.media import probe_duration
    from release_video import (build_tail_frame_probe_commands, inspect_tail_image_black_rectangles,
                               release_plate_integrity_issues, video_window_black_edge_issues)
    if set(releases) != {'main_release_video', 'library_release_video'}:
        raise ValueError('Both release accounts are required')
    if len({str(current(item).resolve()) for item in releases.values()}) != 2:
        raise ValueError('Release accounts require distinct files')
    protect_outputs([output, evidence_dir], [item['path'] for item in releases.values()] + [inputs[k]['path'] for k in ('audio', 'finished_music')] + [item['path'] for item in (demo, logo) if item is not None])
    started = time.monotonic()
    voice, music = current(inputs['audio']), current(inputs['finished_music'])
    v, m = decode_audio(voice), decode_audio(music)
    duration = probe_duration(voice)
    root = Path(evidence_dir) / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    root.mkdir(parents=True, exist_ok=False)
    artifacts, results, issues = {}, [], []
    for role, source in releases.items():
        path = current(source)
        bound = binding(path)
        alignment = probe_av_alignment(path)
        notes = list(alignment['issues'])
        actual = probe_duration(path)
        if abs(actual-duration) > RELEASE_AUDIO_DURATION_TOLERANCE_SECONDS:
            notes.append('duration does not cover authoritative audio')
        fit = narration_music_fit(decode_audio(path), v, m)
        if not fit['passed']:
            notes.append('narration/music fit failed')
        w, h = int(alignment.get('width') or 0), int(alignment.get('height') or 0)
        if not h or abs(w/h-.75) > .01:
            notes.append('not 3:4')
        decoded = subprocess.run(['ffmpeg','-v','error','-i',str(path),'-f','null','-'], capture_output=True, text=True)
        log = root / f'{role}_decode.log'; log.write_text(decoded.stderr)
        if decoded.returncode or decoded.stderr.strip():
            notes.append('full decode errors; inspect log')
        out = root / role; out.mkdir()
        abc_result = None
        if role == 'main_release_video':
            from story_scene_windows import validate_coverage_report, audit_video
            coverage_path = current(abc_coverage) if abc_coverage else path.parent/'main_abc_coverage.json'
            try:
                coverage = validate_coverage_report(coverage_path, path)
                abc_result = audit_video(path, current(coverage['plan']), coverage['templates'], out/'abc_coverage.json', video_box=coverage.get('video_box'), presenter=coverage.get('presenter'))
                notes.extend(abc_result['critical_errors'])
            except (OSError, ValueError, KeyError) as exc:
                notes.append('ABC decoded coverage invalid: '+str(exc))

        for command in build_tail_frame_probe_commands(path, out, actual, fps=12, window_seconds=min(2., actual)):
            subprocess.run(command, check=True, capture_output=True)
        tails = sorted(out.glob('tail_*.jpg'))
        if len(tails) < 2:
            notes.append('insufficient tail frames')
        for frame in tails:
            notes.extend(f'{frame.name}: {e}' for e in inspect_tail_image_black_rectangles(frame))
        # Explicit per-account geometry overrides the established default layout.
        box = tuple((video_boxes or {}).get(role, (0, round(h*416/1440), w, round(h*608/1440))))
        samples = []
        times = sorted(set(max(0., min(actual-.001, t)) for t in (0,.04,1,2,actual*.25,actual*.5,actual*.75,actual-.1)))
        for i, timestamp in enumerate(times):
            frame = out / f'formal_{i:02d}.jpg'
            subprocess.run(['ffmpeg','-v','error','-y','-ss',str(timestamp),'-i',str(path),'-frames:v','1','-q:v','2',str(frame)], check=True, capture_output=True)
            samples.append({**binding(frame), 'time_seconds': timestamp})
            notes.extend(f'{frame.name}: {e}' for e in release_plate_integrity_issues(frame, box)+video_window_black_edge_issues(frame, box, issue_prefix='release_center_video'))
        # Never bind a file that changed during the audit.
        if binding(path) != bound:
            notes.append('release changed during QA')
        notes = list(dict.fromkeys(notes))
        artifacts[role] = bound
        issues.extend(f'{role}: {note}' for note in notes)
        results.append({'label': role, 'path': str(path), 'duration_sec': actual, **alignment,
                        'audio_role': 'narration_plus_music', 'audio_role_fit': fit, 'issues': notes,
                        'tail_probe_dir': str(out), 'tail_probe_frame_count': len(tails),
                        'formal_frame_evidence': samples, 'video_box': box, 'abc_decoded_coverage': abc_result,
                        'full_decode': {'returncode': decoded.returncode, 'stderr_log': binding(log)}})
    current(inputs['audio']); current(inputs['finished_music'])
    report = {'schema_version':'story-release-machine-qa/v3', 'version':3, 'passed':not issues,
              'critical_errors':issues, 'warnings':[], 'artifacts':artifacts,
              'audio_contract':{'required_audio_role':'narration_plus_music','narration':binding(voice),'music_bed':binding(music)},
              'results':results, 'issues':issues, 'duration_seconds':time.monotonic()-started,
              'evidence_directory':str(root), 'independent_visual_review':'required_separately'}
    if demo is not None:
        source = current(demo)
        demo_duration = probe_duration(source)
        out = root / 'demo'; out.mkdir()
        timestamps = sorted(set(max(0., min(demo_duration-.001, float(t))) for t in
                            [0, .04, demo_duration*.25, demo_duration*.5, demo_duration*.75, demo_duration-.1, *(gesture_times or [])]))
        frames = []
        for i, timestamp in enumerate(timestamps):
            frame = out / f'formal_{i:02d}.jpg'
            subprocess.run(['ffmpeg','-v','error','-y','-ss',str(timestamp),'-i',str(source),'-frames:v','1','-q:v','2',str(frame)],check=True,capture_output=True)
            frames.append({**binding(frame), 'time_seconds': timestamp})
        current(demo)
        evidence = {'demo': demo, 'formal_frame_evidence': frames,
                    'authoritative_full_srt': inputs.get('subtitle_srt'), 'official_logo': logo}
        if logo is not None: current(logo)
        if inputs.get('subtitle_srt') is not None: current(inputs['subtitle_srt'])
        write(out/'demo_frame_evidence.json', evidence)
        report['demo_frame_evidence'] = binding(out/'demo_frame_evidence.json')
    report['duration_seconds'] = time.monotonic()-started
    write(root/'qa_release_report.json',report); write(output,report)
    return report
