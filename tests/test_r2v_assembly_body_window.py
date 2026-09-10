import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assemble_r2v_story import expand_body_assembly_segments, sha256_path


class BodyWindowAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'audio.mp3'; self.audio.write_bytes(b'current audio')
        self.title = self.root / 'title.mp4'; self.title.write_bytes(b'reviewed title')
        self.moral = self.root / 'moral.mp4'; self.moral.write_bytes(b'reviewed moral')
        self.timeline = {'authoritative_audio': self.bind(self.audio), 'audio_duration_seconds': 10.000156}
        self.timeline_path = self.write('timeline.json', self.timeline)
        self.request = {'cards': [{'card_kind': 'title_card', 'presentation_window_seconds': 1},
                                  {'card_kind': 'moral_card', 'presentation_window_seconds': 2, 'text': '寓意'}]}
        self.request_path = self.write('semantic_card_motion_request.json', self.request)
        self.receipt = {'request_sha256': sha256_path(self.request_path), 'cards': [
            {'card_kind': k, 'output_video_path': str(p), 'output_video_sha256': sha256_path(p)}
            for k, p in [('title_card', self.title), ('moral_card', self.moral)]]}
        self.receipt_path = self.write('motion.json', self.receipt)
        self.ledger = {'inputs': {'audio': self.bind(self.audio)}, 'artifacts': {
            'authoritative_timeline_receipt': self.bind(self.timeline_path),
            'semantic_card_motion_receipt': self.bind(self.receipt_path)}}
        self.plan = {'source_audio': {**self.bind(self.audio), 'duration_seconds': 10.000156},
                     'body_audio_window': {'source_start': 1, 'source_end': 8.000156,
                                          'authoritative_timeline_receipt': self.bind(self.timeline_path)},
                     'shots': [{'shot_id': 'S01', 'source_start': 1, 'source_end': 4},
                               {'shot_id': 'S02', 'source_start': 4, 'source_end': 8.000156}]}
        # These production validators have independent tests; keep this fixture focused
        # on complement integration, while exercising real hash/path/video bindings.
        self.validator = patch('story_timeline.validate_authoritative_timeline_receipt', return_value=self.timeline).start()
        patcher = patch('semantic_card_motion.semantic_card_motion_receipt_issues', return_value=[])
        patcher.start(); self.addCleanup(patch.stopall)

    def bind(self, path): return {'path': str(path), 'sha256': sha256_path(path)}
    def write(self, name, value):
        p = self.root / name; p.write_text(json.dumps(value)); return p
    def expand(self):
        return expand_body_assembly_segments(self.plan, ledger=self.ledger, audio_path=self.audio,
                                            audio_duration=10.000156, title_video=self.title, moral_video=self.moral)

    def test_complements_use_exact_audio_end_and_do_not_mutate_reviewed_plan(self):
        original = copy.deepcopy(self.plan)
        shots, evidence = self.expand()
        self.assertEqual(self.plan, original)
        self.assertEqual([s['shot_id'] for s in shots], ['S01', 'S02', 'MORAL'])
        self.assertEqual(shots[-1]['source_start'], 8.000156)
        self.assertEqual(shots[-1]['source_end'], 10.000156)
        self.assertEqual(evidence['title_window'], [0, 1])
        self.validator.assert_called_once_with(self.timeline_path.resolve(), expected_inputs=self.ledger['inputs'])

    def test_missing_or_incorrect_presentation_window_rejected(self):
        for value in (None, 1.98):
            with self.subTest(value=value):
                self.request['cards'][1]['presentation_window_seconds'] = value
                self.write(self.request_path.name, self.request)
                self.receipt['request_sha256'] = sha256_path(self.request_path)
                self.write(self.receipt_path.name, self.receipt)
                self.ledger['artifacts']['semantic_card_motion_receipt'] = self.bind(self.receipt_path)
                with self.assertRaisesRegex(ValueError, 'presentation_window'):
                    self.expand()

    def test_body_gap_overlap_and_duplicate_moral_rejected(self):
        original = copy.deepcopy(self.plan)
        for delta in (-0.01, 0.01):
            self.plan = copy.deepcopy(original); self.plan['shots'][1]['source_start'] += delta
            with self.assertRaisesRegex(ValueError, '连续精确'):
                self.expand()
        self.plan = copy.deepcopy(original); self.plan['shots'][1]['shot_id'] = 'MORAL'
        with self.assertRaisesRegex(ValueError, '语义卡不能重复'):
            self.expand()

    def test_stale_timeline_and_wrong_audio_rejected(self):
        self.plan['body_audio_window']['authoritative_timeline_receipt']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, '当前权威时间轴'):
            self.expand()
        self.plan['body_audio_window']['authoritative_timeline_receipt'] = self.bind(self.timeline_path)
        self.plan['source_audio']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'plan 音频'):
            self.expand()

    def test_missing_moral_video_and_replaced_reviewed_video_rejected(self):
        saved = self.moral; self.moral = None
        with self.assertRaisesRegex(ValueError, '--moral-video'):
            self.expand()
        self.moral = saved; saved.write_bytes(b'unreviewed replacement')
        with self.assertRaisesRegex(ValueError, '--moral-video'):
            self.expand()

    def test_diagnostic_requires_evidence_and_uses_existing_encoder_for_all_segments(self):
        from assemble_r2v_story import main
        plan_path = self.write('plan.json', self.plan)
        before = plan_path.read_bytes()
        for shot in self.plan['shots']:
            (self.root / f"{shot['shot_id']}.mp4").write_bytes(b'body source')
        argv = ['assemble_r2v_story.py', '--plan', str(plan_path), '--videos-dir', str(self.root),
                '--title-video', str(self.title), '--moral-video', str(self.moral),
                '--audio', str(self.audio), '--output', str(self.root / 'master.mp4'),
                '--decisions', str(self.root / 'decisions.json'), '--clips-dir', str(self.root / 'clips'),
                '--ppt-plan', str(self.root / 'ppt.json'), '--diagnostic-preview']
        with patch('sys.argv', argv), patch('assemble_r2v_story.duration', return_value=10.000156), \
                patch('assemble_r2v_story.encode_segment') as encoder:
            with self.assertRaisesRegex(ValueError, '--run-file'):
                main()
            encoder.assert_not_called()
        def encode(**kw):
            kw['output'].write_bytes(b'encoded segment')
            return {'timing_strategy': 'uniform_slowdown', 'encoded_frame_count': kw['target_frames']}
        def mux(command): Path(command[-1]).write_bytes(b'muxed')
        with patch('sys.argv', argv + ['--run-file', str(self.root / 'run.json')]), \
                patch('story_run.load_run', return_value=self.ledger), \
                patch('assemble_r2v_story.duration', return_value=10.000156), \
                patch('assemble_r2v_story.encode_segment', side_effect=encode) as encoder, \
                patch('assemble_r2v_story.run', side_effect=mux):
            self.assertEqual(main(), 0)
        self.assertEqual(encoder.call_count, 4)
        self.assertEqual(sum(c.kwargs['target_frames'] for c in encoder.call_args_list), 300)
        self.assertEqual(encoder.call_args_list[-1].kwargs['source'], self.moral)
        self.assertEqual(plan_path.read_bytes(), before)
        decisions = json.loads((self.root / 'decisions.json').read_text())
        self.assertEqual([s['segment_id'] for s in decisions['segments']], ['TITLE', 'S01', 'S02', 'MORAL'])
        self.assertEqual(decisions['qualification'], 'diagnostic_preview_not_deliverable')

    def test_missing_window_request_and_unbound_request_rejected(self):
        self.request_path.unlink()
        with self.assertRaisesRegex(ValueError, '明确语义卡窗口'):
            self.expand()
        self.request['extra'] = True; self.write(self.request_path.name, self.request)
        with self.assertRaisesRegex(ValueError, '请求哈希'):
            self.expand()


if __name__ == '__main__':
    unittest.main()
