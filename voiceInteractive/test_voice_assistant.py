from datetime import datetime
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from voice_assistant import (
    chinese_number,
    CameraFrameStore,
    contains_wake_phrase,
    format_time_zh,
    is_end_conversation_command,
    is_exit_command,
    interruption_action,
    is_stop_speaking_command,
    is_time_command,
    is_weather_command,
    is_weather_follow_up,
    is_vision_command,
    is_vision_follow_up,
    normalize_text,
    OllamaClient,
    OnlineSearchTools,
    parse_weather_query,
    resample_pcm,
    take_speech_segments,
    VoiceAssistant,
)


class TextTests(unittest.TestCase):
    def test_normalize_and_wake(self) -> None:
        wake_phrases = ("老 叶 老 叶", "老爷 老爷")
        self.assertEqual(normalize_text(" 老叶，老叶！ "), "老叶老叶")
        self.assertTrue(contains_wake_phrase("老 叶 老 叶", wake_phrases))
        self.assertTrue(contains_wake_phrase("老爷 老爷", wake_phrases))
        self.assertFalse(contains_wake_phrase("老叶", wake_phrases))
        self.assertFalse(contains_wake_phrase("小布小布", wake_phrases))

    def test_time_command(self) -> None:
        self.assertTrue(is_time_command("现在 几点 了"))
        self.assertTrue(is_time_command("几点？"))
        self.assertFalse(is_time_command("今天天气怎么样"))

    def test_weather_command_and_query(self) -> None:
        self.assertTrue(is_weather_command("帮我查询天气预报"))
        self.assertTrue(is_weather_command("明天会不会下雨"))
        self.assertFalse(is_weather_command("给我讲一个笑话"))
        self.assertTrue(is_weather_follow_up("明天呢"))
        self.assertFalse(is_weather_follow_up("明天开会"))
        self.assertEqual(
            parse_weather_query("明天北京天气怎么样", "洛杉矶"),
            ("北京", [1]),
        )
        self.assertEqual(
            parse_weather_query("未来三天洛杉矶天气", "北京"),
            ("洛杉矶", [0, 1, 2]),
        )
        self.assertEqual(
            parse_weather_query("给我查一下天气预报", "Los Angeles"),
            ("Los Angeles", [0]),
        )

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
        self.assertTrue(is_stop_speaking_command("停一下"))
        self.assertTrue(is_stop_speaking_command("别说了"))
        self.assertTrue(is_stop_speaking_command("请你停下来吧"))
        self.assertFalse(is_stop_speaking_command("停止助手"))
        self.assertFalse(is_stop_speaking_command("你可以停一下再想"))
        self.assertEqual(
            interruption_action("老叶老叶", ("老 叶 老 叶", "老爷 老爷")),
            "wake",
        )
        self.assertEqual(
            interruption_action("停一下", ("老 叶 老 叶", "老爷 老爷")),
            "stop",
        )
        self.assertEqual(
            interruption_action(
                "老叶老叶，请停一下", ("老 叶 老 叶", "老爷 老爷")
            ),
            "stop",
        )
        self.assertEqual(
            interruption_action("喂，老叶老叶", ("老 叶 老 叶", "老爷 老爷")),
            "wake",
        )
        self.assertIsNone(
            interruption_action(
                "今晚想吃点什么", ("老 叶 老 叶", "老爷 老爷")
            )
        )

    def test_speech_segments_keep_incomplete_suffix(self) -> None:
        segments, remaining = take_speech_segments("第一句。第二句还没说完")
        self.assertEqual(segments, ["第一句。"])
        self.assertEqual(remaining, "第二句还没说完")
        segments, remaining = take_speech_segments(remaining, flush=True)
        self.assertEqual(segments, ["第二句还没说完"])
        self.assertEqual(remaining, "")

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
        self.client._stream_request = Mock(
            return_value=[
                {"message": {"content": "", "thinking": "内部思考"}},
                {"message": {"content": "第一句。"}},
                {"message": {"content": "第二句"}},
                {"done": True, "message": {"content": ""}},
            ]
        )

    def test_chat_explicitly_disables_thinking(self) -> None:
        spoken: list[str] = []
        answer = self.client.ask("测试问题", spoken.append)
        path, payload = self.client._stream_request.call_args.args
        self.assertEqual(path, "/api/chat")
        self.assertIs(payload["think"], False)
        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["keep_alive"], "10m")
        self.assertEqual(answer, "第一句。第二句")
        self.assertEqual(spoken, ["第一句。", "第二句"])

    def test_interrupt_cancels_stream_and_does_not_remember_partial_answer(self) -> None:
        cancel_event = threading.Event()
        spoken: list[str] = []

        def chunks():
            yield {"message": {"content": "第一句。"}}
            yield {"message": {"content": "不应处理的第二句。"}}

        def on_segment(segment: str) -> None:
            spoken.append(segment)
            cancel_event.set()

        self.client._stream_request = Mock(return_value=chunks())
        answer = self.client.ask("测试打断", on_segment, cancel_event)
        self.assertEqual(answer, "第一句。")
        self.assertEqual(spoken, ["第一句。"])
        self.assertEqual(self.client.history, [])

    def test_vision_explicitly_disables_thinking(self) -> None:
        self.client.ask_vision("看到了什么", b"jpeg")
        path, payload = self.client._stream_request.call_args.args
        self.assertEqual(path, "/api/chat")
        self.assertIs(payload["think"], False)
        self.assertIs(payload["stream"], True)
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


class OnlineSearchToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        config = SimpleNamespace(
            internet_tools_enabled=True,
            internet_timeout_seconds=10.0,
            internet_retry_count=2,
            weather_default_location="Los Angeles",
        )
        self.tools = OnlineSearchTools(config)
        self.location = {
            "name": "洛杉矶",
            "admin1": "加利福尼亚州",
            "latitude": 34.05,
            "longitude": -118.24,
        }
        self.forecast = {
            "current": {
                "temperature_2m": 22.4,
                "apparent_temperature": 21.6,
                "weather_code": 1,
                "wind_speed_10m": 8.2,
            },
            "daily": {
                "weather_code": [1, 2, 61],
                "temperature_2m_max": [26.2, 25.1, 21.3],
                "temperature_2m_min": [16.1, 15.8, 14.2],
                "precipitation_probability_max": [5, 15, 70],
            },
        }

    def test_current_weather_uses_online_results(self) -> None:
        self.tools._get_json = Mock(
            side_effect=[{"results": [self.location]}, self.forecast]
        )
        answer = self.tools.search_weather("查询天气预报")
        self.assertIn("洛杉矶，加利福尼亚州", answer)
        self.assertIn("当前22度", answer)
        self.assertIn("降雨概率5%", answer)
        geocoding_call = self.tools._get_json.call_args_list[0]
        self.assertEqual(geocoding_call.args[1]["name"], "Los Angeles")

    def test_disabled_weather_does_not_make_a_network_request(self) -> None:
        self.tools.config.internet_tools_enabled = False
        self.tools._get_json = Mock()
        with self.assertRaisesRegex(RuntimeError, "配置中关闭"):
            self.tools.search_weather("成都天气")
        self.tools._get_json.assert_not_called()

    def test_three_day_forecast(self) -> None:
        self.tools._get_json = Mock(
            side_effect=[{"results": [self.location]}, self.forecast]
        )
        answer = self.tools.search_weather("洛杉矶未来三天天气")
        self.assertIn("今天晴间多云", answer)
        self.assertIn("明天多云", answer)
        self.assertIn("后天有小雨", answer)

    def test_unknown_location_is_reported(self) -> None:
        self.tools._get_json = Mock(return_value={"results": []})
        with self.assertRaisesRegex(RuntimeError, "没有找到地点"):
            self.tools.search_weather("火星天气")

    def test_full_chinese_address_produces_city_fallback(self) -> None:
        candidates = self.tools._location_candidates("中华人民共和国四川省成都市")
        self.assertEqual(candidates[0], "中华人民共和国四川省成都市")
        self.assertIn("成都", candidates)

    def test_known_home_city_skips_geocoding_request(self) -> None:
        self.tools._get_json = Mock(return_value=self.forecast)
        answer = self.tools.search_weather("成都天气")
        self.assertIn("成都，四川", answer)
        self.assertEqual(self.tools._get_json.call_count, 1)
        self.assertEqual(
            self.tools._get_json.call_args.args[0], self.tools.FORECAST_URL
        )

    def test_weather_follow_up_reuses_previous_location(self) -> None:
        self.tools._get_json = Mock(
            side_effect=[
                {"results": [self.location]},
                self.forecast,
                self.forecast,
            ]
        )
        self.tools.search_weather("洛杉矶天气")
        answer = self.tools.search_weather("明天呢")
        self.assertIn("明天多云", answer)
        self.assertEqual(self.tools._get_json.call_count, 3)
        self.assertEqual(self.tools.last_weather_location, "洛杉矶")

        self.tools.start_conversation()
        self.assertEqual(self.tools.last_weather_location, "")

    def test_network_request_retries_once_then_succeeds(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true}'
        with (
            patch(
                "voice_assistant.urllib.request.urlopen",
                side_effect=[OSError("temporary failure"), response],
            ) as urlopen,
            patch("voice_assistant.time.sleep") as sleep,
        ):
            result = self.tools._get_json("https://example.test", {"q": "weather"})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()


