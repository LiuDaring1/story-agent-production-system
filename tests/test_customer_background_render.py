from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path

from render_customer_backgrounds import body_timings_from_plan, validate_formal_assembly, file_sha256


class CustomerBackgroundRenderTests(unittest.TestCase):
    def test_v2_decisions_select_body_rows_without_title_or_moral(self):
        plan = {"schema_version": "story-r2v-assembly-decisions-v2", "segments": [
            {"segment_id": "TITLE", "timeline_start": 0, "timeline_end": 3},
            {"segment_id": "shot-001", "timeline_start": 3, "timeline_end": 4},
            {"segment_id": "shot-002", "timeline_start": 4, "timeline_end": 6},
            {"segment_id": "MORAL", "timeline_start": 6, "timeline_end": 8}]}
        rows = [{"line": f"L{i}", "source_start": i, "source_end": i+.5} for i in range(1, 8)]
        self.assertEqual([r.line for r in body_timings_from_plan(plan, rows)], ["L3", "L4", "L5"])
        plan["segments"][2]["timeline_start"] = 4.2
        with self.assertRaisesRegex(ValueError, "不连续"):
            body_timings_from_plan(plan, rows)
        plan["segments"][2]["timeline_start"] = 4
        plan["segments"][1]["timeline_start"] = 3.2
        with self.assertRaisesRegex(ValueError, "截断"):
            body_timings_from_plan(plan, rows)

    def test_v2_decisions_reject_changed_master_or_plan_window(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); master=root/'master.mp4'; audio=root/'voice.mp3'; plan=root/'plan.json'
            master.write_bytes(b'master');audio.write_bytes(b'audio');plan.write_text(json.dumps({'shots':[{'shot_id':'shot-001','source_start':3,'source_end':6}]}))
            receipt={'schema_version':'story-r2v-assembly-decisions-v2','qualification':'formal_reviewed_master','output_path':str(master),'output_sha256':file_sha256(master),'audio_path':str(audio),'audio_sha256':file_sha256(audio),'plan_path':str(plan),'plan_sha256':file_sha256(plan),'segments':[{'segment_id':'TITLE','timeline_start':0,'timeline_end':3},{'segment_id':'shot-001','timeline_start':3,'timeline_end':6},{'segment_id':'MORAL','timeline_start':6,'timeline_end':8}]}
            validate_formal_assembly(receipt,master,audio)
            receipt['segments'][1]['timeline_end']=7
            with self.assertRaisesRegex(ValueError,'不一致'):validate_formal_assembly(receipt,master,audio)
            receipt['segments'][1]['timeline_end']=6;master.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'哈希不匹配'):validate_formal_assembly(receipt,master,audio)

    def test_v2_decisions_reject_unknown_non_story_segment_ids(self):
        plan = {"schema_version": "story-r2v-assembly-decisions-v2", "segments": [
            {"segment_id": "TITLE", "timeline_start": 0, "timeline_end": 1},
            {"segment_id": "chapter-one", "timeline_start": 1, "timeline_end": 2},
            {"segment_id": "MORAL", "timeline_start": 2, "timeline_end": 3},
        ]}
        rows = [{"line": "正文", "source_start": 1, "source_end": 2}]
        with self.assertRaisesRegex(ValueError, "未知片段 ID"):
            body_timings_from_plan(plan, rows)

    def test_body_srt_uses_only_non_title_non_moral_authoritative_rows(self) -> None:
        plan = {
            "shots": [
                {"shot_id": "S01", "authoritative_line_start": 3, "authoritative_line_end": 4},
                {"shot_id": "S02", "authoritative_line_start": 5, "authoritative_line_end": 5},
                {"shot_id": "MORAL", "authoritative_line_start": 6, "authoritative_line_end": 7},
            ]
        }
        rows = [
            {"line": f"L{index}", "source_start": float(index), "source_end": float(index) + 0.5}
            for index in range(1, 8)
        ]
        timings = body_timings_from_plan(plan, rows)
        self.assertEqual([item.line for item in timings], ["L3", "L4", "L5"])
        self.assertEqual(timings[0].source_start, 3.0)
        self.assertEqual(timings[-1].source_end, 5.5)


if __name__ == "__main__":
    unittest.main()
