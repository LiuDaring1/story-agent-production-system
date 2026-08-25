from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from semantic_card_motion import (
    MOTION_PROVIDER_RESOLUTION,
    MOTION_PROVIDER_SAFE_PROMPT_CHARS,
    MOTION_RECEIPT_SCHEMA,
    file_sha256,
    load_semantic_card_motion_paths,
    select_motion_provider_seconds,
    semantic_card_motion_prompt,
    semantic_card_motion_receipt_issues,
    write_semantic_card_motion_request,
)


class SemanticCardMotionTests(unittest.TestCase):
    def test_provider_prompt_stays_within_observed_safe_limit(self) -> None:
        prompt = semantic_card_motion_prompt()
        self.assertLessEqual(
            len(prompt.encode("utf-16-le")) // 2,
            MOTION_PROVIDER_SAFE_PROMPT_CHARS,
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.card_dir = Path(self.temporary.name) / "semantic_cards"
        self.card_dir.mkdir()
        self.cards = []
        for kind, text, color in (
            ("title_card", "爱比美的公鸡", (220, 160, 80)),
            ("moral_card", "不要只注重外表。", (80, 150, 100)),
        ):
            path = self.card_dir / f"{kind}.png"
            Image.new("RGB", (320, 180), color).save(path)
            self.cards.append(
                {
                    "card_kind": kind,
                    "text": text,
                    "path": str(path),
                    "sha256": file_sha256(path),
                    "ocr_passed": True,
                }
            )
        (self.card_dir / "semantic_card_generation_receipt.json").write_text(
            json.dumps(
                {
                    "schema_version": "story-semantic-card-generation/v1",
                    "artifact_semantic_plan_sha256": "a" * 64,
                    "imagegen_native": True,
                    "post_render_text_overlay": False,
                    "attempt_count": 1,
                    "cards": self.cards,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.windows = [
            {
                "card_kind": "title_card",
                "semantic_kind": "title",
                "text": "爱比美的公鸡",
                "start": 0.0,
                "end": 7.0,
            },
            {
                "card_kind": "moral_card",
                "semantic_kind": "moral",
                "text": "不要只注重外表。",
                "start": 18.0,
                "end": 21.0,
            },
        ]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_valid_receipt(self, request_path: Path) -> Path:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        rows = []
        for index, item in enumerate(request["cards"], start=1):
            output = Path(item["output_video_path"])
            fixture_duration = float(item["required_duration_seconds"]) + 0.1
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"color=c={'red' if index == 1 else 'blue'}:s=320x180:d={fixture_duration}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rows.append(
                {
                    "card_kind": item["card_kind"],
                    "text": item["text"],
                    "source_image_sha256": item["source_image_sha256"],
                    "prompt_sha256": item["prompt_sha256"],
                    "output_video_path": str(output),
                    "output_video_sha256": file_sha256(output),
                    "attempt_count": 1,
                    "provider": "fixture-provider",
                    "provider_request_id": f"request-{index}",
                    "text_region_locked": True,
                    "non_text_motion_only": True,
                    "ocr_first_frame_passed": True,
                    "ocr_middle_frame_passed": True,
                    "ocr_last_frame_passed": True,
                    "text_stability_passed": True,
                    "visual_review_passed": True,
                }
            )
        receipt = self.card_dir / "semantic_card_motion_receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": MOTION_RECEIPT_SCHEMA,
                    "request_sha256": file_sha256(request_path),
                    "artifact_semantic_plan_sha256": request["artifact_semantic_plan_sha256"],
                    "cards": rows,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return receipt

    def test_request_chooses_nearest_six_or_ten_second_clip_without_looping(self) -> None:
        request_path = write_semantic_card_motion_request(
            card_dir=self.card_dir,
            windows=self.windows,
            artifact_semantic_plan_sha256="a" * 64,
        )
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual([row["required_duration_seconds"] for row in request["cards"]], [6.0, 6.0])
        self.assertEqual([row["loop_policy"] for row in request["cards"]], ["forbidden", "forbidden"])
        self.assertEqual(
            [row["requested_resolution"] for row in request["cards"]],
            [MOTION_PROVIDER_RESOLUTION] * 2,
        )
        self.assertEqual(request["cards"][0]["presentation_window_seconds"], 7.0)
        self.assertTrue(request["production_requires_provider_video"])

    def test_provider_duration_selection_uses_nearest_choice_and_longer_tie(self) -> None:
        self.assertEqual(select_motion_provider_seconds(2.0), 6.0)
        self.assertEqual(select_motion_provider_seconds(7.9), 6.0)
        self.assertEqual(select_motion_provider_seconds(8.0), 10.0)
        self.assertEqual(select_motion_provider_seconds(13.0), 10.0)

    def test_receipt_requires_current_provider_video_and_text_stability(self) -> None:
        request_path = write_semantic_card_motion_request(
            card_dir=self.card_dir,
            windows=self.windows,
            artifact_semantic_plan_sha256="a" * 64,
        )
        receipt = self._write_valid_receipt(request_path)
        self.assertEqual(semantic_card_motion_receipt_issues(request_path, receipt), [])
        self.assertEqual(set(load_semantic_card_motion_paths(request_path, receipt)), {"title_card", "moral_card"})
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["cards"][0]["text_stability_passed"] = False
        receipt.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertIn(
            "semantic_card_motion_text_stability_passed_failed:title_card",
            semantic_card_motion_receipt_issues(request_path, receipt),
        )


if __name__ == "__main__":
    unittest.main()
