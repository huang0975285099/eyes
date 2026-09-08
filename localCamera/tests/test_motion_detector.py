import unittest

import cv2
import numpy as np

from app import MonitorConfig, MotionDetector


class MonitorConfigTests(unittest.TestCase):
    def test_update_clamps_values(self) -> None:
        config = MonitorConfig()
        config.update(
            {
                "camera_index": 99,
                "sensitivity": -10,
                "min_area_percent": 90,
                "consecutive_frames": 0,
            }
        )
        self.assertEqual(config.camera_index, 9)
        self.assertEqual(config.sensitivity, 1)
        self.assertEqual(config.min_area_percent, 50.0)
        self.assertEqual(config.consecutive_frames, 1)


class MotionDetectorTests(unittest.TestCase):
    def test_sustained_change_triggers_after_warmup(self) -> None:
        detector = MotionDetector()
        config = MonitorConfig(
            sensitivity=70,
            min_area_percent=0.5,
            consecutive_frames=3,
            cooldown_seconds=1,
        )
        base = np.zeros((360, 640, 3), dtype=np.uint8)
        for index in range(22):
            score, triggered, _ = detector.process(base, config, 100 + index, 0)
            self.assertFalse(triggered)

        changed = base.copy()
        cv2.rectangle(changed, (80, 80), (220, 220), (255, 255, 255), -1)
        results = [detector.process(changed, config, 200 + index, 0) for index in range(3)]
        self.assertFalse(results[0][1])
        self.assertFalse(results[1][1])
        self.assertTrue(results[2][1])
        self.assertGreater(results[2][0], 0.5)
        self.assertTrue(results[2][2])

    def test_cooldown_suppresses_alert(self) -> None:
        detector = MotionDetector()
        config = MonitorConfig(consecutive_frames=1, cooldown_seconds=10)
        base = np.zeros((180, 320, 3), dtype=np.uint8)
        for index in range(22):
            detector.process(base, config, 100 + index, 0)
        changed = np.full_like(base, 255)
        _, triggered, _ = detector.process(changed, config, 105, 100)
        self.assertFalse(triggered)


if __name__ == "__main__":
    unittest.main()
