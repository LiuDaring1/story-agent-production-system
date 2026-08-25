from __future__ import annotations

import unittest

from story_video_synthesizer.align import align_confirmed_text_to_char_times


def timed_chars(text: str) -> list[tuple[str, float, float]]:
    return [(char, index * 0.2, (index + 1) * 0.2) for index, char in enumerate(text)]


class ConfirmedTextAlignmentTests(unittest.TestCase):
    def test_asr_typos_never_replace_confirmed_text(self) -> None:
        confirmed = ["珍妮得到了一朵七色花。", "老婆婆温柔地安慰她。"]
        recognized = "珍妮得到了一朵七色发老婆婆温柔的安慰她"

        timings = align_confirmed_text_to_char_times(confirmed, timed_chars(recognized))

        self.assertEqual([item.line for item in timings], confirmed)
        self.assertGreater(timings[1].source_start, timings[0].source_start)

    def test_local_asr_omission_uses_surrounding_anchors(self) -> None:
        confirmed = ["老婆婆慢慢走过来", "她蹲下来询问珍妮", "珍妮抬头回答"]
        recognized = "老婆婆慢慢走过来珍妮抬头回答"

        timings = align_confirmed_text_to_char_times(confirmed, timed_chars(recognized))

        self.assertEqual([item.line for item in timings], confirmed)
        self.assertEqual(len(timings), 3)
        self.assertTrue(all(item.duration >= 0.15 for item in timings))

    def test_wholly_unrelated_audio_is_blocked(self) -> None:
        confirmed = ["珍妮得到了一朵七色花，然后许下愿望。"]
        recognized = "abcdefgxyz"

        with self.assertRaisesRegex(RuntimeError, "整段无法映射"):
            align_confirmed_text_to_char_times(confirmed, timed_chars(recognized))


if __name__ == "__main__":
    unittest.main()
