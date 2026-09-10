import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from story_timeline import import_confirmed_user_srt, validate_authoritative_timeline_receipt, write_authoritative_timeline_receipt


class ConfirmedSrtTimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.txt = self.root / 'input.txt'
        self.srt = self.root / 'input.srt'
        self.audio = self.root / 'audio.wav'
        self.txt.write_bytes('甲，乙。\r\n丙！\r\n'.encode('utf-8-sig'))
        self.srt.write_bytes('1\r\n00:00:00,100 --> 00:00:01,000\r\n甲，乙。\r\n\r\n2\r\n00:00:01,000 --> 00:00:02,000\r\n丙！\r\n'.encode('utf-8-sig'))
        self.audio.write_bytes(b'audio')
        self.receipt = self.root / 'receipt.json'
        self.timings = self.root / 'timings.json'
        self.duration = patch('story_timeline._audio_duration', return_value=3.0)
        self.duration.start()
        self.addCleanup(self.duration.stop)

    def run_import(self):
        return import_confirmed_user_srt(receipt_path=self.receipt, timings_path=self.timings,
              subtitle_txt=self.txt, subtitle_srt=self.srt, authoritative_audio=self.audio)

    def test_verbatim_source_and_no_asr_claim(self):
        original = self.srt.read_bytes()
        self.run_import()
        payload = validate_authoritative_timeline_receipt(self.receipt)
        self.assertEqual(payload['source_kind'], 'confirmed_user_srt')
        self.assertNotIn('alignment_metadata', payload)
        self.assertEqual(Path(payload['output_srt']['path']).read_bytes(), original)
        self.assertEqual(self.srt.read_bytes(), original)

    def test_reject_text_change(self):
        self.txt.write_text('甲乙。\n丙！\n')
        with self.assertRaises(ValueError): self.run_import()

    def test_reject_overlap_order_and_invalid_clock(self):
        raw = self.srt.read_text(encoding='utf-8-sig')
        for original, altered in [('00:00:01,000 --> 00:00:02,000', '00:00:00,900 --> 00:00:02,000'), ('\n2\n', '\n3\n'), ('00:00:02,000', '00:60:02,000')]:
            with self.subTest(altered=altered):
                self.srt.write_text(raw.replace(original, altered))
                with self.assertRaises(ValueError): self.run_import()

    def test_reject_beyond_audio(self):
        with patch('story_timeline._audio_duration', return_value=1.9):
            with self.assertRaises(ValueError): self.run_import()

    def test_reject_hash_drift(self):
        self.run_import()
        self.srt.write_bytes(self.srt.read_bytes() + b'\n')
        with self.assertRaises(ValueError): validate_authoritative_timeline_receipt(self.receipt)

    def test_reject_wrong_ledger_srt(self):
        self.run_import()
        data = json.loads(self.receipt.read_text())
        inputs = {'subtitle_txt': data['subtitle_txt'], 'audio': data['authoritative_audio'], 'subtitle_srt': dict(data['subtitle_srt'], sha256='0' * 64)}
        with self.assertRaises(ValueError): validate_authoritative_timeline_receipt(self.receipt, expected_inputs=inputs)

    def test_whisper_model_remains_required(self):
        self.run_import()
        metadata = self.root / 'metadata.json'
        metadata.write_text('{"timed_char_count": 6}')
        with self.assertRaisesRegex(ValueError, '缺少模型'):
            write_authoritative_timeline_receipt(receipt_path=self.root/'asr.json', source_kind='whisper_confirmed_line_timings', timings_path=self.timings, alignment_metadata_path=metadata, subtitle_txt=self.txt, authoritative_audio=self.audio, alignment_audio=self.audio, output_srt=self.root/'asr.srt')

    def test_nonfinite_duration_evidence_rejected(self):
        self.run_import()
        data = json.loads(self.receipt.read_text())
        data['audio_duration_seconds'] = float('nan')
        self.receipt.write_text(json.dumps(data))
        with self.assertRaises(ValueError): validate_authoritative_timeline_receipt(self.receipt)

    def test_input_overwrite_rejected(self):
        with self.assertRaises(ValueError):
            import_confirmed_user_srt(receipt_path=self.srt, timings_path=self.timings, subtitle_txt=self.txt, subtitle_srt=self.srt, authoritative_audio=self.audio)

if __name__ == '__main__':
    unittest.main()
