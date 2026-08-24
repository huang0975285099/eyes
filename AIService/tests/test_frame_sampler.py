from __future__ import annotations

import unittest

from ai_service.modules.frame_sampler import plan_frame_offsets


class FramePlanTests(unittest.TestCase):
    def test_short_segment_has_no_frames(self) -> None:
        self.assertEqual(plan_frame_offsets(29.9), [])

    def test_normal_segment_has_two_centered_frames(self) -> None:
        self.assertEqual(plan_frame_offsets(300), [75, 225])

    def test_ten_minute_segment_uses_expected_sampling_rule(self) -> None:
        self.assertEqual(plan_frame_offsets(600), [150, 450])

    def test_long_segment_adds_one_frame_per_five_minutes(self) -> None:
        self.assertEqual(plan_frame_offsets(900), [150, 450, 750])


if __name__ == "__main__":
    unittest.main()
