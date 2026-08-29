from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from story_video_synthesizer.pipeline import SynthesisConfig, _overlay_semantic_cards


class SemanticCardOverlayTests(unittest.TestCase):
    def test_rejects_programmatic_title_text_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            card_dir = root / "cards"
            card_dir.mkdir()
            card = card_dir / "title.png"
            Image.new("RGB", (1920, 1080), "white").save(card)
            (card_dir / "semantic_card_generation_receipt.json").write_text(json.dumps({
                "schema_version": "story-semantic-card-generation/v1",
                "imagegen_native": False,
                "post_render_text_overlay": True,
                "cards": [{
                    "card_kind": "title_card", "text": "通用故事",
                    "path": str(card), "sha256": hashlib.sha256(card.read_bytes()).hexdigest(),
                    "text_rendering": "deterministic_locked_text",
                }],
            }, ensure_ascii=False), encoding="utf-8")
            config = SynthesisConfig(
                video_dir=root, script_path=root / "script.txt", narration_path=root / "voice.wav",
                music_path=root / "music.mp3", output_dir=root,
            )
            with self.assertRaisesRegex(RuntimeError, "必须由 ImageGen 一体成型"):
                _overlay_semantic_cards(
                    root / "story.mp4",
                    [{"card_kind": "title_card", "text": "通用故事", "start": 0.0, "end": 4.0}],
                    root / "out.mp4", card_dir, 8.0, config,
                )

    def test_provider_motion_uniformly_slows_to_full_title_window_without_looping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            card_dir = root / "cards"
            card_dir.mkdir()
            card = card_dir / "title.png"
            Image.new("RGB", (1920, 1080), "white").save(card)
            (card_dir / "semantic_card_generation_receipt.json").write_text(json.dumps({
                "schema_version": "story-semantic-card-generation/v1",
                "imagegen_native": True,
                "post_render_text_overlay": False,
                "artifact_semantic_plan_sha256": "a" * 64,
                "cards": [{
                    "card_kind": "title_card", "text": "《通用故事》",
                    "path": str(card), "sha256": hashlib.sha256(card.read_bytes()).hexdigest(),
                }],
            }, ensure_ascii=False), encoding="utf-8")
            (card_dir / "semantic_card_motion_request.json").write_text("{}", encoding="utf-8")
            motion = card_dir / "title_motion_6s.mp4"
            motion.write_bytes(b"provider-motion")
            config = SynthesisConfig(
                video_dir=root, script_path=root / "script.txt", narration_path=root / "voice.wav",
                music_path=root / "music.mp3", output_dir=root,
            )
            with patch(
                "story_video_synthesizer.pipeline.load_semantic_card_motion_paths",
                return_value={"title_card": motion},
            ), patch(
                "story_video_synthesizer.pipeline.probe_duration",
                return_value=6.0,
            ), patch("story_video_synthesizer.pipeline.run_command") as run:
                _overlay_semantic_cards(
                    root / "story.mp4",
                    [{"card_kind": "title_card", "text": "《通用故事》", "start": 0.0, "end": 10.2}],
                    root / "out.mp4",
                    card_dir,
                    20.0,
                    config,
                )
            command = run.call_args.args[0]
            self.assertNotIn("-stream_loop", command)
            filter_graph = command[command.index("-filter_complex") + 1]
            self.assertIn("between(t,0.000,10.200)", filter_graph)
            self.assertIn("setpts=1.70000000*(PTS-STARTPTS)+0.000/TB", filter_graph)


if __name__ == "__main__":
    unittest.main()
