import asyncio
from datetime import datetime
from pathlib import Path
import queue
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from voice_assistant import (
    build_proxy_opener,
    chinese_number,
    CameraFrameStore,
    contains_wake_phrase,
    DesktopTools,
    format_time_zh,
    is_end_conversation_command,
    is_desktop_command,
    is_desktop_follow_up,
    is_exit_command,
    interruption_action,
    is_stop_speaking_command,
    is_time_command,
    is_rain_question,
    is_weather_command,
    is_weather_follow_up,
    is_vision_command,
    is_vision_follow_up,
    ModelRouter,
    normalize_text,
    OllamaClient,
    OnlineQwenClient,
    OnlineSearchTools,
    parse_weather_query,
    resample_pcm,
    Speaker,
    take_speech_segments,
    strip_code_fence,
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

    def test_desktop_command_detection(self) -> None:
        self.assertTrue(is_desktop_command("打开计算器"))
        self.assertTrue(is_desktop_command("打开记事本，然后写几行代码"))
        self.assertFalse(is_desktop_command("解释一下计算器的原理"))
        self.assertTrue(is_desktop_follow_up("保存到桌面"))
        self.assertTrue(is_desktop_follow_up("打开运行"))
        self.assertEqual(strip_code_fence("```python\nprint('ok')\n```"), "print('ok')")

    def test_weather_command_and_query(self) -> None:
        self.assertTrue(is_weather_command("帮我查询天气预报"))
        self.assertTrue(is_weather_command("明天会不会下雨"))
        self.assertTrue(is_weather_command("今天要带伞吗"))
        self.assertTrue(is_rain_question("今天会不会下雨"))
        self.assertFalse(is_rain_question("今天多少度"))
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
        self.assertEqual(
            parse_weather_query("查 询 成 都 天 气", "北京"),
            ("成都", [0]),
        )
        self.assertEqual(
            parse_weather_query("查 询 天 气 预 报", "成都"),
            ("成都", [0]),
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


class SpeakerTests(unittest.TestCase):
    def test_synthesis_uses_configured_proxy(self) -> None:
        class FakeCommunicate:
            async def stream(self):
                yield {"type": "audio", "data": b"audio"}

        config = SimpleNamespace(
            tts_voice="zh-CN-YunyangNeural",
            tts_rate="+25%",
            tts_volume="+0%",
            tts_proxy="http://127.0.0.1:52351",
        )
        speaker = Speaker(SimpleNamespace(), config)
        with patch(
            "voice_assistant.edge_tts.Communicate", return_value=FakeCommunicate()
        ) as communicate:
            audio = asyncio.run(speaker._synthesize("代理测试"))
        self.assertEqual(audio, b"audio")
        communicate.assert_called_once_with(
            "代理测试",
            "zh-CN-YunyangNeural",
            rate="+25%",
            volume="+0%",
            proxy="http://127.0.0.1:52351",
        )


class ProxyTests(unittest.TestCase):
    def test_proxy_opener_configures_http_and_https(self) -> None:
        handler = Mock()
        with (
            patch("voice_assistant.urllib.request.ProxyHandler", return_value=handler) as factory,
            patch("voice_assistant.urllib.request.build_opener") as build_opener,
        ):
            build_proxy_opener("http://127.0.0.1:52351")
        factory.assert_called_once_with(
            {
                "http": "http://127.0.0.1:52351",
                "https": "http://127.0.0.1:52351",
            }
        )
        build_opener.assert_called_once_with(handler)


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


class OnlineQwenClientTests(unittest.TestCase):
    def setUp(self) -> None:
        config = SimpleNamespace(
            online_api_base_url="https://example.test/v1",
            online_api_key="test-key",
            online_api_key_env="",
            online_config_db=None,
            online_model="qwen3.8-flash",
            online_timeout_seconds=30.0,
            ollama_system_prompt="直接回答",
            conversation_history_turns=2,
            network_proxy="http://127.0.0.1:52351",
        )
        self.client = OnlineQwenClient(config)

    def test_online_chat_streams_without_thinking(self) -> None:
        spoken: list[str] = []
        self.client._stream_request = Mock(
            return_value=[
                {"choices": [{"delta": {"reasoning_content": "不应播报"}}]},
                {"choices": [{"delta": {"content": "第一句。"}}]},
                {"choices": [{"delta": {"content": "第二句"}}]},
            ]
        )
        answer = self.client.ask("测试问题", spoken.append)
        path, payload = self.client._stream_request.call_args.args
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(payload["model"], "qwen3.8-flash")
        self.assertIs(payload["enable_thinking"], False)
        self.assertIs(payload["stream"], True)
        self.assertEqual(answer, "第一句。第二句")
        self.assertEqual(spoken, ["第一句。", "第二句"])

    def test_online_qwen_request_uses_configured_opener(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data": []}'
        self.client._opener = Mock()
        self.client._opener.open.return_value = response
        with patch("voice_assistant.urllib.request.urlopen") as urlopen:
            result = self.client._request("/models")
        self.assertEqual(result, {"data": []})
        self.client._opener.open.assert_called_once()
        urlopen.assert_not_called()

    def test_online_vision_uses_data_url(self) -> None:
        self.client._stream_request = Mock(
            return_value=[{"choices": [{"delta": {"content": "看到了。"}}]}]
        )
        self.client.ask_vision("看到了什么", b"jpeg")
        payload = self.client._stream_request.call_args.args[1]
        user_content = payload["messages"][-1]["content"]
        self.assertEqual(user_content[0]["type"], "text")
        self.assertEqual(user_content[1]["type"], "image_url")
        self.assertEqual(
            user_content[1]["image_url"]["url"], "data:image/jpeg;base64,anBlZw=="
        )


class ModelRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ModelRouter.__new__(ModelRouter)
        self.router.config = SimpleNamespace(
            online_model="qwen3.8-flash",
            ollama_model="qwen3.5:4b",
        )
        self.router._lock = threading.RLock()
        self.router._provider = "online"
        self.router.online = Mock()
        self.router.local = Mock()
        self.router._persist_provider = Mock()

    def test_switches_to_ready_local_model_and_persists_choice(self) -> None:
        self.router.local.check.return_value = (True, "qwen3.5:4b")
        info = self.router.switch("ollama")
        self.assertEqual(info["provider"], "ollama")
        self.assertEqual(info["model"], "qwen3.5:4b")
        self.router.online.end_conversation.assert_called_once()
        self.router.local.end_conversation.assert_called_once()
        self.router._persist_provider.assert_called_once_with("ollama")

    def test_rejects_unavailable_model_without_switching(self) -> None:
        self.router.local.check.return_value = (False, "连接失败")
        with self.assertRaisesRegex(RuntimeError, "连接失败"):
            self.router.switch("ollama")
        self.assertEqual(self.router.info()["provider"], "online")
        self.router._persist_provider.assert_not_called()


class DesktopToolsTests(unittest.TestCase):
    def test_opens_allowlisted_calculator(self) -> None:
        tools = DesktopTools()
        with patch("voice_assistant.subprocess.Popen") as popen:
            answer = tools.execute("请打开计算器")
        popen.assert_called_once_with(["calc.exe"])
        self.assertEqual(answer, "计算器已打开。")

    def test_generates_code_and_opens_it_in_notepad(self) -> None:
        model = Mock()
        model.generate_code.return_value = "```python\nprint('hello')\n```"
        with TemporaryDirectory() as directory:
            tools = DesktopTools(model, Path(directory))
            with patch("voice_assistant.subprocess.Popen") as popen:
                answer = tools.execute("打开记事本，然后写几行代码")
            opened_path = Path(popen.call_args.args[0][1])
            self.assertEqual(opened_path.suffix, ".txt")
            self.assertEqual(tools.current_code_suffix, ".py")
            self.assertEqual(opened_path.read_text(encoding="utf-8"), "print('hello')")
        model.generate_code.assert_called_once_with(
            "使用Python写一个简短的问候程序，并打印当前时间"
        )
        self.assertIn("代码已经写好了", answer)

    def test_writes_dictated_text_without_calling_model(self) -> None:
        model = Mock()
        with TemporaryDirectory() as directory:
            tools = DesktopTools(model, Path(directory))
            with patch("voice_assistant.subprocess.Popen") as popen:
                answer = tools.execute("打开记事本，写入今天下午三点开会")
            opened_path = Path(popen.call_args.args[0][1])
            self.assertEqual(
                opened_path.read_text(encoding="utf-8"), "今天下午三点开会"
            )
        model.generate_code.assert_not_called()
        self.assertIn("内容已经写好了", answer)

    def test_sequential_notepad_save_and_run_workflow(self) -> None:
        model = Mock()
        model.generate_code.return_value = "print(sum(range(1, 101)))"
        with TemporaryDirectory() as notes_directory, TemporaryDirectory() as desktop:
            tools = DesktopTools(model, Path(notes_directory), Path(desktop))
            completed = SimpleNamespace(returncode=0, stdout="5050\n", stderr="")
            with (
                patch("voice_assistant.subprocess.Popen") as popen,
                patch("voice_assistant.subprocess.run", return_value=completed) as run,
            ):
                self.assertEqual(tools.execute("打开记事本"), "记事本已打开。")
                draft_path = tools.current_document
                self.assertIsNotNone(draft_path)

                answer = tools.execute("在记事本写代码")
                self.assertIn("代码已经写好了", answer)
                self.assertEqual(
                    draft_path.read_text(encoding="utf-8"),
                    "print(sum(range(1, 101)))",
                )

                answer = tools.execute("保存到桌面，文件名叫求和程序")
                saved_path = tools.current_document
                self.assertIn("已保存到桌面", answer)
                self.assertEqual(saved_path.parent, Path(desktop))
                self.assertEqual(saved_path.name, "求和程序.py")

                answer = tools.execute("打开运行")
                self.assertEqual(answer, "代码运行成功，输出是：5050。")
                run_command = run.call_args.args[0]
                self.assertEqual(run_command[:2], [sys.executable, "-I"])
                self.assertEqual(run_command[2], str(saved_path))
        model.generate_code.assert_called_once_with(
            "使用Python写一个简短的问候程序，并打印当前时间"
        )

    def test_blocks_generated_python_with_system_access(self) -> None:
        with TemporaryDirectory() as directory:
            tools = DesktopTools(notes_dir=Path(directory))
            tools.current_document = Path(directory) / "unsafe.txt"
            tools.current_document.write_text(
                "import os\nos.system('whoami')", encoding="utf-8"
            )
            tools.current_code_suffix = ".py"
            with self.assertRaisesRegex(RuntimeError, "已阻止运行"):
                tools.execute("运行代码")

    def test_failed_run_can_be_fixed_then_rerun_after_confirmation(self) -> None:
        model = Mock()
        model.generate_code.return_value = "print(missing_name)"
        model.fix_code.return_value = "print('fixed')"
        failed = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="NameError: name 'missing_name' is not defined\n",
        )
        succeeded = SimpleNamespace(returncode=0, stdout="fixed\n", stderr="")
        with TemporaryDirectory() as directory:
            tools = DesktopTools(model, Path(directory), Path(directory))
            with (
                patch("voice_assistant.subprocess.Popen"),
                patch(
                    "voice_assistant.subprocess.run", side_effect=[failed, succeeded]
                ) as run,
            ):
                tools.execute("在记事本写Python代码")
                failed_answer = tools.execute("运行代码")
                self.assertIn("代码运行失败", failed_answer)
                self.assertIn("要我分析并修改吗", failed_answer)
                self.assertTrue(tools.can_handle_follow_up("好的"))

                fixed_answer = tools.execute("好的")
                self.assertIn("代码已经修改", fixed_answer)
                self.assertIn("要重新运行吗", fixed_answer)
                model.fix_code.assert_called_once()

                rerun_answer = tools.execute("确认运行")
                self.assertEqual(rerun_answer, "代码运行成功，输出是：fixed。")
                self.assertEqual(run.call_count, 2)


class OnlineSearchToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        config = SimpleNamespace(
            internet_tools_enabled=True,
            internet_timeout_seconds=10.0,
            internet_retry_count=2,
            network_proxy="http://127.0.0.1:52351",
            weather_default_location="成都",
        )
        self.tools = OnlineSearchTools(config)
        self.forecast = {
            "province": "四川省",
            "city": "成都市",
            "weather": "阴",
            "temperature": 19,
            "feels_like": 22,
            "temp_max": 23,
            "temp_min": 17,
            "humidity": 88,
            "wind_direction": "东北风",
            "wind_power": "1级",
            "aqi_category": "优",
            "hourly_forecast": [
                {
                    "time": "2026-09-14 14:00:00",
                    "weather": "阴",
                    "precip": 0,
                    "pop": 0,
                },
                {
                    "time": "2026-09-14 15:00:00",
                    "weather": "阵雨",
                    "precip": 1,
                    "pop": 80,
                },
                {
                    "time": "2026-09-14 18:00:00",
                    "weather": "阵雨",
                    "precip": 0.5,
                    "pop": 90,
                },
                {
                    "time": "2026-09-15 00:00:00",
                    "weather": "多云",
                    "precip": 0,
                    "pop": 0,
                },
            ],
            "forecast": [
                {
                    "date": "2026-09-14",
                    "temp_max": 23,
                    "temp_min": 17,
                    "weather_day": "阵雨",
                    "weather_night": "阵雨",
                    "pop": 60,
                },
                {
                    "date": "2026-09-15",
                    "temp_max": 24,
                    "temp_min": 18,
                    "weather_day": "多云",
                    "weather_night": "阵雨",
                    "pop": 30,
                },
                {
                    "date": "2026-09-16",
                    "temp_max": 25,
                    "temp_min": 18,
                    "weather_day": "晴",
                    "weather_night": "晴",
                    "pop": 0,
                },
            ],
        }

    def test_current_weather_uses_online_results(self) -> None:
        self.tools._get_json = Mock(return_value=self.forecast)
        answer = self.tools.search_weather("查询天气预报")
        self.assertIn("成都市，四川省", answer)
        self.assertIn("当前19度", answer)
        self.assertIn("湿度88%", answer)
        self.assertIn("空气质量优", answer)
        weather_call = self.tools._get_json.call_args
        self.assertEqual(weather_call.args[0], self.tools.WEATHER_URL)
        self.assertEqual(weather_call.args[1]["city"], "成都")
        self.assertEqual(weather_call.args[1]["hourly"], "true")

    def test_rain_question_answers_probability_and_time_window(self) -> None:
        self.tools._get_json = Mock(return_value=self.forecast)
        answer = self.tools.search_weather("今天会不会下雨")
        self.assertIn("今天会下雨", answer)
        self.assertIn("15点到18点", answer)
        self.assertIn("最高降雨概率90%", answer)
        self.assertIn("建议带伞", answer)
        self.assertNotIn("当前19度", answer)

    def test_no_rain_question_gives_direct_answer(self) -> None:
        dry_forecast = dict(self.forecast)
        dry_forecast["forecast"] = [
            {
                "date": "2026-09-14",
                "weather_day": "晴",
                "weather_night": "多云",
                "precip": 0,
                "pop": 10,
            }
        ]
        dry_forecast["hourly_forecast"] = []
        self.tools._get_json = Mock(return_value=dry_forecast)
        answer = self.tools.search_weather("今天下雨吗")
        self.assertIn("今天大概率不会下雨", answer)
        self.assertIn("最高降雨概率10%", answer)

    def test_disabled_weather_does_not_make_a_network_request(self) -> None:
        self.tools.config.internet_tools_enabled = False
        self.tools._get_json = Mock()
        with self.assertRaisesRegex(RuntimeError, "配置中关闭"):
            self.tools.search_weather("成都天气")
        self.tools._get_json.assert_not_called()

    def test_three_day_forecast(self) -> None:
        self.tools._get_json = Mock(return_value=self.forecast)
        answer = self.tools.search_weather("成都未来三天天气")
        self.assertIn("今天阴", answer)
        self.assertIn("明天多云转阵雨", answer)
        self.assertIn("后天晴", answer)

    def test_unknown_location_is_reported(self) -> None:
        self.tools._get_json = Mock(return_value={"message": "城市不存在"})
        with self.assertRaisesRegex(RuntimeError, "城市不存在"):
            self.tools.search_weather("火星天气")

    def test_domestic_api_requires_only_one_request(self) -> None:
        self.tools._get_json = Mock(return_value=self.forecast)
        answer = self.tools.search_weather("成都天气")
        self.assertIn("成都市，四川省", answer)
        self.assertEqual(self.tools._get_json.call_count, 1)
        self.assertEqual(self.tools._get_json.call_args.args[0], self.tools.WEATHER_URL)

    def test_weather_follow_up_reuses_previous_location(self) -> None:
        self.tools._get_json = Mock(return_value=self.forecast)
        self.tools.search_weather("成都天气")
        answer = self.tools.search_weather("明天呢")
        self.assertIn("明天多云转阵雨", answer)
        self.assertEqual(self.tools._get_json.call_count, 1)
        self.assertEqual(self.tools.last_weather_location, "成都市")

        self.tools.start_conversation()
        self.assertEqual(self.tools.last_weather_location, "")

    def test_recent_weather_is_used_when_domestic_api_briefly_fails(self) -> None:
        self.tools._get_json = Mock(return_value=self.forecast)
        self.tools.search_weather("成都天气")
        cached_at = self.tools._forecast_cache["成都"][0]
        self.tools._get_json = Mock(side_effect=RuntimeError("temporary"))
        with patch("voice_assistant.time.monotonic", return_value=cached_at + 301):
            answer = self.tools.search_weather("成都明天天气")
        self.assertIn("明天多云转阵雨", answer)
        self.assertIn("最近一次成功查询", answer)

    def test_network_request_retries_once_then_succeeds(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true}'
        self.tools._opener = Mock()
        self.tools._opener.open.side_effect = [OSError("temporary failure"), response]
        with patch("voice_assistant.time.sleep") as sleep:
            result = self.tools._get_json("https://example.test", {"q": "weather"})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.tools._opener.open.call_count, 2)
        sleep.assert_called_once()

    def test_domestic_weather_request_uses_configured_opener(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"city": "Chengdu"}'
        self.tools._opener = Mock()
        self.tools._opener.open.return_value = response
        with patch("voice_assistant.urllib.request.urlopen") as urlopen:
            result = self.tools._get_json(self.tools.WEATHER_URL, {"city": "成都"})
        self.assertEqual(result, {"city": "Chengdu"})
        self.tools._opener.open.assert_called_once()
        urlopen.assert_not_called()


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
