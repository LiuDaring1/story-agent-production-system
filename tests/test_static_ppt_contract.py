from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image

from shot_storyboard_pipeline import (
    build_storyboard_manifest,
    compile_consumers,
    create_asset_bundle,
    seal_storyboard_manifest,
)
from static_ppt_contract import (
    file_sha256,
    validate_delivery_receipt,
    validate_pair,
    write_delivery_receipt,
)
from tests.test_story_r2v_skill import valid_two_shot_plan


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_minimal_static_ppt(
    path: Path,
    durations: list[float],
    music: bytes,
    subtitles: list[str] | None = None,
) -> None:
    namespace = "http://schemas.openxmlformats.org/presentationml/2006/main"
    drawing = "http://schemas.openxmlformats.org/drawingml/2006/main"
    subtitles = subtitles or [""] * len(durations)
    with zipfile.ZipFile(path, "w") as archive:
        for index, (seconds, subtitle) in enumerate(zip(durations, subtitles), start=1):
            milliseconds = max(500, int(round(seconds * 1000)))
            subtitle_xml = ""
            if subtitle:
                paragraphs = "".join(
                    f'<a:p><a:r><a:t>{escape(line)}</a:t></a:r></a:p>'
                    for line in subtitle.split("\n")
                )
                subtitle_xml = (
                    f'<p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" '
                    f'name="subtitle-{index:03d}"/></p:nvSpPr><p:txBody>{paragraphs}'
                    f'</p:txBody></p:sp></p:spTree></p:cSld>'
                )
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                (
                    f'<p:sld xmlns:p="{namespace}" xmlns:a="{drawing}">'
                    f"{subtitle_xml}"
                    f'<p:transition advClick="1" advTm="{milliseconds}"/>'
                    "</p:sld>"
                ),
            )
        archive.writestr("ppt/media/story-score.mp3", music)


class StaticPptContractTests(unittest.TestCase):
    def test_delivery_receipt_accepts_dynamic_director_count_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            director = valid_two_shot_plan()
            for index, asset in enumerate(director["assets"], start=1):
                asset_path = root / "assets" / f"asset-{index:02d}.png"
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (64, 64), (index * 20, 80, 140)).save(asset_path)
                asset["path"] = str(asset_path)
                asset["sha256"] = file_sha256(asset_path)
            director_path = root / "director.json"
            write_json(director_path, director)

            asset_bundle_path = root / "asset_bundle.json"
            asset_bundle = create_asset_bundle(director_path, asset_bundle_path)
            asset_review_path = root / "asset_review.json"
            write_json(
                asset_review_path,
                {
                    "approved": True,
                    "score": 93,
                    "critical_errors": [],
                    "artifact_sha256": asset_bundle["asset_bundle_sha256"],
                },
            )
            planned_path = root / "storyboards_planned.json"
            storyboards_dir = root / "storyboards"
            planned = build_storyboard_manifest(
                director_path,
                asset_bundle_path,
                asset_review_path,
                storyboards_dir,
                planned_path,
            )
            for index, entry in enumerate(planned["entries"], start=1):
                Image.new("RGB", (1600, 900), (50 * index, 120, 180)).save(entry["image_path"])
            sealed_path = root / "storyboards_sealed.json"
            sealed = seal_storyboard_manifest(planned_path, sealed_path)
            storyboard_review_path = root / "storyboard_review.json"
            write_json(
                storyboard_review_path,
                {
                    "approved": True,
                    "score": 95,
                    "critical_errors": [],
                    "artifact_sha256": sealed["storyboard_bundle_sha256"],
                },
            )

            music = b"embedded-story-score"
            music_path = root / "music.mp3"
            music_path.write_bytes(music)
            title = root / "title.png"
            Image.new("RGB", (1600, 900), (30, 40, 50)).save(title)
            previous_ppt_path = root / "previous_ppt.json"
            write_json(
                previous_ppt_path,
                {
                    "story_name": "动态镜头数测试",
                    "music_path": str(music_path),
                    "music_sha256": hashlib.sha256(music).hexdigest(),
                    "slides": [
                        {
                            "shot_id": "TITLE",
                            "poster_path": str(title),
                            "poster_sha256": file_sha256(title),
                            "duration_seconds": 2.0,
                            "subtitle": "",
                        },
                        {"shot_id": "shot-001", "duration_seconds": 4.0},
                        {"shot_id": "shot-002", "duration_seconds": 4.0},
                    ],
                },
            )
            r2v_path = root / "compiled_r2v.json"
            jobs_path = root / "jobs.csv"
            plan_path = root / "static_ppt_plan.json"
            compile_receipt_path = root / "compile_receipt.json"
            compile_consumers(
                sealed_path,
                storyboard_review_path,
                r2v_path,
                jobs_path,
                compile_receipt_path,
                previous_ppt_path,
                plan_path,
            )
            # Provider execution legitimately appends status/result data after
            # the immutable storyboard/PPT compile receipt is sealed.
            with jobs_path.open("a", encoding="utf-8") as handle:
                handle.write("provider-status-updated\n")

            durations = [2.0, 4.0, 4.0]
            plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
            subtitles = [
                ""
                if row["shot_id"] in {"TITLE", "MORAL"}
                else str(row.get("subtitle") or "")
                for row in plan_payload["slides"]
            ]
            with_subtitles = root / "with-subtitles.pptx"
            without_subtitles = root / "without-subtitles.pptx"
            write_minimal_static_ppt(with_subtitles, durations, music, subtitles)
            write_minimal_static_ppt(without_subtitles, durations, music)
            slide_ids, _slides = validate_pair(
                director_path, plan_path, with_subtitles, without_subtitles
            )
            self.assertEqual(slide_ids, ["TITLE", "shot-001", "shot-002"])

            multiline_ppt = root / "multiline.pptx"
            bad_subtitles = list(subtitles)
            bad_subtitles[1] = "错误\n换行"
            write_minimal_static_ppt(multiline_ppt, durations, music, bad_subtitles)
            with self.assertRaisesRegex(ValueError, "字幕不是单行"):
                validate_pair(director_path, plan_path, multiline_ppt, without_subtitles)

            original_plan_text = plan_path.read_text(encoding="utf-8")
            bad_plan = json.loads(original_plan_text)
            bad_plan["slides"][1]["subtitle"] += "\n错误换行"
            write_json(plan_path, bad_plan)
            with self.assertRaisesRegex(ValueError, "必须合并为底部单行"):
                validate_pair(director_path, plan_path, with_subtitles, without_subtitles)
            plan_path.write_text(original_plan_text, encoding="utf-8")

            delivery_receipt_path = root / "static_ppt_delivery_receipt.json"
            receipt = write_delivery_receipt(
                director_path=director_path,
                compile_receipt_path=compile_receipt_path,
                plan_path=plan_path,
                with_subtitles=with_subtitles,
                without_subtitles=without_subtitles,
                output_path=delivery_receipt_path,
            )
            self.assertEqual(receipt["director_shot_count"], 2)
            self.assertEqual(receipt["total_slide_count"], 3)
            self.assertEqual(validate_delivery_receipt(delivery_receipt_path)["slide_ids"], slide_ids)

            with with_subtitles.open("ab") as handle:
                handle.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "绑定失效"):
                validate_delivery_receipt(delivery_receipt_path)


if __name__ == "__main__":
    unittest.main()
