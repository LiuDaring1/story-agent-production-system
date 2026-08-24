from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from production_keying import (
    PRODUCTION_KEYING_FILTER_VERSION,
    RVM_ALPHA_CHOKE_FILTER,
    RVM_ALPHA_CHOKE_PIXELS,
    production_keying_contract,
    production_keying_filter_chain,
)
from rvm_keying import (
    OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
    RVM_BACKEND_VERSION,
    RVM_RECEIPT_SCHEMA_VERSION,
    file_sha256,
    rvm_receipt_issues,
)


class RvmKeyingTests(unittest.TestCase):
    def test_rvm_compositor_chokes_existing_alpha_once_then_despills(self) -> None:
        graph = production_keying_filter_chain(
            "[0:v]",
            {
                "keyer": "rvm",
                "person_grade": "natural",
                "rvm_model_sha256": OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
                "rvm_foreground_sha256": "a" * 64,
                "rvm_receipt_sha256": "b" * 64,
            },
        )
        self.assertIn("format=rgba,split[rvm_person_rgb][rvm_person_alpha_source]", graph)
        self.assertIn(
            f"[rvm_person_alpha_source]alphaextract,{RVM_ALPHA_CHOKE_FILTER}"
            "[rvm_person_alpha]",
            graph,
        )
        self.assertIn("[rvm_person_rgb][rvm_person_alpha]alphamerge,despill=type=green", graph)
        self.assertIn("brightness=0.2", graph)
        self.assertNotIn("colorkey=", graph)
        self.assertNotIn("chromakey=", graph)
        self.assertEqual(1, graph.count("erosion"))
        self.assertEqual(1, RVM_ALPHA_CHOKE_PIXELS)

    def test_rvm_contract_discloses_alpha_choke_and_binds_v5_graph(self) -> None:
        contract = production_keying_contract({"keyer": "rvm"})
        self.assertEqual("story-production-keying-filter/v5", PRODUCTION_KEYING_FILTER_VERSION)
        self.assertEqual(RVM_ALPHA_CHOKE_FILTER, contract["rvm_alpha_choke_filter"])
        self.assertEqual(RVM_ALPHA_CHOKE_PIXELS, contract["rvm_alpha_choke_pixels"])
        self.assertIn("alphaextract,erosion", contract["filter_graph_template"])

    def test_rvm_alpha_choke_is_selected_per_preset(self) -> None:
        zero = production_keying_filter_chain(
            "[0:v]", {"keyer": "rvm", "rvm_alpha_choke_pixels": 0}
        )
        two = production_keying_filter_chain(
            "[0:v]", {"keyer": "rvm", "rvm_alpha_choke_pixels": 2}
        )
        self.assertIn("alphaextract[rvm_person_alpha]", zero)
        self.assertNotIn("alphaextract,erosion", zero)
        self.assertEqual(2, two.count("erosion"))
        self.assertEqual(0, production_keying_contract({
            "keyer": "rvm", "rvm_alpha_choke_pixels": 0,
        })["rvm_alpha_choke_pixels"])

    def test_receipt_binds_source_and_reusable_alpha_video_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mov"
            output = root / "foreground.mov"
            receipt = root / "receipt.json"
            source.write_bytes(b"source-video")
            output.write_bytes(b"alpha-video")
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": RVM_RECEIPT_SCHEMA_VERSION,
                        "backend_version": RVM_BACKEND_VERSION,
                        "status": "complete",
                        "source_sha256": file_sha256(source),
                        "model_sha256": OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
                        "frame_count": 125,
                        "output_video": str(output),
                        "output_sha256": file_sha256(output),
                        "temporal_recurrence_used": True,
                        "alpha_channel_verified": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                [],
                rvm_receipt_issues(receipt, expected_source=source, expected_output=output),
            )
            output.write_bytes(b"changed")
            self.assertIn(
                "rvm_output_sha256_mismatch",
                rvm_receipt_issues(receipt, expected_source=source, expected_output=output),
            )

    def test_receipt_rejects_non_temporal_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "foreground.mov"
            receipt = root / "receipt.json"
            output.write_bytes(b"alpha-video")
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": RVM_RECEIPT_SCHEMA_VERSION,
                        "backend_version": RVM_BACKEND_VERSION,
                        "status": "complete",
                        "model_sha256": OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
                        "frame_count": 1,
                        "output_video": str(output),
                        "output_sha256": file_sha256(output),
                        "temporal_recurrence_used": False,
                        "alpha_channel_verified": True,
                    }
                ),
                encoding="utf-8",
            )
            self.assertIn("rvm_temporal_recurrence_missing", rvm_receipt_issues(receipt))


if __name__ == "__main__":
    unittest.main()
