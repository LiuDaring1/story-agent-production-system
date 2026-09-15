import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from presenter_layout import scan_rvm_body_overflow
from story_production_v2 import binding
from story_release_policy import (
    FRAME_REQUIREMENT_ID,
    PRESENTER_REQUIREMENT_ID,
    RELEASE_REVIEW_EVIDENCE_SCHEMA,
    release_safety_requirements,
    validate_frame_derivation,
    validate_release_safety_projection,
)
from story_scene_windows import frame_templates, prepare_plan, validate_plan
from tests.release_safety_fixture import write_scan_report


class ReleaseSafetyPolicyTests(unittest.TestCase):
    def fixture(self, root, duration=30.0):
        srt = root / 'speech.srt'
        srt.write_text('1\n00:00:00,000 --> 00:00:29,000\ntext\n')
        timeline = root / 'timeline.json'
        timeline.write_text(json.dumps({'audio_duration_seconds': duration}))
        foreground = root / 'foreground.webm'
        foreground.write_bytes(b'current synthetic foreground')
        ledger = {
            'production_contract': 'story-production/v2',
            'project_dir': str(root),
            'inputs': {'subtitle_srt': binding(srt)},
            'artifacts': {'authoritative_timeline_receipt': binding(timeline)},
        }
        return srt, timeline, foreground, ledger

    def test_risks_merge_into_b_without_duplicating_existing_b_or_c(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _srt, _timeline, foreground, ledger = self.fixture(root)
            samples = [(round(index / 5, 3), 1.0) for index in range(150)]
            for index, (time, _visible) in enumerate(samples):
                if 5.4 <= time <= 6.0 or 12.0 <= time <= 12.8 or 20.4 <= time <= 21.0:
                    samples[index] = (time, 0.45)
            calls = []
            def scan(source, **kwargs):
                calls.append(str(kwargs['report_path']))
                report_path = kwargs.pop('report_path')
                return write_scan_report(source, report_path, samples=samples, duration=30., **kwargs)
            plan_path = root / '99_项目状态' / 'explicit.json'
            with patch('story_run.load_run', return_value=ledger), \
                 patch('story_timeline.validate_authoritative_timeline_receipt'), \
                 patch('presenter_layout.scan_rvm_body_overflow', side_effect=scan):
                plan = prepare_plan(
                    root / 'run.json', plan_path,
                    presenter_foreground=foreground, fixed_anchor_x=0,
                    b_windows='5-7,24-26', c_windows='20-22',
                )
                same = prepare_plan(
                    root / 'run.json', plan_path,
                    presenter_foreground=foreground, fixed_anchor_x=0,
                    b_windows='5-7,24-26', c_windows='20-22',
                )
            self.assertEqual(plan, same)
            self.assertEqual(len(set(calls)), 1)
            self.assertIn('11.750-13.250', plan['b_windows'])
            self.assertEqual(plan['c_windows'], '20.000-22.000')
            self.assertTrue(all(not row['uncovered'] for row in plan['presenter_protection']['coverage']))
            self.assertTrue(any('presenter_risk_midpoint' in row['reasons'] for row in plan['review_samples']))
            validate_plan(plan_path, ledger=ledger)

    def test_missing_or_stale_scan_and_changed_input_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _srt, _timeline, foreground, ledger = self.fixture(root)
            def scan(source, **kwargs):
                report_path = kwargs.pop('report_path')
                return write_scan_report(source, report_path, duration=30., **kwargs)
            plan_path = root / '99_项目状态' / 'plan.json'
            with patch('story_run.load_run', return_value=ledger), \
                 patch('story_timeline.validate_authoritative_timeline_receipt'), \
                 patch('presenter_layout.scan_rvm_body_overflow', side_effect=scan):
                plan = prepare_plan(root / 'run.json', plan_path,
                    presenter_foreground=foreground, fixed_anchor_x=0,
                    b_windows='5-7', c_windows='20-22')
            report = Path(plan['presenter_protection']['report']['path'])
            original = report.read_bytes()
            report.unlink()
            with self.assertRaisesRegex(ValueError, 'Changed binding|missing'):
                validate_plan(plan_path)
            report.write_bytes(original)
            foreground.write_bytes(b'changed foreground')
            with self.assertRaisesRegex(ValueError, 'Changed binding'):
                validate_plan(plan_path)

    def test_cached_complete_scan_does_not_decode_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _srt, _timeline, foreground, _ledger = self.fixture(root, duration=1.0)
            report = root / 'report.json'
            expected = write_scan_report(foreground, report, duration=1.0)
            with patch('presenter_layout.subprocess.run', side_effect=AssertionError('cache must avoid ffmpeg/ffprobe')):
                actual = scan_rvm_body_overflow(
                    foreground, fixed_anchor_x=0, canvas_width=1920,
                    source_width=1920, source_height=1080, rendered_height=1080,
                    report_path=report,
                )
            self.assertEqual(actual, expected)

    def test_frame_a_b_are_deterministic_derivatives_of_one_mother(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mother = root / 'mother.png'
            image = Image.new('RGBA', (1920, 1080))
            draw = ImageDraw.Draw(image)
            draw.rectangle((180, 240, 1160, 810), outline=(230, 170, 80, 255), width=28)
            image.save(mother)
            config = SimpleNamespace(
                frame_image=mother, frame_image_b=root / 'ignored-independent-b.png',
                story_box=(210, 270, 910, 512), b_story_box=(356, 180, 1209, 680),
                story_bleed=0,
            )
            templates = frame_templates(config, root / 'derived')
            lineage = validate_frame_derivation(
                Path(templates['derivation_receipt']['path']), expected_mother=mother)
            self.assertEqual(lineage['mother_asset']['sha256'], binding(mother)['sha256'])
            self.assertNotEqual(templates['a']['sha256'], templates['b']['sha256'])
            forged = json.loads(Path(templates['derivation_receipt']['path']).read_text())
            forged['independent_b_asset'] = True
            Path(templates['derivation_receipt']['path']).write_text(json.dumps(forged))
            with self.assertRaisesRegex(ValueError, 'independent B'):
                validate_frame_derivation(Path(templates['derivation_receipt']['path']))
            other = root / 'other_mother.png'
            Image.new('RGBA', (1920,1080), (1,2,3,4)).save(other)
            forged['independent_b_asset'] = False
            forged['mother_asset'] = binding(other)
            for item in forged['derivatives'].values():
                item['source_sha256'] = forged['mother_asset']['sha256']
            Path(templates['derivation_receipt']['path']).write_text(json.dumps(forged))
            with self.assertRaisesRegex(ValueError, 'another mother'):
                validate_frame_derivation(
                    Path(templates['derivation_receipt']['path']), expected_mother=mother,
                )

    def test_applicable_projection_requires_exact_shared_policy(self):
        evidence = [
            'presenter_body_overflow_report', 'release_plan_compliance',
            'decoded_video_execution', 'shared_frame_derivation',
            'independent_visual_review',
        ]
        payload = {'requirements': release_safety_requirements(source='current rules'),
                   'acceptance_evidence': evidence}
        validate_release_safety_projection(payload)
        broken = json.loads(json.dumps(payload))
        broken['requirements'] = [row for row in broken['requirements']
                                  if row['requirement_id'] != FRAME_REQUIREMENT_ID]
        with self.assertRaisesRegex(ValueError, FRAME_REQUIREMENT_ID):
            validate_release_safety_projection(broken)
        broken = json.loads(json.dumps(payload))
        row = next(row for row in broken['requirements']
                   if row['requirement_id'] == PRESENTER_REQUIREMENT_ID)
        row['parameters']['trigger_threshold'] = .60
        with self.assertRaisesRegex(ValueError, 'parameters stale'):
            validate_release_safety_projection(broken)

    def test_review_preparation_expands_exact_once_prepared_evidence(self):
        from story_review_preparation import prepare_review
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _srt, _timeline, foreground, ledger = self.fixture(root)
            def scan(source, **kwargs):
                report_path = kwargs.pop('report_path')
                return write_scan_report(source, report_path, duration=30., **kwargs)
            plan_path = root / '99_项目状态' / 'plan.json'
            with patch('story_run.load_run', return_value=ledger), \
                 patch('story_timeline.validate_authoritative_timeline_receipt'), \
                 patch('presenter_layout.scan_rvm_body_overflow', side_effect=scan):
                plan = prepare_plan(
                    root / 'run.json', plan_path,
                    presenter_foreground=foreground, fixed_anchor_x=0,
                    b_windows='5-7', c_windows='20-22',
                )
            mother = root / 'mother.png'
            frame_image = Image.new('RGBA', (1920, 1080), (0, 0, 0, 0))
            ImageDraw.Draw(frame_image).rectangle((180,240,1160,810), outline=(230,170,80,255), width=28)
            frame_image.save(mother)
            templates = frame_templates(SimpleNamespace(
                frame_image=mother, frame_image_b=None,
                story_box=(210,270,910,512), b_story_box=(356,180,1209,680),
                story_bleed=0,
            ), root / 'derived')
            projection = root / 'projection.json'
            projection.write_text(json.dumps({
                'requirements': release_safety_requirements(source='review fixture'),
                'acceptance_evidence': [
                    'presenter_body_overflow_report', 'release_plan_compliance',
                    'decoded_video_execution', 'shared_frame_derivation',
                    'independent_visual_review',
                ],
            }))
            frames = []
            for index, row in enumerate(plan['review_samples']):
                frame = root / f'checked_{index}.png'
                Image.new('RGB', (8, 8), (index % 255, 0, 0)).save(frame)
                frames.append({
                    'time_seconds': row['time_seconds'],
                    'mode': row['expected_mode'],
                    'reasons': row['reasons'],
                    'frame': binding(frame),
                })
            evidence = root / 'release_review_evidence.json'
            evidence.write_text(json.dumps({
                'schema_version': RELEASE_REVIEW_EVIDENCE_SCHEMA,
                'applicable_requirements': binding(projection),
                'plan': binding(plan_path),
                'presenter_overflow_report': plan['presenter_protection']['report'],
                'frame_derivation': templates['derivation_receipt'],
                'prepared_frames': frames,
            }))
            packet = prepare_review(
                project_id='fixture',
                items=[{'scope':'release','path':str(plan_path),'release_evidence':str(evidence)}],
                rules=[str(Path(__file__).resolve().parents[1] / 'story_release_policy.py')],
                output=root / 'review_packet.json',
            )
            item = packet['items'][0]
            self.assertEqual(item['required_checked_frames'], frames)
            self.assertIn(str(projection.resolve()), {row['path'] for row in item['dependencies']})
            tampered = json.loads(evidence.read_text())
            tampered['prepared_frames'][0]['mode'] = 'wrong'
            evidence.write_text(json.dumps(tampered))
            with self.assertRaisesRegex(ValueError, 'mode/reasons'):
                prepare_review(
                    project_id='fixture',
                    items=[{'scope':'release','path':str(plan_path),'release_evidence':str(evidence)}],
                    rules=[str(Path(__file__).resolve().parents[1] / 'story_release_policy.py')],
                    output=root / 'tampered_packet.json',
                )


if __name__ == '__main__':
    unittest.main()