class StreamingSpeechTests(unittest.TestCase):
    def test_model_runs_in_worker_but_audio_plays_on_calling_thread(self) -> None:
        caller_thread = threading.current_thread().name
        model_threads: list[str] = []
        preparation_threads: list[str] = []
        playback_threads: list[str] = []
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.audio_queue = queue.Queue()
        assistant.interrupt_audio_queue = queue.Queue()
        assistant.speaking = threading.Event()
        assistant.barge_in_enabled = threading.Event()
        assistant.playback_cancel = threading.Event()
        assistant._interrupt_lock = threading.Lock()
        assistant._interrupt_action = None
        assistant.dashboard = None

        def prepare(text, cancel_event=None):
            preparation_threads.append(threading.current_thread().name)
            return text

        def play_prepared(samples, cancel_event=None):
            playback_threads.append(threading.current_thread().name)

        assistant.speaker = SimpleNamespace(
            prepare=prepare,
            play_prepared=play_prepared,
            chime=lambda success: None,
        )

        def request(on_segment):
            model_threads.append(threading.current_thread().name)
            on_segment("回答。")
            return "回答。"

        answer, interrupt_action = assistant._speak_streamed_answer(request)
        self.assertEqual(answer, "回答。")
        self.assertIsNone(interrupt_action)
        self.assertNotEqual(model_threads, [caller_thread])
        self.assertNotEqual(preparation_threads, [caller_thread])
        self.assertEqual(playback_threads, [caller_thread])

    def test_streamed_segments_are_spoken_in_order(self) -> None:
        spoken: list[str] = []
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.audio_queue = queue.Queue()
        assistant.interrupt_audio_queue = queue.Queue()
        assistant.speaking = threading.Event()
        assistant.barge_in_enabled = threading.Event()
        assistant.playback_cancel = threading.Event()
        assistant._interrupt_lock = threading.Lock()
        assistant._interrupt_action = None
        assistant.dashboard = None
        assistant.speaker = SimpleNamespace(
            prepare=lambda text, cancel_event=None: text,
            play_prepared=lambda text, cancel_event=None: spoken.append(text),
            chime=lambda success: None,
        )

        def request(on_segment):
            on_segment("第一句。")
            on_segment("第二句。")
            return "第一句。第二句。"

        answer, interrupt_action = assistant._speak_streamed_answer(request)
        self.assertEqual(answer, "第一句。第二句。")
        self.assertIsNone(interrupt_action)
        self.assertEqual(spoken, ["第一句。", "第二句。"])

    def test_next_sentence_is_prepared_during_current_playback(self) -> None:
        second_ready = threading.Event()
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.audio_queue = queue.Queue()
        assistant.interrupt_audio_queue = queue.Queue()
        assistant.speaking = threading.Event()
        assistant.barge_in_enabled = threading.Event()
        assistant.playback_cancel = threading.Event()
        assistant._interrupt_lock = threading.Lock()
        assistant._interrupt_action = None
        assistant.dashboard = None

        def prepare(text, cancel_event=None):
            if text == "第二句。":
                second_ready.set()
            return text

        def play_prepared(text, cancel_event=None):
            if text == "第一句。" and not second_ready.wait(1.0):
                raise AssertionError("播放第一句时没有预合成第二句")

        assistant.speaker = SimpleNamespace(
            prepare=prepare,
            play_prepared=play_prepared,
            chime=lambda success: None,
        )

        def request(on_segment):
            on_segment("第一句。")
            on_segment("第二句。")
            return "第一句。第二句。"

        assistant._speak_streamed_answer(request)
        self.assertTrue(second_ready.is_set())

    def test_audio_is_routed_to_interrupt_recognizer_while_speaking(self) -> None:
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.audio_queue = queue.Queue()
        assistant.interrupt_audio_queue = queue.Queue()
        assistant.speaking = threading.Event()
        assistant.barge_in_enabled = threading.Event()
        assistant.speaking.set()
        assistant.barge_in_enabled.set()

        assistant._audio_callback(b"audio", 0, None, None)
        self.assertEqual(assistant.interrupt_audio_queue.get_nowait(), b"audio")
        self.assertTrue(assistant.audio_queue.empty())

    def test_interrupt_stops_remaining_audio_but_keeps_full_answer(self) -> None:
        spoken: list[str] = []
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.audio_queue = queue.Queue()
        assistant.interrupt_audio_queue = queue.Queue()
        assistant.speaking = threading.Event()
        assistant.barge_in_enabled = threading.Event()
        assistant.playback_cancel = threading.Event()
        assistant._interrupt_lock = threading.Lock()
        assistant._interrupt_action = None
        assistant.dashboard = None

        def play_prepared(samples, cancel_event=None):
            spoken.append(samples)
            assistant._set_interrupt_action("stop")
            assistant.playback_cancel.set()

        assistant.speaker = SimpleNamespace(
            prepare=lambda text, cancel_event=None: text,
            play_prepared=play_prepared,
            chime=lambda success: None,
        )

        def request(on_segment):
            on_segment("第一句。")
            if not assistant.playback_cancel.wait(1.0):
                raise AssertionError("播放线程没有触发测试打断")
            on_segment("不会播放的第二句。")
            return "第一句。不会播放的第二句。"

        answer, interrupt_action = assistant._speak_streamed_answer(request)
        self.assertEqual(answer, "第一句。不会播放的第二句。")
        self.assertEqual(interrupt_action, "stop")
        self.assertEqual(spoken, ["第一句。"])


if __name__ == "__main__":
    unittest.main()
