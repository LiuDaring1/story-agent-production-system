from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from keying_quality import (
    analyze_keyed_rgba,
    blurred_background_issues,
    file_sha256,
    keying_preset_lock_issues,
    keying_review_images,
    lock_keying_preset,
    representative_evidence_images,
    write_evidence_assets,
)
from production_keying import (
    PRODUCTION_KEYING_FILTER_VERSION,
    production_keying_contract,
    production_keying_filter_chain,
    production_keying_fingerprint,
)
from demo_quality import demo_render_manifest_issues, write_demo_render_manifest
from story_agent_runtime import write_review_bundle


def _silhouette(*, color=(186, 145, 116, 255)) -> Image.Image:
    image = Image.new("RGBA", (160, 180), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((55, 10, 105, 60), fill=color)
    draw.rounded_rectangle((40, 50, 120, 155), radius=15, fill=color)
    draw.rectangle((15, 62, 45, 83), fill=color)
    draw.rectangle((115, 62, 145, 83), fill=color)
    draw.rectangle((52, 145, 72, 177), fill=color)
    draw.rectangle((88, 145, 108, 177), fill=color)
    return image


def _locked_fixture(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "original.mp4"
    source.write_bytes(b"read-only-green-source")
    candidate = root / "candidate.png"
    _silhouette().save(candidate)
    evidence_asset = root / "hand.png"
    _silhouette().crop((0, 45, 55, 100)).save(evidence_asset)
    search = root / "keying_search.json"
    search.write_text(json.dumps({"source_video": str(source), "candidates": [{"id": "balanced"}]}), encoding="utf-8")
    qa = root / "keying_machine_qa.json"
    evidence = root / "evidence_manifest.json"
    preset = root / "keying_preset.json"
    preset_payload = {
        "preset_version": "story-keying-preset/v2", "keying_candidate": "balanced",
        "keying_search": str(search), "machine_qa": str(qa), "evidence_manifest": str(evidence),
        "keyer": "colorkey", "chroma_color": "0x00FF00", "chroma_similarity": 0.1,
        "chroma_blend": 0.0, "person_grade": "natural", "person_beauty": "light",
        "person_crop": None,
    }
    preset.write_text(json.dumps(preset_payload), encoding="utf-8")
    fingerprint = production_keying_fingerprint(preset_payload)
    renderer = {
        "renderer_kind": "production_ffmpeg",
        "filter_version": PRODUCTION_KEYING_FILTER_VERSION,
        "filter_fingerprint": fingerprint,
        "filter_contract": production_keying_contract(preset_payload),
        "preset_path": str(preset),
        "preset_sha256": file_sha256(preset),
    }
    evidence.write_text(json.dumps({
        "version": 2, **renderer, "keying_candidate": "balanced", "artifacts": [{
            "role": "left_hand", "path": str(evidence_asset), "sha256": file_sha256(evidence_asset),
        }],
    }), encoding="utf-8")
    qa.write_text(json.dumps({
        "schema_version": "story-keying-qa/v1", **renderer, "keying_candidate": "balanced",
        "candidate_file": str(candidate), "candidate_sha256": file_sha256(candidate),
        "evidence_manifest": str(evidence), "evidence_manifest_sha256": file_sha256(evidence),
        "passed": True, "critical_errors": [],
    }), encoding="utf-8")
    bundle = write_review_bundle(root / "keying_review_bundle.json", [preset, qa, evidence, evidence_asset])
    review = root / "keying_review_review.json"
    review.write_text(json.dumps({
        "approved": True, "score": 95, "critical_errors": [], "artifact_sha256": file_sha256(bundle),
    }), encoding="utf-8")
    lock = lock_keying_preset(
        preset, machine_qa_path=qa, evidence_manifest_path=evidence,
        review_bundle_path=bundle, review_path=review,
    )
    return preset, lock, candidate, evidence_asset, source


class KeyingQualityTests(unittest.TestCase):
    def test_demo_release_and_evidence_share_one_production_filter_builder(self) -> None:
        from product_package import KeyingPreset, keying_filter_chain
        from release_video import person_key_filters

        preset = KeyingPreset(
            keyer="colorkey", chroma_color="0x14DC1E", chroma_similarity=.08,
            chroma_blend=.04, person_grade="natural", person_beauty="light",
        )
        release_settings = SimpleNamespace(**preset.__dict__)
        expected = production_keying_filter_chain("[source]", preset)
        self.assertEqual(keying_filter_chain("[source]", preset), expected)
        self.assertEqual(";".join(person_key_filters(release_settings, "[source]")), expected)

    def test_clean_edge_passes_and_fine_hair_is_not_rejected(self) -> None:
        clean = _silhouette()
        draw = ImageDraw.Draw(clean)
        for x in range(62, 100, 5):
            draw.line((x, 11, x - 4, 1), fill=(186, 145, 116, 100), width=1)
        result = analyze_keyed_rgba(clean)
        self.assertTrue(result["passed"], result)
        self.assertIn("head_hair", result["regions"])

    def test_green_spill_and_dark_halo_fail(self) -> None:
        spill = analyze_keyed_rgba(_silhouette(color=(20, 210, 30, 255)))
        halo = analyze_keyed_rgba(_silhouette(color=(8, 8, 8, 255)))
        self.assertTrue(any("green_spill" in item for item in spill["critical_errors"]))
        self.assertTrue(any("dark_halo" in item for item in halo["critical_errors"]))

    def test_jagged_edge_and_internal_alpha_hole_fail(self) -> None:
        jagged = Image.new("RGBA", (160, 180), (0, 0, 0, 0))
        draw = ImageDraw.Draw(jagged)
        points = [(30 + (8 if y % 4 < 2 else 0), y) for y in range(10, 170)]
        draw.polygon(points + [(130, 169), (130, 10)], fill=(186, 145, 116, 255))
        jagged_result = analyze_keyed_rgba(jagged)
        self.assertIn("severe_jagged_edge", jagged_result["critical_errors"])
        hole = _silhouette()
        ImageDraw.Draw(hole).ellipse((65, 80, 95, 112), fill=(0, 0, 0, 0))
        hole_result = analyze_keyed_rgba(hole)
        self.assertIn("alpha_holes_or_internal_transparency", hole_result["critical_errors"])

    def test_hand_and_hem_damage_are_region_specific_failures(self) -> None:
        damaged = _silhouette()
        alpha = damaged.getchannel("A")
        ImageDraw.Draw(alpha).rectangle((15, 62, 45, 83), fill=80)
        ImageDraw.Draw(alpha).rectangle((40, 135, 120, 160), fill=80)
        damaged.putalpha(alpha)
        regions = {
            "left_shoulder_forearm_hand": (10, 55, 50, 90),
            "hem_lower_outline": (35, 130, 125, 165),
            "full_body": (10, 5, 150, 178),
        }
        result = analyze_keyed_rgba(damaged, region_boxes=regions)
        self.assertIn("contour_or_internal_transparency", result["regions"]["left_shoulder_forearm_hand"]["issues"])
        self.assertIn("contour_or_internal_transparency", result["regions"]["hem_lower_outline"]["issues"])

    def test_background_leak_fails(self) -> None:
        leaked = _silhouette()
        ImageDraw.Draw(leaked).rectangle((0, 0, 12, 12), fill=(186, 145, 116, 255))
        self.assertIn("background_leak:corners", analyze_keyed_rgba(leaked)["critical_errors"])

    def test_blurred_background_rectangular_seam(self) -> None:
        continuous = Image.new("RGB", (640, 360))
        pixels = continuous.load()
        for y in range(360):
            for x in range(640):
                pixels[x, y] = (70 + x // 12, 100 + x // 20, 140)
        blocked = continuous.copy()
        ImageDraw.Draw(blocked).rectangle((320, 0, 639, 359), fill=(180, 80, 60))
        self.assertEqual(blurred_background_issues(continuous), [])
        self.assertTrue(blurred_background_issues(blocked))

    def test_preset_lock_binds_candidate_evidence_review_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preset, _lock, candidate, evidence_asset, source = _locked_fixture(Path(directory))
            self.assertEqual(keying_preset_lock_issues(preset), [])
            original_hash = file_sha256(source)
            candidate.write_bytes(candidate.read_bytes() + b"tamper")
            self.assertTrue(any("candidate" in issue for issue in keying_preset_lock_issues(preset)))
            self.assertEqual(file_sha256(source), original_hash)

            # Recreate, then mutate concrete evidence: a current review cannot
            # authorize different hair/hand/body evidence bytes.
            preset, _lock, _candidate, evidence_asset, source = _locked_fixture(Path(directory) / "again")
            evidence_asset.write_bytes(evidence_asset.read_bytes() + b"tamper")
            self.assertTrue(any("evidence" in issue or "review" in issue for issue in keying_preset_lock_issues(preset)))

            # A review file is part of the authorization, not an advisory file
            # that can be edited after the preset has been locked.
            preset, _lock, _candidate, _evidence_asset, _source = _locked_fixture(Path(directory) / "review")
            review = preset.with_name("keying_review_review.json")
            review.write_bytes(review.read_bytes() + b" ")
            self.assertTrue(any("review" in issue for issue in keying_preset_lock_issues(preset)))

    def test_filter_contract_change_invalidates_old_evidence_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preset, _lock, _candidate, _evidence_asset, _source = _locked_fixture(Path(directory))
            self.assertEqual(keying_preset_lock_issues(preset), [])
            with patch("production_keying.PRODUCTION_KEYING_FILTER_VERSION", "story-production-keying-filter/v999"):
                self.assertTrue(any("filter" in issue for issue in keying_preset_lock_issues(preset)))

    def test_evidence_generation_does_not_modify_original_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            standing = root / "standing.png"
            gesture = root / "gesture.png"
            source = root / "original.mp4"
            green = Image.new("RGB", (160, 180), (20, 220, 30))
            green.paste(_silhouette().convert("RGB"), (0, 0), _silhouette().getchannel("A"))
            green.save(standing)
            green.save(gesture)
            source.write_bytes(b"original-user-video")
            before = file_sha256(source)
            preset_payload = {
                "keyer": "colorkey", "chroma_color": "0x14DC1E", "chroma_similarity": .08,
                "chroma_blend": .04, "person_grade": "natural", "person_beauty": "light",
                "person_crop": None,
            }
            preset = root / "keying_preset.json"
            preset.write_text(json.dumps(preset_payload), encoding="utf-8")
            with patch("keying_quality.render_production_keyed_foreground", wraps=__import__(
                "production_keying"
            ).render_production_keyed_foreground) as renderer:
                _candidate, _qa, manifest = write_evidence_assets(
                    standing, gesture, chroma_color="0x14DC1E", similarity=.08, blend=.04,
                    output_dir=root / "evidence", candidate_id="balanced",
                    preset=preset_payload, preset_path=preset,
                )
            self.assertEqual(renderer.call_count, 2)
            self.assertEqual(manifest["renderer_kind"], "production_ffmpeg")
            self.assertEqual(manifest["filter_fingerprint"], production_keying_fingerprint(preset_payload))
            self.assertEqual(file_sha256(source), before)

    def test_representative_evidence_is_explicitly_added_to_review_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roles = [
                "standing_full_body", "wide_gesture_full_body", "standing_head_hair",
                "wide_gesture_left_shoulder_forearm_hand", "wide_gesture_right_shoulder_forearm_hand",
                "standing_garment_outline", "wide_gesture_high_contrast_edges",
            ]
            artifacts = []
            for role in roles:
                path = root / f"{role}.png"
                _silhouette().save(path)
                artifacts.append({"role": role, "path": str(path), "sha256": file_sha256(path)})
            manifest = root / "evidence_manifest.json"
            manifest.write_text(json.dumps({"version": 2, "artifacts": artifacts}), encoding="utf-8")
            candidate = root / "candidates.jpg"
            _silhouette().convert("RGB").save(candidate)
            preview = root / "preview.png"
            _silhouette().save(preview)
            selected = representative_evidence_images(manifest)
            images = keying_review_images(candidate, manifest, [preview])
            self.assertEqual(images[0], candidate)
            self.assertTrue(all(path in images for path in selected))
            self.assertEqual(images[-1], preview)

    def test_non_applicable_lower_body_regions_are_not_forced(self) -> None:
        alpha = Image.new("L", (160, 180), 0)
        ImageDraw.Draw(alpha).ellipse((50, 10, 110, 85), fill=255)
        from keying_quality import evidence_regions

        regions = evidence_regions(alpha)
        self.assertNotIn("hem_lower_outline", regions)
        self.assertNotIn("legs_feet", regions)

    def test_demo_manifest_binds_one_logo_semantic_plan_and_reviewed_keying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            plan_path = project / "99_项目状态" / "story_contract" / "artifact_semantic_plan.json"
            plan_path.parent.mkdir(parents=True)
            plan = {
                "schema_version": "1.0", "story_contract_dependency_sha256": "b" * 64,
                "artifacts": {"demo_subtitles": {"decisions": [{
                    "semantic_kind": "story_body", "action": "include", "subtitle_policy": "show",
                }]}},
            }
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            preset, _lock, _candidate, _evidence, source = _locked_fixture(root / "keying")
            logo = root / "official-logo.png"
            Image.new("RGBA", (120, 40), (250, 170, 20, 255)).save(logo)
            brand_spec = root / "demo.compiled.json"
            brand = {
                "version": 1, "consumer": "demo", "contract_schema_version": "1.0.0",
                "story_contract_sha256": "a" * 64, "story_contract_dependency_sha256": "b" * 64,
                "contract_projection_sha256": "c" * 64, "official_logo_path": str(logo),
                "official_logo_sha256": file_sha256(logo), "official_logo_count": 1,
            }
            brand_spec.write_text(json.dumps(brand), encoding="utf-8")
            output = root / "demo.mp4"
            output.write_bytes(b"offline-demo")
            manifest = write_demo_render_manifest(
                root / "demo_render_manifest.json", project_dir=project, semantic_plan=plan,
                demo_brand_spec_path=brand_spec, demo_brand_spec=brand, keying_preset_path=preset,
                source_greenscreen=source, output_artifacts=[output], preview=False,
            )
            with patch("demo_quality.load_current_artifact_semantic_plan", return_value=plan):
                self.assertEqual(demo_render_manifest_issues(manifest, project), [])
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["official_logo_count"], 1)
            self.assertEqual(payload["official_logo_sha256"], file_sha256(logo))
            self.assertEqual(payload["artifact_semantic_plan_sha256"], file_sha256(plan_path))

            # A changed semantic plan makes the existing Demo receipt stale.
            plan["artifacts"]["demo_subtitles"]["decisions"][0]["subtitle_policy"] = "hide"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with patch("demo_quality.load_current_artifact_semantic_plan", return_value=plan):
                self.assertTrue(any("semantic_plan" in issue for issue in demo_render_manifest_issues(manifest, project)))

    def test_bad_preset_file_cannot_write_demo_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            plan_path = project / "99_项目状态" / "story_contract" / "artifact_semantic_plan.json"
            plan_path.parent.mkdir(parents=True)
            plan = {"schema_version": "1.0", "story_contract_dependency_sha256": "b" * 64}
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            preset, _lock, _candidate, _evidence, source = _locked_fixture(root / "keying")
            preset.write_text(preset.read_text(encoding="utf-8") + " ", encoding="utf-8")
            logo = root / "logo.png"
            Image.new("RGBA", (20, 20), (255, 255, 255, 255)).save(logo)
            spec = root / "spec.json"
            brand = {
                "contract_schema_version": "1.0.0", "story_contract_sha256": "a" * 64,
                "story_contract_dependency_sha256": "b" * 64, "contract_projection_sha256": "c" * 64,
                "official_logo_path": str(logo),
            }
            spec.write_text(json.dumps(brand), encoding="utf-8")
            output = root / "demo.mp4"
            output.write_bytes(b"offline")
            with self.assertRaisesRegex(ValueError, "invalid reviewed keying preset"):
                write_demo_render_manifest(
                    root / "manifest.json", project_dir=project, semantic_plan=plan,
                    demo_brand_spec_path=spec, demo_brand_spec=brand, keying_preset_path=preset,
                    source_greenscreen=source, output_artifacts=[output], preview=False,
                )


if __name__ == "__main__":
    unittest.main()
