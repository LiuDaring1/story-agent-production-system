import json
import tempfile
import unittest
import wave
from pathlib import Path

from story_materials import bind_packaging, validate_packaging
from story_packaging_defaults import prepare_inputs
from story_production_v2 import binding
from story_timeline import import_confirmed_user_srt


class PackagingConfirmedSrtRecoveryTests(unittest.TestCase):
    def test_no_input_recovery_revalidates_confirmed_srt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / 'audio.wav'
            with wave.open(str(audio), 'wb') as stream:
                stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0' * 16000)
            txt, srt = root / 'subtitle.txt', root / 'subtitle.srt'
            txt.write_text('确认原文。\n', encoding='utf-8')
            srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n确认原文。\n', encoding='utf-8')
            brief = root / 'brief.json'
            brief.write_text(json.dumps({'story_info': {
                'story_name': '恢复测试', 'story_type': '寓言故事',
                'age_range': '8岁以上',
                'sources': {key: 'confirmed fixture' for key in
                            ('story_name', 'story_type', 'age_range')},
            }}, ensure_ascii=False), encoding='utf-8')
            paths, _ = prepare_inputs({'story_requirements': brief}, root / 'project')
            inputs = {key: binding(path) for key, path in paths.items()}
            inputs.update({key: binding(path) for key, path in
                           [('audio', audio), ('subtitle_txt', txt), ('subtitle_srt', srt)]})
            timeline = root / 'timeline.json'
            import_confirmed_user_srt(
                receipt_path=timeline, timings_path=root / 'timings.json',
                subtitle_txt=txt, subtitle_srt=srt, authoritative_audio=audio,
                expected_inputs=inputs,
            )
            receipt = root / 'packaging.json'
            bind_packaging(inputs=inputs, output=root / 'prompt.txt', receipt=receipt,
                           timeline_receipt=binding(timeline))
            recovered = validate_packaging(receipt)
            self.assertEqual(recovered['fields']['duration_text'], '2秒')
            self.assertEqual(recovered, validate_packaging(receipt, inputs))
            srt.write_bytes(srt.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, '哈希漂移'):
                validate_packaging(receipt)


if __name__ == '__main__':
    unittest.main()
