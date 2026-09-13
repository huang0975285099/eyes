from datetime import datetime
import unittest

import numpy as np

from voice_assistant import (
    chinese_number,
    CameraFrameStore,
    contains_wake_phrase,
    format_time_zh,
    is_time_command,
    is_vision_command,
    normalize_text,
    resample_pcm,
)


class TextTests(unittest.TestCase):
    def test_normalize_and_wake(self) -> None:
        self.assertEqual(normalize_text(" 小布，小布！ "), "小布小布")
        self.assertTrue(contains_wake_phrase("小 布 小 布", ("小布小布",)))
        self.assertTrue(contains_wake_phrase("小步 小步", ("小步 小步",)))

    def test_time_command(self) -> None:
        self.assertTrue(is_time_command("现在 几点 了"))
        self.assertTrue(is_time_command("几点？"))
        self.assertFalse(is_time_command("今天天气怎么样"))

    def test_vision_command(self) -> None:
        self.assertTrue(is_vision_command("你 看到了 什么"))
        self.assertTrue(is_vision_command("你 看到 的 什么"))
        self.assertTrue(is_vision_command("画面里有什么？"))
        self.assertFalse(is_vision_command("讲一个笑话"))

    def test_chinese_number(self) -> None:
        self.assertEqual(chinese_number(0), "零")
        self.assertEqual(chinese_number(10), "十")
        self.assertEqual(chinese_number(19), "十九")
        self.assertEqual(chinese_number(42), "四十二")

    def test_format_time(self) -> None:
        self.assertEqual(
            format_time_zh(datetime(2026, 9, 12, 0, 0)),
            "现在是凌晨十二点整。",
        )
        self.assertEqual(
            format_time_zh(datetime(2026, 9, 12, 18, 5)),
            "现在是晚上六点零五分。",
        )
        self.assertEqual(
            format_time_zh(datetime(2026, 9, 12, 13, 30)),
            "现在是中午一点三十分。",
        )


class AudioTests(unittest.TestCase):
    def test_resample_pcm_shape_and_edges(self) -> None:
        samples = np.array([[0], [100], [200]], dtype=np.int16)
        result = resample_pcm(samples, 3, 6)
        self.assertEqual(result.shape, (6, 1))
        self.assertEqual(int(result[0, 0]), 0)
        self.assertEqual(int(result[-1, 0]), 200)


class CameraFrameStoreTests(unittest.TestCase):
    def test_frame_and_status(self) -> None:
        store = CameraFrameStore()
        self.assertIsNone(store.latest_frame(1))
        store.update_frame(b"jpeg")
        self.assertEqual(store.latest_frame(1), b"jpeg")
        store.set_assistant_status("正在分析", "回答")
        status = store.status()
        self.assertTrue(status["camera_ready"])
        self.assertEqual(status["last_answer"], "回答")

    def test_shutdown_request(self) -> None:
        store = CameraFrameStore()
        self.assertFalse(store.shutdown_event.is_set())
        store.request_shutdown()
        self.assertTrue(store.shutdown_event.is_set())
        self.assertEqual(store.status()["assistant_status"], "正在关闭语音助手")


if __name__ == "__main__":
    unittest.main()
