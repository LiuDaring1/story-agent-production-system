from __future__ import annotations

import unittest

from render_customer_backgrounds import body_timings_from_plan


class CustomerBackgroundRenderTests(unittest.TestCase):
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
