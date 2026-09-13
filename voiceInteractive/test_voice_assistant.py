from datetime import datetime
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

from voice_assistant import (
    chinese_number,
    CameraFrameStore,
    contains_wake_phrase,
    format_time_zh,
    is_end_conversation_command,
    is_exit_command,
    is_time_command,
    is_vision_command,
    is_vision_follow_up,
    normalize_text,
    OllamaClient,
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

    def test_vision_follow_up(self) -> None:
        self.assertTrue(is_vision_follow_up("这个男的是年轻人吗"))
        self.assertTrue(is_vision_follow_up("左边有什么"))
        self.assertFalse(is_vision_follow_up("给我讲一个笑话"))

    def test_conversation_and_application_exit_commands(self) -> None:
        self.assertTrue(is_end_conversation_command("好了，不用了"))
        self.assertTrue(is_end_conversation_command("再见"))
        self.assertFalse(is_exit_command("再见"))
        self.assertTrue(is_exit_command("关闭助手"))
        self.assertFalse(is_exit_command("停止播放音乐"))

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

    def test_requested_snapshot_is_the_frame_used_for_analysis(self) -> None:
        store = CameraFrameStore()
        captured: list[bytes | None] = []
        worker = threading.Thread(
            target=lambda: captured.append(store.request_snapshot(1.0))
        )
        worker.start()

        request_id = 0
        deadline = time.monotonic() + 0.5
        while request_id == 0 and time.monotonic() < deadline:
            request_id = store.status()["snapshot_request_id"]
            time.sleep(0.005)

        self.assertGreater(request_id, 0)
        self.assertTrue(store.status()["snapshot_pending"])
        self.assertTrue(store.submit_snapshot(request_id, b"new jpeg"))
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(captured, [b"new jpeg"])
        self.assertEqual(store.analysis_snapshot(), (request_id, b"new jpeg"))
        self.assertFalse(store.status()["snapshot_pending"])

    def test_snapshot_rejects_an_expired_request_id(self) -> None:
        store = CameraFrameStore()
        self.assertFalse(store.submit_snapshot(99, b"old jpeg"))

    def test_timed_out_snapshot_cannot_arrive_late(self) -> None:
        store = CameraFrameStore()
        self.assertIsNone(store.request_snapshot(0.01))
        request_id = store.status()["snapshot_request_id"]
        self.assertFalse(store.status()["snapshot_pending"])
        self.assertFalse(store.submit_snapshot(request_id, b"late jpeg"))


class OllamaClientTests(unittest.TestCase):
    def setUp(self) -> None:
        config = SimpleNamespace(
            ollama_enabled=True,
            ollama_url="http://127.0.0.1:11434",
            ollama_model="qwen3.5:4b",
            ollama_timeout_seconds=120.0,
            ollama_keep_alive="10m",
            ollama_system_prompt="直接回答",
            conversation_history_turns=2,
        )
        self.client = OllamaClient(config)
        self.client._request = Mock(
            return_value={"message": {"content": "测试回答", "thinking": ""}}
        )

    def test_chat_explicitly_disables_thinking(self) -> None:
        self.client.ask("测试问题")
        path, payload = self.client._request.call_args.args
        self.assertEqual(path, "/api/chat")
        self.assertIs(payload["think"], False)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["keep_alive"], "10m")

    def test_vision_explicitly_disables_thinking(self) -> None:
        self.client.ask_vision("看到了什么", b"jpeg")
        path, payload = self.client._request.call_args.args
        self.assertEqual(path, "/api/chat")
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["messages"][-1]["images"], ["anBlZw=="])

    def test_warm_up_loads_model_without_thinking(self) -> None:
        self.client.warm_up()
        path, payload = self.client._request.call_args.args
        self.assertEqual(path, "/api/generate")
        self.assertEqual(payload["prompt"], "")
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["keep_alive"], "10m")

    def test_history_is_limited_to_configured_turns(self) -> None:
        for index in range(3):
            self.client.remember(f"问题{index}", f"回答{index}")
        self.assertEqual(len(self.client.history), 4)
        self.assertEqual(self.client.history[0]["content"], "问题1")

    def test_new_conversation_clears_old_context(self) -> None:
        self.client.remember("旧问题", "旧回答")
        self.client.start_conversation()
        self.assertEqual(self.client.history, [])


if __name__ == "__main__":
    unittest.main()
