import copy
import json
import tempfile
import unittest
import wave
from pathlib import Path

from test_story_r2v_skill import VALIDATOR, valid_v4_plan
from story_timeline import import_confirmed_user_srt, file_sha256


class BodyAudioWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        audio = self.root/'audio.wav'
        with wave.open(str(audio), 'wb') as out:
            out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            out.writeframes(b'\0\0' * 160000)
        txt, srt = self.root/'text.txt', self.root/'text.srt'
        txt.write_text('Title\nBody\nMoral\n')
        srt.write_text('1\n00:00:00,000 --> 00:00:04,000\nTitle\n\n2\n00:00:05,000 --> 00:00:14,000\nBody\n\n3\n00:00:16,000 --> 00:00:19,000\nMoral\n')
        receipt = self.root/'receipt.json'
        import_confirmed_user_srt(receipt_path=receipt, timings_path=self.root/'timings.json', subtitle_txt=txt, subtitle_srt=srt, authoritative_audio=audio)
        self.plan = valid_v4_plan()
        self.plan['source_audio'] = dict(path=str(audio), sha256=file_sha256(audio), duration_seconds=20.0)
        self.plan['body_audio_window'] = dict(source_start=5.0, source_end=15.0, authoritative_timeline_receipt=dict(path=str(receipt), sha256=file_sha256(receipt)))
        self.plan['shots'][0].update(source_start=5.0, source_end=15.0)

    def errors(self):
        return VALIDATOR.validate_plan(self.plan, require_current_schema=True)

    def test_accepts_absolute_body_with_trailing_pause_and_complete_audio(self):
        self.assertEqual(self.errors(), [])
        schema = Path(VALIDATOR.__file__).parents[1]/'references/story_r2v_plan.schema.json'
        window_schema = json.loads(schema.read_text())["properties"]["body_audio_window"]
        self.assertEqual(set(window_schema["required"]), set(self.plan["body_audio_window"]))

    def test_legacy_no_window_still_requires_complete_audio_end(self):
        del self.plan['body_audio_window']
        self.assertTrue(any('authoritative audio duration' in e for e in self.errors()))

    def test_hash_drift_rejected(self):
        self.plan['body_audio_window']['authoritative_timeline_receipt']['sha256'] = '0'*64
        self.assertTrue(any('hash drift' in e for e in self.errors()))

    def test_wrong_audio_binding_rejected(self):
        self.plan['source_audio']['sha256'] = '0'*64
        self.assertTrue(any("complete authoritative audio" in e for e in self.errors()))

    def test_duration_cannot_be_replaced_with_body_duration(self):
        self.plan['source_audio']['duration_seconds'] = 15.0
        self.assertTrue(any('complete authoritative audio duration' in e for e in self.errors()))

    def test_window_cannot_cut_cue(self):
        self.plan['body_audio_window']['source_start'] = 2.0
        self.assertTrue(any('cuts an authoritative subtitle cue' in e for e in self.errors()))

    def test_first_and_last_must_match_window(self):
        self.plan['shots'][0].update(source_start=5.02, source_end=14.98)
        self.assertTrue(any('first source_start' in e for e in self.errors()))
        self.assertTrue(any('final source_end' in e for e in self.errors()))

    def test_tiny_gap_requires_continuous_coverage(self):
        second = copy.deepcopy(self.plan['shots'][0])
        self.plan['shots'][0]['source_end'] = 10.0
        second.update(shot_id='second', source_start=10.005, source_end=15.0)
        self.plan['shots'].append(second)
        self.assertTrue(any('continuous shot coverage' in e for e in self.errors()))

    def test_offscreen_crowd_is_included_in_focus_contract(self):
        shot = self.plan['shots'][0]
        crowd = copy.deepcopy(shot['subject_presence'][1])
        crowd.update(subject_id='followers', subject_type='crowd', entry_presence='off_screen', exit_presence='off_screen', entry_zone_id=None, exit_zone_id=None)
        shot['subject_presence'].append(crowd)
        shot['focus_contract']['off_screen_subject_ids'].append('followers')
        self.assertEqual(self.errors(), [])
        shot['focus_contract']['off_screen_subject_ids'].remove('followers')
        self.assertTrue(any('must match subjects off-screen' in e for e in self.errors()))

if __name__ == '__main__':
    unittest.main()
