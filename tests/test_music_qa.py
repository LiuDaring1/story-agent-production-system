from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from story_project import init_project, qa_music


def write_tone(path: Path, duration: float, amplitude: float = 0.25, frequency: float = 220.0) -> None:
    sample_rate = 16000
    frames = bytearray()
    for index in range(round(duration * sample_rate)):
        value = int(32767 * amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames.extend(struct.pack("<h", value))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(frames)


class MusicQaTests(unittest.TestCase):
    def test_music_qa_passes_covered_non_clipping_audio_and_hashes_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：配乐测试"
            init_project(project, story_name="配乐测试", slug="music-qa")
            music = root / "music.wav"
            narration = root / "narration.wav"
            plan = root / "plan.csv"
            write_tone(music, 3.0)
            write_tone(narration, 2.0, amplitude=0.15, frequency=330)
            plan.write_text("segment,duration_sec\n1,1.5\n2,1.5\n", encoding="utf-8-sig")

            report = qa_music(project, music, narration, plan)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["metrics"]["segment_count"], 2)
            self.assertIn("sha256", payload["artifacts"]["music"])

    def test_music_qa_rejects_audio_shorter_than_narration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：短配乐"
            init_project(project, story_name="短配乐", slug="short-music")
            music = root / "music.wav"
            narration = root / "narration.wav"
            write_tone(music, 1.0)
            write_tone(narration, 3.0)

            payload = json.loads(qa_music(project, music, narration).read_text(encoding="utf-8"))
            self.assertFalse(payload["passed"])
            self.assertIn("coverage_short", {item["code"] for item in payload["issues"]})


if __name__ == "__main__":
    unittest.main()
