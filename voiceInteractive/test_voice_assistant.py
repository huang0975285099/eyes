import asyncio
from dataclasses import replace
from datetime import datetime
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import queue
import re
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import cv2
import voice_assistant as voice_assistant_module

from face_service import FaceDatabase, describe_position, match_face_embeddings
from assistant.face import FaceRecognitionService
from assistant.face.service import mark_target_face
from assistant.gesture import EXPRESSION_MESSAGES, GestureService, classify_expression
from assistant.dashboard import DashboardHandler
from assistant.paths import APP_DIR
from assistant.config import CameraMotionRule, DEFAULT_CONFIG, NativeCameraConfig, load_config
from assistant.native_camera import (
    MotionDetector,
    MotionEventArchive,
    MotionSettings,
    NativeCameraMonitor,
)
from assistant.platform_utils import audio_device_options, find_audio_device

from voice_assistant import (
    build_accent_aware_question,
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
    is_person_identity_query,
    interruption_action,
    is_stop_speaking_command,
    is_time_command,
    is_rain_question,
    is_weather_command,
    is_weather_follow_up,
    is_vision_command,
    is_vision_follow_up,
    vision_camera_hint,
    is_person_location_query,
    ModelRouter,
    normalize_text,
    OllamaClient,
    OnlineQwenClient,
    OnlineSearchTools,
    parse_weather_query,
    parse_person_presence,
    PersonPresenceMonitor,
    PreparedAudio,
    recognition_alternatives,
    resample_pcm,
    select_actionable_recognition,
    Speaker,
    take_speech_segments,
    strip_code_fence,
    VoiceAssistant,
    YoloPersonDetector,
)


class TextTests(unittest.TestCase):
    def test_compatibility_entry_keeps_previous_private_helpers(self) -> None:
        for name in (
            "_host_api_name",
            "_safe_extract_zip",
            "_recognizer",
            "_result_text",
        ):
            self.assertTrue(hasattr(voice_assistant_module, name), name)

    def test_face_service_compatibility_entry_uses_packaged_implementation(self) -> None:
        self.assertEqual(FaceRecognitionService.__module__, "assistant.face.service")

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

    def test_person_location_query(self) -> None:
        self.assertTrue(is_person_location_query("王二在哪里"))
        self.assertTrue(is_person_location_query("张三在什么位置"))
        self.assertFalse(is_person_location_query("王二是谁"))


    def test_accented_recognition_uses_actionable_alternative(self) -> None:
        payload = json.dumps(
            {
                "alternatives": [
                    {"text": "打开寄事本", "confidence": 120.0},
                    {"text": "打开记事本", "confidence": 118.0},
                    {"text": "打开记事簿", "confidence": 115.0},
                ]
            },
            ensure_ascii=False,
        )
        candidates = recognition_alternatives(payload)
        self.assertEqual(candidates, ["打开寄事本", "打开记事本", "打开记事簿"])
        self.assertEqual(
            select_actionable_recognition(
                candidates, ("老 叶 老 叶", "老爷 老爷"), "command"
            ),
            "打开记事本",
        )
        self.assertEqual(
            select_actionable_recognition(
                ["数据库咋个优化", "数据库怎么优化"], (), "command"
            ),
            "数据库咋个优化",
        )

    def test_accent_candidates_are_added_without_replacing_user_history(self) -> None:
        prompt = build_accent_aware_question(
            "数据库杂个优化", ["数据库杂个优化", "数据库咋个优化"]
        )
        self.assertIn("带四川口音的普通话", prompt)
        self.assertIn("数据库咋个优化", prompt)
        self.assertIn("不要提及识别过程", prompt)

    def test_desktop_command_detection(self) -> None:
        self.assertTrue(is_desktop_command("打开计算器"))
        self.assertTrue(is_desktop_command("打开记事本，然后写几行代码"))
        self.assertFalse(is_desktop_command("解释一下计算器的原理"))
        self.assertTrue(is_desktop_follow_up("保存到桌面"))
        self.assertTrue(is_desktop_follow_up("打开运行"))
        self.assertEqual(strip_code_fence("```python\nprint('ok')\n```"), "print('ok')")

    def test_person_presence_result_parser(self) -> None:
        self.assertTrue(parse_person_presence("PERSON"))
        self.assertFalse(parse_person_presence("EMPTY"))
        with self.assertRaisesRegex(RuntimeError, "无法识别"):
            parse_person_presence("不确定")

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
        self.assertTrue(is_vision_command("画面里面的人是谁？"))
        self.assertFalse(is_vision_command("讲一个笑话"))

    def test_identity_question_uses_local_match_path(self) -> None:
        self.assertTrue(is_person_identity_query("画面里面的人是谁？"))
        self.assertTrue(is_person_identity_query("摄像头里有谁"))
        self.assertTrue(is_person_identity_query("这个人是谁"))
        self.assertFalse(is_person_identity_query("你看到了什么"))
        self.assertFalse(is_person_identity_query("谁是中国总统"))

    def test_vision_follow_up(self) -> None:
        self.assertTrue(is_vision_follow_up("这个男的是年轻人吗"))
        self.assertTrue(is_vision_follow_up("左边有什么"))
        self.assertFalse(is_vision_follow_up("给我讲一个笑话"))

    def test_vision_camera_hint(self) -> None:
        self.assertEqual(vision_camera_hint("右眼 看见了 什么"), "right")
        self.assertEqual(vision_camera_hint("右眼看到什么"), "right")
        self.assertIsNone(vision_camera_hint("左眼 看见了 什么"))
        self.assertIsNone(vision_camera_hint("你 看到了 什么"))
        self.assertIsNone(vision_camera_hint("给我讲一个笑话"))
        # 右眼问句同时也是视觉指令，左眼问句不应指向辅助摄像头。
        self.assertTrue(is_vision_command("左眼 看见了 什么"))
        self.assertTrue(is_vision_command("右眼 看见了 什么"))

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


class FaceDatabaseTests(unittest.TestCase):
    def test_video_list_playback_range_and_delete_keep_face_samples(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = FaceDatabase(Path(temp_dir) / "faces.db")
            person = database.add_person("王二")
            database.add_sample(
                person["id"], np.array([0.6, 0.8], dtype=np.float32), b"jpeg bytes", 0.95, 88.0
            )
            video_dir = database.photos_dir / person["id"] / "videos"
            video_dir.mkdir(parents=True)
            filename = "20260916-120000-a1b2c3d4.webm"
            (video_dir / filename).write_bytes(b"0123456789")
            self.assertEqual(database.list_videos(person["id"])[0]["filename"], filename)
            self.assertIsNone(database.video_path(person["id"], "../faces.db"))
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.daemon_threads = True
            server.face_service = SimpleNamespace(database=database)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}/api/people/{person['id']}/videos"
            try:
                with urlopen(base) as response:
                    self.assertEqual(len(json.load(response)["videos"]), 1)
                with urlopen(Request(f"{base}/{filename}", headers={"Range": "bytes=2-5"})) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
                    self.assertEqual(response.read(), b"2345")
                with urlopen(Request(f"{base}/{filename}", method="HEAD")) as response:
                    self.assertEqual(response.headers["Content-Length"], "10")
                with self.assertRaises(HTTPError) as invalid_range:
                    urlopen(Request(f"{base}/{filename}", headers={"Range": "bytes=99-100"}))
                self.assertEqual(invalid_range.exception.code, 416)
                with urlopen(Request(f"{base}/{filename}", method="DELETE")) as response:
                    self.assertTrue(json.load(response)["ok"])
                self.assertEqual(database.list_videos(person["id"]), [])
                self.assertEqual(database.list_people()[0]["sample_count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
                database.close()

    def test_person_samples_resolution_and_delete(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = FaceDatabase(Path(temp_dir) / "faces.db")
            try:
                person = database.add_person("王二", ["小王", "王师傅"])
                self.assertEqual(database.resolve_person("小王在哪")["id"], person["id"])
                vector = np.array([0.6, 0.8], dtype=np.float32)
                sample_id = database.add_sample(
                    person["id"], vector, b"jpeg bytes", 0.95, 88.0
                )
                self.assertGreater(sample_id, 0)
                self.assertEqual(database.list_people()[0]["sample_count"], 1)
                stored = database.embeddings()[0][2]
                np.testing.assert_allclose(stored, vector)
                self.assertTrue(database.delete_person(person["id"]))
                self.assertEqual(database.list_people(), [])
            finally:
                database.close()

    def test_duplicate_person_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = FaceDatabase(Path(temp_dir) / "faces.db")
            try:
                database.add_person("张三")
                with self.assertRaisesRegex(ValueError, "已经存在"):
                    database.add_person("张三")
            finally:
                database.close()

    def test_aliases_cannot_resolve_to_two_people(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = FaceDatabase(Path(temp_dir) / "faces.db")
            try:
                database.add_person("王二", ["王师傅"])
                with self.assertRaisesRegex(ValueError, "已经被其他人员使用"):
                    database.add_person("张三", ["王师傅"])
                with self.assertRaisesRegex(ValueError, "已经被其他人员使用"):
                    database.add_person("王师傅")
                with self.assertRaisesRegex(ValueError, "已经存在"):
                    database.add_person("王 二")
            finally:
                database.close()

    def test_position_description(self) -> None:
        self.assertIn("左侧", describe_position([20, 20, 100, 100], 1000, 600))
        self.assertIn("中间", describe_position([450, 20, 100, 100], 1000, 600))
        self.assertIn("右侧", describe_position([820, 20, 100, 100], 1000, 600))

    def test_frame_assigns_each_identity_only_once(self) -> None:
        samples = [
            ("a", "张三", np.array([1.0, 0.0], dtype=np.float32)),
            ("b", "李四", np.array([0.0, 1.0], dtype=np.float32)),
        ]
        distinct = match_face_embeddings(
            [
                np.array([1.0, 0.0], dtype=np.float32),
                np.array([0.0, 1.0], dtype=np.float32),
            ],
            samples,
            0.48,
            0.05,
        )
        self.assertEqual([item["person_id"] for item in distinct], ["a", "b"])

        duplicate = match_face_embeddings(
            [
                np.array([1.0, 0.0], dtype=np.float32),
                np.array([0.99, 0.01], dtype=np.float32),
            ],
            samples,
            0.48,
            0.05,
        )
        self.assertEqual(sum(item["person_id"] == "a" for item in duplicate), 1)

    def test_single_bad_sample_does_not_dominate_multiple_samples(self) -> None:
        samples = [
            ("a", "张三", np.array([1.0, 0.0], dtype=np.float32)),
            ("a", "张三", np.array([-1.0, 0.0], dtype=np.float32)),
            ("a", "张三", np.array([-1.0, 0.0], dtype=np.float32)),
        ]
        result = match_face_embeddings(
            [np.array([1.0, 0.0], dtype=np.float32)],
            samples,
            0.48,
            0.05,
        )
        self.assertFalse(result[0]["known"])


class FaceVoiceQueryTests(unittest.TestCase):
    def test_location_query_searches_both_cameras_automatically(self) -> None:
        assistant = object.__new__(VoiceAssistant)
        face_service = Mock()
        face_service.database.resolve_person.return_value = {"id": "person-1", "name": "王二"}
        face_service.identify_frames.side_effect = lambda frames: {
            "face_count": 1, "no_samples": False, "quality_rejected": 0,
            "matches": ([{"person_id": "person-1", "position": "画面左侧",
                         "bbox": [20, 20, 100, 100], "frame_index": 2}]
                        if frames[0] == b"side-1" else []),
        }
        monitor = SimpleNamespace(
            primary_camera_id=lambda: "front", secondary_camera_id=lambda: "side",
            capture_frames=Mock(side_effect=lambda camera_id, count, timeout: (
                [b"front-1", b"front-2", b"front-3"] if camera_id == "front"
                else [b"side-1", b"side-2", b"side-3"]
            )),
        )
        assistant.dashboard = SimpleNamespace(
            face_service=face_service, store=CameraFrameStore(), native_camera=monitor
        )
        assistant.config = SimpleNamespace(camera_snapshot_timeout_seconds=3.0)
        assistant.ollama = SimpleNamespace(describe_target_context=Mock(return_value="站在门旁边"))
        with patch("assistant.core.mark_target_face", return_value=b"marked-side"):
            _, answer = assistant._person_location_response("王二在哪", [])
        self.assertIn("辅助摄像头的画面左侧", answer)
        self.assertIn("站在门旁边", answer)
        self.assertEqual(monitor.capture_frames.call_count, 2)
        self.assertEqual(assistant.dashboard.store.analysis_snapshot()[1], b"marked-side")

    def test_unavailable_second_camera_is_not_reported_as_person_absent(self) -> None:
        assistant = object.__new__(VoiceAssistant)
        face_service = Mock()
        face_service.database.resolve_person.return_value = {"id": "p1", "name": "王二"}
        face_service.identify_frames.return_value = {
            "face_count": 0, "matches": [], "no_samples": False,
            "quality_rejected": 0,
        }
        monitor = SimpleNamespace(
            primary_camera_id=lambda: "front", secondary_camera_id=lambda: "side",
            capture_frames=Mock(side_effect=lambda camera_id, count, timeout: (
                [b"one", b"two", b"three"] if camera_id == "front" else []
            )),
        )
        assistant.dashboard = SimpleNamespace(
            face_service=face_service, store=CameraFrameStore(), native_camera=monitor
        )
        assistant.config = SimpleNamespace(camera_snapshot_timeout_seconds=3.0)
        _, answer = assistant._person_location_response("王二在哪", [])
        self.assertIn("主摄像头未检测到清晰人脸", answer)
        self.assertIn("辅助摄像头暂时取不到画面", answer)
        self.assertNotIn("王二不在", answer)

    def test_location_query_uses_current_frame_and_an_alternative_with_a_known_name(self) -> None:
        assistant = object.__new__(VoiceAssistant)
        face_service = Mock()
        face_service.database.resolve_person.side_effect = lambda text: (
            {"id": "person-1", "name": "王二"} if "王二" in text else None
        )
        face_service.identify_frames.return_value = {
            "face_count": 1, "no_samples": False, "quality_rejected": 0,
            "matches": [{"person_id": "person-1", "position": "画面右侧",
                         "bbox": [30, 20, 80, 80], "frame_index": 2}],
        }
        monitor = SimpleNamespace(
            primary_camera_id=lambda: "front", secondary_camera_id=lambda: None,
            capture_frames=Mock(return_value=[b"frame-1", b"frame-2", b"frame-3"]),
        )
        assistant.dashboard = SimpleNamespace(
            face_service=face_service, store=CameraFrameStore(), native_camera=monitor
        )
        assistant.config = SimpleNamespace(camera_snapshot_timeout_seconds=3.0)
        assistant.ollama = SimpleNamespace(describe_target_context=Mock(return_value="靠近门口"))
        with patch("assistant.core.mark_target_face", return_value=b"marked-frame"):
            result = assistant._person_location_response(
                "王儿在哪", ["王儿在哪", "王二在哪"]
            )
        self.assertEqual(result[0], "王二在哪")
        self.assertIn("王二在主摄像头的画面右侧", result[1])
        self.assertIn("靠近门口", result[1])
        monitor.capture_frames.assert_called_once_with("front", 3, 3.0)
        face_service.identify_frames.assert_called_once_with(
            [b"frame-1", b"frame-2", b"frame-3"]
        )
        assistant.ollama.describe_target_context.assert_called_once_with(b"marked-frame")
        self.assertEqual(assistant.dashboard.store.analysis_snapshot()[1], b"marked-frame")

    def test_location_query_can_use_the_right_camera(self) -> None:
        assistant = object.__new__(VoiceAssistant)
        face_service = Mock()
        face_service.database.resolve_person.return_value = {"id": "person-1", "name": "王二"}
        face_service.identify_frames.return_value = {
            "face_count": 1, "no_samples": False, "quality_rejected": 0,
            "matches": [{"person_id": "person-1", "position": "画面左侧",
                         "bbox": [30, 20, 80, 80], "frame_index": 2}],
        }
        monitor = SimpleNamespace(
            primary_camera_id=lambda: "front", secondary_camera_id=lambda: "side",
            capture_frames=Mock(return_value=[b"right-1", b"right-2", b"right-3"]),
        )
        assistant.dashboard = SimpleNamespace(face_service=face_service, store=Mock(), native_camera=monitor)
        assistant.config = SimpleNamespace(camera_snapshot_timeout_seconds=3.0)
        assistant.ollama = SimpleNamespace(describe_target_context=Mock(return_value="周围参照物不清楚"))
        with patch("assistant.core.mark_target_face", return_value=b"marked-frame"):
            result = assistant._person_location_response("右眼里王二在哪", [])
        self.assertIn("王二在辅助摄像头的画面左侧", result[1])
        self.assertNotIn("周围参照", result[1])
        monitor.capture_frames.assert_called_once_with("side", 3, 3.0)

    def test_location_query_reports_unavailable_camera_without_using_old_results(self) -> None:
        assistant = object.__new__(VoiceAssistant)
        face_service = Mock()
        face_service.database.resolve_person.return_value = {"id": "person-1", "name": "王二"}
        monitor = SimpleNamespace(
            primary_camera_id=lambda: "front", secondary_camera_id=lambda: None,
            capture_frames=Mock(return_value=[]),
        )
        assistant.dashboard = SimpleNamespace(face_service=face_service, store=Mock(), native_camera=monitor)
        assistant.config = SimpleNamespace(camera_snapshot_timeout_seconds=3.0)
        result = assistant._person_location_response("王二在哪", [])
        self.assertIn("主摄像头暂时取不到画面", result[1])
        face_service.identify_frames.assert_not_called()

    def test_location_snapshot_requires_matching_person_id_in_exact_frame(self) -> None:
        service = object.__new__(FaceRecognitionService)
        service.database = SimpleNamespace(
            resolve_person=lambda text: {"id": "person-1", "name": "王二"}
        )
        service.identify_snapshot = Mock(return_value={
            "face_count": 2,
            "matches": [
                {"person_id": "person-2", "name": "王二", "position": "画面左侧"},
                {"person_id": "person-1", "name": "王二", "position": "画面右侧"},
            ],
            "no_samples": False,
        })
        answer = service.answer_location_snapshot("王二在哪", b"current-frame")
        service.identify_snapshot.assert_called_once_with(b"current-frame")
        self.assertEqual(answer, "根据本地人员库，王二在主摄像头的画面右侧。")
        service.identify_snapshot.return_value = {
            "face_count": 1, "matches": [], "no_samples": False,
        }
        self.assertIn("没有可靠匹配", service.answer_location_snapshot("王二在哪", b"next-frame"))

    def test_local_identity_answer_requires_reliable_match(self) -> None:
        matched = {
            "face_count": 2,
            "matches": [{"name": "王二", "position": "画面左侧"}],
            "no_samples": False,
        }
        self.assertIn("王二在画面左侧", VoiceAssistant._local_identity_answer(matched))
        self.assertIn("其他人尚未确认", VoiceAssistant._local_identity_answer(matched))
        unknown = {"face_count": 1, "matches": [], "no_samples": False}
        self.assertIn("未能与本地人员库可靠匹配", VoiceAssistant._local_identity_answer(unknown))

    def test_compound_scene_and_identity_question_keeps_both_answers(self) -> None:
        self.assertTrue(
            VoiceAssistant._asks_scene_and_identity("你看到了什么，画面里面的人是谁？")
        )
        self.assertFalse(VoiceAssistant._asks_scene_and_identity("画面里面的人是谁？"))
        self.assertFalse(VoiceAssistant._asks_scene_and_identity("你看到了什么？"))

    def test_identify_snapshot_uses_exact_frame_even_with_live_switch_off(self) -> None:
        service = object.__new__(FaceRecognitionService)
        service.enabled = False
        service.config = SimpleNamespace(
            face_match_threshold=0.48, face_match_margin=0.05,
            face_min_size=80, face_min_blur=35.0,
        )
        service.database = SimpleNamespace(
            embeddings=lambda: [("person-1", "王二", np.array([1.0, 0.0], dtype=np.float32))]
        )
        service.extractor = SimpleNamespace(
            extract=Mock(return_value={
                "width": 1000,
                "height": 600,
                "faces": [{"embedding": [1.0, 0.0], "bbox": [20, 20, 100, 100]}],
            })
        )
        result = service.identify_snapshot(b"current-frame")
        service.extractor.extract.assert_called_once_with(b"current-frame")
        self.assertEqual(result["face_count"], 1)
        self.assertEqual(result["matches"][0]["person_id"], "person-1")
        self.assertEqual(result["matches"][0]["name"], "王二")
        self.assertIn("左侧", result["matches"][0]["position"])

    def test_identify_snapshot_never_guesses_without_samples(self) -> None:
        service = object.__new__(FaceRecognitionService)
        service.database = SimpleNamespace(embeddings=lambda: [])
        service.extractor = SimpleNamespace(extract=Mock())
        result = service.identify_snapshot(b"current-frame")
        self.assertTrue(result["no_samples"])
        self.assertEqual(result["matches"], [])
        service.extractor.extract.assert_not_called()

    def test_identify_frames_needs_two_votes_including_latest_frame(self) -> None:
        service = object.__new__(FaceRecognitionService)
        known = {"face_count": 1, "no_samples": False, "quality_rejected": 0,
                 "matches": [{"person_id": "p1", "name": "王二", "position": "画面左侧",
                              "bbox": [20, 20, 100, 100]}]}
        unknown = {"face_count": 1, "no_samples": False, "quality_rejected": 0,
                   "matches": []}
        service.identify_snapshot = Mock(side_effect=[known, known, unknown])
        self.assertEqual(service.identify_frames([b"one", b"two", b"three"])["matches"], [])
        service.identify_snapshot.side_effect = [known, unknown, known]
        result = service.identify_frames([b"one", b"two", b"three"])
        self.assertEqual(result["matches"][0]["frame_index"], 2)
        self.assertEqual(result["matches"][0]["confirmations"], 2)

    def test_snapshot_rejects_small_blurry_or_badly_lit_faces(self) -> None:
        service = object.__new__(FaceRecognitionService)
        service.config = SimpleNamespace(
            face_match_threshold=0.48, face_match_margin=0.05,
            face_min_size=80, face_min_blur=35.0,
        )
        service.database = SimpleNamespace(embeddings=lambda: [
            ("p1", "王二", np.array([1.0, 0.0], dtype=np.float32))
        ])
        service.extractor = SimpleNamespace(extract=Mock(return_value={
            "width": 640, "height": 480,
            "faces": [
                {"embedding": [1, 0], "bbox": [20, 20, 30, 30], "blur": 90, "brightness": 120},
                {"embedding": [1, 0], "bbox": [20, 20, 100, 100], "blur": 10, "brightness": 120},
                {"embedding": [1, 0], "bbox": [20, 20, 100, 100], "blur": 90, "brightness": 10},
            ],
        }))
        result = service.identify_snapshot(b"bad-frames")
        self.assertEqual(result["face_count"], 3)
        self.assertEqual(result["quality_rejected"], 3)
        self.assertEqual(result["matches"], [])

    def test_mark_target_face_adds_box_without_name(self) -> None:
        ok, original = cv2.imencode(".jpg", np.full((160, 240, 3), 120, dtype=np.uint8))
        self.assertTrue(ok)
        marked = mark_target_face(original.tobytes(), [50, 40, 90, 80])
        image = cv2.imdecode(np.frombuffer(marked, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertGreater(int(image[36, 90, 1]), int(image[36, 90, 0]) + 60)

    def test_spoken_question_uses_on_demand_primary_frame(self) -> None:
        assistant = object.__new__(VoiceAssistant)
        store = CameraFrameStore()
        monitor = SimpleNamespace(
            primary_camera_id=lambda: "front",
            preview_frame=Mock(return_value=b"exact-current-frame"),
        )
        assistant.dashboard = SimpleNamespace(native_camera=monitor, store=store)
        assistant.config = SimpleNamespace(camera_snapshot_timeout_seconds=3.0)
        frame, error = assistant._request_primary_eye_frame()
        self.assertIsNone(error)
        self.assertEqual(frame, b"exact-current-frame")
        monitor.preview_frame.assert_called_once_with("front", 3.0)
        self.assertEqual(store.analysis_snapshot()[1], frame)

    def test_greeting_enqueue_on_first_arrival_and_respect_cooldown(self) -> None:
        service = object.__new__(FaceRecognitionService)
        service.config = SimpleNamespace(
            face_greeting_enabled=True, face_greeting_cooldown_seconds=300.0
        )
        service._lock = threading.RLock()
        service._greeting_queue = queue.Queue(maxsize=4)
        service._present_ids = set()
        service._greeting_last = {}
        person = {"person_id": "person-1", "name": "王二"}

        with service._lock:
            service._enqueue_greetings([person])
        self.assertEqual(service.consume_greeting(), "王二")
        self.assertIsNone(service.consume_greeting())

        with service._lock:
            service._enqueue_greetings([])  # 离开画面
        with service._lock:
            service._enqueue_greetings([person])  # 冷却期内回来
        self.assertIsNone(service.consume_greeting())

        with service._lock:
            service._enqueue_greetings([])  # 离开画面
        service._greeting_last["person-1"] = time.time() - 301.0
        with service._lock:
            service._enqueue_greetings([person])  # 冷却结束后回来
        self.assertEqual(service.consume_greeting(), "王二")

    def test_greeting_disabled_produces_no_events(self) -> None:
        service = object.__new__(FaceRecognitionService)
        service.config = SimpleNamespace(
            face_greeting_enabled=False, face_greeting_cooldown_seconds=300.0
        )
        service._lock = threading.RLock()
        service._greeting_queue = queue.Queue(maxsize=4)
        service._present_ids = set()
        service._greeting_last = {}
        with service._lock:
            service._enqueue_greetings([{"person_id": "person-1", "name": "王二"}])
        self.assertIsNone(service.consume_greeting())
        self.assertEqual(service._present_ids, {"person-1"})

    def test_consume_face_greeting_joins_multiple_names(self) -> None:
        assistant = object.__new__(VoiceAssistant)
        assistant.dashboard = SimpleNamespace(
            face_service=SimpleNamespace(
                consume_greeting=Mock(side_effect=["王二", "张三", None])
            )
        )
        self.assertEqual(assistant._consume_face_greeting(), "王二和张三回来了。")

        assistant.dashboard = SimpleNamespace(
            face_service=SimpleNamespace(consume_greeting=Mock(side_effect=["王二", None]))
        )
        self.assertEqual(assistant._consume_face_greeting(), "王二回来了。")
        assistant.dashboard = SimpleNamespace(
            face_service=SimpleNamespace(consume_greeting=Mock(return_value=None))
        )
        self.assertIsNone(assistant._consume_face_greeting())
        assistant.dashboard = None
        self.assertIsNone(assistant._consume_face_greeting())

    def test_disabling_face_recognition_clears_pending_greetings(self) -> None:
        service = object.__new__(FaceRecognitionService)
        service._lock = threading.RLock()
        service.enabled = True
        service._results = []
        service._result_time = 0.0
        service._error = ""
        service._recent_candidates = []
        service._present_ids = set()
        service._greeting_queue = queue.Queue(maxsize=4)
        service._greeting_queue.put_nowait("王二")
        service._generation = 0

        service.set_enabled(False)

        self.assertIsNone(service.consume_greeting())
        self.assertEqual(service._generation, 1)

    def test_live_recognition_rejects_low_quality_faces(self) -> None:
        service = object.__new__(FaceRecognitionService)
        service.config = SimpleNamespace(
            face_match_threshold=0.48,
            face_match_margin=0.05,
            face_min_size=80,
            face_min_blur=35.0,
            face_greeting_enabled=False,
            face_greeting_cooldown_seconds=300.0,
        )
        service.database = SimpleNamespace(embeddings=lambda: [
            ("p1", "王二", np.array([1.0, 0.0], dtype=np.float32))
        ])
        service.extractor = SimpleNamespace(extract=Mock(return_value={
            "width": 640,
            "height": 480,
            "faces": [{
                "embedding": [1.0, 0.0],
                "bbox": [20, 20, 30, 30],
                "blur": 10.0,
                "brightness": 10.0,
            }],
        }))
        service.enabled = True
        service._lock = threading.RLock()
        service._busy = True
        service._results = []
        service._result_time = 0.0
        service._status = ""
        service._error = ""
        service._recent_candidates = []
        service._greeting_queue = queue.Queue(maxsize=4)
        service._present_ids = set()
        service._greeting_last = {}
        service._generation = 0
        service._suspended = False

        service._recognize(b"frame", 0)

        self.assertEqual(len(service._results), 1)
        self.assertFalse(service._results[0]["known"])
        self.assertEqual(service._results[0]["name"], "画面质量不足")
        self.assertIsNone(service.consume_greeting())

    def test_gesture_feedback_uses_cancellable_speech(self) -> None:
        assistant = object.__new__(VoiceAssistant)
        assistant.config = SimpleNamespace(camera_frame_max_age_seconds=5.0)
        assistant.playback_cancel = threading.Event()
        assistant.speaker = SimpleNamespace(say=Mock(), chime=Mock())
        gesture_service = SimpleNamespace(
            consume_event=Mock(side_effect=[
                {"type": "gesture", "name": "thumbs_up"}, None
            ])
        )
        store = SimpleNamespace(
            scene_broadcast_enabled=Mock(return_value=False),
            set_assistant_status=Mock(),
        )
        assistant.dashboard = SimpleNamespace(
            gesture_service=gesture_service, store=store
        )
        assistant._play_interruptible = Mock(return_value=None)

        with patch("builtins.print"):
            self.assertTrue(assistant._handle_gesture_events())

        assistant._play_interruptible.assert_called_once()
        args = assistant._play_interruptible.call_args.args
        self.assertIs(args[0], assistant.speaker.say)
        self.assertIs(args[2], assistant.playback_cancel)


class AudioTests(unittest.TestCase):
    def test_dashboard_audio_choices_prefer_wasapi_and_keep_cameras_distinct(self) -> None:
        devices = [
            {"name": "Deli", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
            {"name": "Deli", "hostapi": 1, "max_input_channels": 1, "max_output_channels": 0},
            {"name": "2- Deli", "hostapi": 1, "max_input_channels": 1, "max_output_channels": 0},
        ]
        with (
            patch("assistant.platform_utils.sd.query_devices", return_value=devices),
            patch(
                "assistant.platform_utils.sd.query_hostapis",
                side_effect=lambda index: {
                    "name": "Windows WASAPI" if index == 1 else "MME"
                },
            ),
        ):
            options = audio_device_options("input")
        self.assertEqual([item["name"] for item in options], ["Deli", "2- Deli"])
        self.assertEqual([item["selector"] for item in options], ["Deli", "2- Deli"])

    def test_audio_device_survives_windows_duplicate_number_change(self) -> None:
        devices = [
            {
                "name": "Microphone (3- Deli-1080P-Camera-Audio)",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_low_input_latency": 0.01,
                "default_samplerate": 48000,
            },
            {
                "name": "麦克风 (Deli-1080P-Camera-Audio)",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_low_input_latency": 0.01,
                "default_samplerate": 48000,
            },
        ]
        with (
            patch("assistant.platform_utils.sd.query_devices", return_value=devices),
            patch(
                "assistant.platform_utils.sd.query_hostapis",
                return_value={"name": "Windows WASAPI"},
            ),
        ):
            selected = find_audio_device(
                "Microphone (2- Deli-1080P-Camera-Audio)", "input"
            )
        self.assertEqual(selected.index, 0)
        self.assertIn("3- Deli", selected.name)

    def test_audio_device_matches_localized_name_change(self) -> None:
        # 配置里保存的是中文名，Windows 重插同型号设备后端点变成英文+编号。
        devices = [
            {
                "name": "Microphone (3- Deli-1080P-Camera-Audio)",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_low_input_latency": 0.01,
                "default_samplerate": 48000,
            },
            {
                "name": "麦克风 (Realtek High Definition Audio)",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 0,
                "default_low_input_latency": 0.01,
                "default_samplerate": 48000,
            },
        ]
        with (
            patch("assistant.platform_utils.sd.query_devices", return_value=devices),
            patch(
                "assistant.platform_utils.sd.query_hostapis",
                return_value={"name": "Windows WASAPI"},
            ),
        ):
            selected = find_audio_device(
                "麦克风 (Deli-1080P-Camera-Audio)", "input"
            )
        self.assertEqual(selected.index, 0)
        self.assertIn("Deli-1080P-Camera-Audio", selected.name)

    def test_resample_pcm_shape_and_edges(self) -> None:
        samples = np.array([[0], [100], [200]], dtype=np.int16)
        result = resample_pcm(samples, 3, 6)
        self.assertEqual(result.shape, (6, 1))
        self.assertEqual(int(result[0, 0]), 0)
        self.assertEqual(int(result[-1, 0]), 200)


class SpeakerTests(unittest.TestCase):
    def test_startup_cleanup_removes_only_audio_cache(self) -> None:
        with TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            (cache_dir / "old.mp3").write_bytes(b"audio")
            (cache_dir / "keep.txt").write_text("keep", encoding="utf-8")
            with patch.object(Speaker, "CACHE_DIR", cache_dir):
                self.assertEqual(Speaker.clear_cache(), 1)
            self.assertFalse((cache_dir / "old.mp3").exists())
            self.assertTrue((cache_dir / "keep.txt").exists())

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

    def test_played_audio_cache_is_deleted(self) -> None:
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "speech.mp3"
            cache_path.write_bytes(b"audio")
            prepared = PreparedAudio(
                samples=np.zeros((8, 2), dtype=np.int16),
                cache_path=cache_path,
            )
            speaker = Speaker(
                SimpleNamespace(sample_rate=16000, index=1), SimpleNamespace()
            )
            with patch("voice_assistant.sd.play") as play:
                speaker.play_prepared(prepared)
            play.assert_called_once()
            self.assertFalse(cache_path.exists())

    def test_cancelled_or_failed_audio_cache_is_deleted(self) -> None:
        speaker = Speaker(
            SimpleNamespace(sample_rate=16000, index=1), SimpleNamespace()
        )
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cancelled.mp3"
            cache_path.write_bytes(b"audio")
            prepared = PreparedAudio(
                samples=np.zeros((8, 2), dtype=np.int16),
                cache_path=cache_path,
            )
            cancelled = threading.Event()
            cancelled.set()
            with patch("voice_assistant.sd.play") as play:
                speaker.play_prepared(prepared, cancelled)
            play.assert_not_called()
            self.assertFalse(cache_path.exists())

            failed_path = Path(directory) / "failed.mp3"
            failed_path.write_bytes(b"audio")
            failed = PreparedAudio(
                samples=np.zeros((8, 2), dtype=np.int16),
                cache_path=failed_path,
            )
            with (
                patch("voice_assistant.sd.play", side_effect=RuntimeError("失败")),
                self.assertRaisesRegex(RuntimeError, "失败"),
            ):
                speaker.play_prepared(failed)
            self.assertFalse(failed_path.exists())


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
        store.set_tray_active(True)
        self.assertTrue(store.status()["tray_active"])

    def test_on_demand_analysis_snapshot_uses_exact_frame(self) -> None:
        store = CameraFrameStore()
        snapshot_id = store.record_analysis_snapshot(b"spoken-question-frame")
        self.assertEqual(store.analysis_snapshot(), (snapshot_id, b"spoken-question-frame"))

    def test_shutdown_request(self) -> None:
        store = CameraFrameStore()
        self.assertFalse(store.shutdown_event.is_set())
        store.request_shutdown()
        self.assertTrue(store.shutdown_event.is_set())
        self.assertEqual(store.status()["assistant_status"], "正在关闭语音助手")

    def test_conversations_are_newest_first_and_goodbye_closes_current(self) -> None:
        store = CameraFrameStore()
        first_id = store.start_conversation("老叶老叶", "我在")
        self.assertTrue(store.add_conversation_message("user", "今天会下雨吗"))
        self.assertTrue(store.add_conversation_message("assistant", "今天降雨概率不高。"))
        self.assertTrue(store.end_conversation("再见", "好的，需要时再叫我。"))

        first = store.conversations()["conversations"][0]
        self.assertEqual(first["id"], first_id)
        self.assertFalse(first["active"])
        self.assertEqual(first["end_reason"], "再见")
        self.assertEqual(first["title"], "今天会下雨吗")
        self.assertEqual(first["messages"][0]["text"], "好的，需要时再叫我。")

        second_id = store.start_conversation("老叶老叶", "我在")
        snapshot = store.conversations()
        self.assertEqual(snapshot["active_conversation_id"], second_id)
        self.assertEqual(snapshot["conversations"][0]["id"], second_id)
        self.assertEqual(snapshot["conversations"][1]["id"], first_id)
        self.assertEqual(snapshot["conversations"][0]["messages"][0]["text"], "我在")

    def test_conversation_history_persists_and_closes_after_restart(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "conversations.json"
            store = CameraFrameStore(path)
            store.start_conversation()
            store.add_conversation_message("user", "测试持久化")

            restored = CameraFrameStore(path)
            snapshot = restored.conversations()
            self.assertEqual(len(snapshot["conversations"]), 1)
            self.assertFalse(snapshot["conversations"][0]["active"])
            self.assertEqual(snapshot["conversations"][0]["end_reason"], "程序重启")
            self.assertEqual(snapshot["conversations"][0]["title"], "测试持久化")

    def test_wake_phrase_starts_new_conversation_and_closes_previous(self) -> None:
        store = CameraFrameStore()
        first_id = store.start_conversation()
        second_id = store.start_conversation()
        snapshot = store.conversations()
        self.assertEqual(snapshot["active_conversation_id"], second_id)
        previous = next(item for item in snapshot["conversations"] if item["id"] == first_id)
        self.assertFalse(previous["active"])
        self.assertEqual(previous["end_reason"], "重新唤醒")

    def test_restart_request_uses_distinct_exit_signal(self) -> None:
        store = CameraFrameStore()
        store.request_restart()
        self.assertTrue(store.shutdown_event.is_set())
        self.assertTrue(store.restart_event.is_set())
        self.assertEqual(store.status()["assistant_status"], "正在重启语音助手")

    def test_person_alert_only_fires_on_empty_to_person_transition(self) -> None:
        store = CameraFrameStore()
        self.assertTrue(store.begin_presence_check())
        self.assertFalse(store.finish_presence_check(False, b"empty", "baseline"))
        self.assertIsNone(store.consume_presence_alert())

        self.assertTrue(store.begin_presence_check())
        self.assertTrue(store.finish_presence_check(True, b"person", "motion"))
        event_id = store.consume_presence_alert()
        self.assertEqual(event_id, 1)
        self.assertEqual(store.presence_snapshot(), (1, b"person"))

        self.assertTrue(store.begin_presence_check())
        self.assertFalse(store.finish_presence_check(True, b"same", "motion"))
        self.assertIsNone(store.consume_presence_alert())

    def test_disabling_presence_monitor_cancels_pending_and_stale_results(self) -> None:
        store = CameraFrameStore()
        self.assertTrue(store.begin_presence_check())
        store.set_presence_enabled(False)
        self.assertFalse(store.finish_presence_check(True, b"late", "motion"))
        status = store.status()
        self.assertFalse(status["presence_enabled"])
        self.assertFalse(status["presence_initialized"])
        self.assertEqual(status["presence_status"], "动态人物监测已关闭")
        self.assertFalse(store.begin_presence_check())
        self.assertIsNone(store.consume_presence_alert())

    def test_scene_broadcast_uses_matching_snapshot_and_can_be_disabled(self) -> None:
        store = CameraFrameStore()
        self.assertFalse(store.submit_scene_broadcast(b"closed", 0))
        store.set_scene_broadcast_enabled(True)
        self.assertTrue(store.submit_scene_broadcast(b"scene", 0))
        self.assertFalse(store.submit_scene_broadcast(b"busy", 0))
        self.assertEqual(store.consume_scene_broadcast(), (1, b"scene"))
        self.assertTrue(store.finish_scene_broadcast(1, b"scene", "一名男子走进房间。"))
        self.assertEqual(store.scene_broadcast_snapshot(), (1, b"scene"))
        status = store.status()
        self.assertEqual(status["last_scene_description"], "一名男子走进房间。")
        self.assertEqual(status["scene_broadcast_status"], "画面内容已播报")

        self.assertTrue(store.submit_scene_broadcast(b"late", 0))
        self.assertEqual(store.consume_scene_broadcast(), (2, b"late"))
        store.set_scene_broadcast_enabled(False)
        self.assertFalse(store.finish_scene_broadcast(2, b"late", "不应播报"))
        self.assertEqual(store.status()["scene_broadcast_status"], "动态画面播报已关闭")


class NativeCameraTests(unittest.TestCase):
    def test_on_demand_capture_returns_distinct_frames_without_enabling_monitor(self) -> None:
        class FakeCapture:
            def __init__(self) -> None:
                self.opened = True
                self.counter = 0

            def isOpened(self) -> bool:
                return self.opened

            def read(self):
                self.counter += 1
                return True, np.full(
                    (120, 160, 3), 80 + self.counter % 100, dtype=np.uint8
                )

            def release(self) -> None:
                self.opened = False

        config = replace(load_config(DEFAULT_CONFIG), native_camera_enabled=False)
        monitor = NativeCameraMonitor(
            config, CameraFrameStore(),
            SimpleNamespace(enabled=False), SimpleNamespace(enabled=False),
        )
        with patch.object(monitor, "_open_camera", side_effect=lambda _: FakeCapture()):
            monitor.start()
            try:
                frames = monitor.capture_frames(monitor.primary_camera_id(), 3, 2.0)
                self.assertFalse(monitor.enabled)
            finally:
                monitor.stop()
        self.assertEqual(len(frames), 3)
        self.assertEqual(len(set(frames)), 3)
        self.assertEqual(monitor.status()["cameras"][0]["preview_clients"], 0)

    def test_config_accepts_multiple_native_cameras(self) -> None:
        with TemporaryDirectory() as temporary:
            raw = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            raw["native_cameras"] = [
                {"id": "front", "name": "Front", "index": 0, "primary": True},
                {"id": "side", "name": "Side", "index": 1, "primary": False},
            ]
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(path)
        self.assertEqual([camera.index for camera in config.native_cameras], [0, 1])
        self.assertEqual(config.native_camera_index, 0)

    def test_disabled_second_camera_is_not_started_or_counted(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"native_cameras": [
                {"id": "front", "index": 0, "enabled": True, "primary": True},
                {"id": "side", "index": 1, "enabled": False, "primary": False},
            ]}), encoding="utf-8")
            config = load_config(path)
        monitor = NativeCameraMonitor(
            config, CameraFrameStore(),
            SimpleNamespace(enabled=False), SimpleNamespace(enabled=False),
        )
        self.assertEqual(monitor.status()["configured_count"], 1)
        self.assertTrue(monitor.has_camera("front"))
        self.assertFalse(monitor.has_camera("side"))

    def test_preview_does_not_count_as_active_monitoring(self) -> None:
        config = replace(load_config(DEFAULT_CONFIG), native_camera_enabled=False)
        monitor = NativeCameraMonitor(
            config, CameraFrameStore(),
            SimpleNamespace(enabled=False), SimpleNamespace(enabled=False),
        )
        runtime = next(iter(monitor._cameras.values()))
        runtime.active = True
        status = monitor.status()
        self.assertEqual(status["active_count"], 1)
        self.assertEqual(status["active_monitoring_count"], 0)

    def test_camera_motion_rules_inherit_legacy_defaults_independently(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({
                "person_motion_sensitivity": 62,
                "person_motion_min_area_percent": 0.6,
                "native_cameras": [
                    {"id": "front", "index": 0, "primary": True,
                     "motion": {"sensitivity": 85, "roi": {
                         "x": 0.1, "y": 0.2, "width": 0.4, "height": 0.5,
                     }}},
                    {"id": "side", "index": 1},
                ],
            }), encoding="utf-8")
            config = load_config(path)
        front, side = config.native_cameras
        self.assertEqual(front.motion.sensitivity, 85)
        self.assertEqual(front.motion.roi, (0.1, 0.2, 0.4, 0.5))
        self.assertEqual(side.motion.sensitivity, 62)
        self.assertEqual(side.motion.min_area_percent, 0.6)
        self.assertIsNone(side.motion.roi)

    def test_motion_detector_ignores_changes_outside_roi(self) -> None:
        detector = MotionDetector()
        settings = MotionSettings(70, 0.5, 2, 1.0)
        base = np.zeros((200, 200, 3), dtype=np.uint8)
        roi = (0.0, 0.0, 0.5, 1.0)
        detector.process(base, settings, 100.0, 0.0, roi)
        outside = base.copy()
        cv2.rectangle(outside, (120, 30), (190, 170), (255, 255, 255), -1)
        for moment in (102.0, 103.0, 104.0):
            score, triggered = detector.process(outside, settings, moment, 0.0, roi)
            self.assertEqual(score, 0.0)
            self.assertFalse(triggered)
        inside = base.copy()
        cv2.rectangle(inside, (20, 30), (80, 170), (255, 255, 255), -1)
        self.assertFalse(detector.process(inside, settings, 105.0, 0.0, roi)[1])
        score, triggered = detector.process(inside, settings, 106.0, 0.0, roi)
        self.assertTrue(triggered)
        self.assertGreater(score, 0.5)

    def test_motion_confirmation_uses_elapsed_time_as_well_as_frames(self) -> None:
        detector = MotionDetector()
        settings = MotionSettings(70, 0.5, 2, 1.0, confirmation_seconds=1.5)
        base = np.zeros((120, 160, 3), dtype=np.uint8)
        changed = base.copy()
        cv2.rectangle(changed, (20, 20), (100, 100), (255, 255, 255), -1)
        detector.process(base, settings, 100.0, 0.0)
        self.assertFalse(detector.process(changed, settings, 102.0, 0.0)[1])
        self.assertFalse(detector.process(changed, settings, 102.1, 0.0)[1])
        self.assertTrue(detector.process(changed, settings, 103.6, 0.0)[1])

    def test_each_camera_uses_its_own_rule_and_disabled_rule_emits_no_alert(self) -> None:
        config = replace(
            load_config(DEFAULT_CONFIG),
            native_camera_enabled=True,
            native_cameras=(
                NativeCameraConfig(
                    "front", "Front", 0, True, True,
                    CameraMotionRule(sensitivity=91, roi=(0.1, 0.1, 0.5, 0.5)),
                ),
                NativeCameraConfig(
                    "side", "Side", 1, True, False,
                    CameraMotionRule(enabled=False, sensitivity=20),
                ),
            ),
        )
        monitor = NativeCameraMonitor(
            config, CameraFrameStore(),
            SimpleNamespace(enabled=False), SimpleNamespace(enabled=False),
        )
        front = monitor._cameras["front"]
        side = monitor._cameras["side"]
        front.detector.process = Mock(return_value=(3.0, True))
        side.detector.process = Mock(return_value=(3.0, True))
        monitor.archive.record = Mock(return_value={"camera_id": "front"})
        frame = np.full((90, 160, 3), 120, dtype=np.uint8)

        monitor._process_frame(front, frame, time.monotonic())
        monitor._process_frame(side, frame, time.monotonic())

        self.assertEqual(front.detector.process.call_args.args[1].sensitivity, 91)
        self.assertEqual(front.detector.process.call_args.args[4], (0.1, 0.1, 0.5, 0.5))
        side.detector.process.assert_not_called()
        monitor.archive.record.assert_called_once()
        self.assertEqual(monitor.status()["monitoring_count"], 1)

    def test_updating_one_camera_rule_persists_and_resets_only_that_detector(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"native_cameras": [
                {"id": "front", "index": 0, "primary": True},
                {"id": "side", "index": 1},
            ]}), encoding="utf-8")
            config = load_config(path)
            monitor = NativeCameraMonitor(
                config, CameraFrameStore(),
                SimpleNamespace(enabled=False), SimpleNamespace(enabled=False),
            )
            front = monitor._cameras["front"]
            side = monitor._cameras["side"]
            front.detector.background = np.ones((4, 4), dtype=np.uint8)
            side.detector.background = np.ones((4, 4), dtype=np.uint8)
            rule = monitor.update_motion_rule("front", {
                "sensitivity": 80,
                "consecutive_frames": 4,
                "roi": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
            })
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["native_cameras"][0]["motion"], rule)
            self.assertNotIn("motion", stored["native_cameras"][1])
            self.assertEqual(monitor.motion_rule("front")["sensitivity"], 80)
            self.assertEqual(monitor.motion_rule("side")["sensitivity"], 70)
            self.assertIsNone(front.detector.background)
            self.assertIsNotNone(side.detector.background)

    def test_invalid_motion_rule_does_not_change_file_or_runtime(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"native_cameras": [
                {"id": "front", "index": 0, "primary": True},
            ]}), encoding="utf-8")
            monitor = NativeCameraMonitor(
                load_config(path), CameraFrameStore(),
                SimpleNamespace(enabled=False), SimpleNamespace(enabled=False),
            )
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                monitor.update_motion_rule("front", {
                    "roi": {"x": 0.9, "y": 0.0, "width": 0.3, "height": 0.5}
                })
            self.assertEqual(path.read_bytes(), before)
            self.assertIsNone(monitor.motion_rule("front")["roi"])

    def test_motion_rule_endpoint_saves_only_requested_camera(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"native_cameras": [
                {"id": "front", "index": 0, "primary": True},
                {"id": "side", "index": 1},
            ]}), encoding="utf-8")
            monitor = NativeCameraMonitor(
                load_config(path), CameraFrameStore(),
                SimpleNamespace(enabled=False), SimpleNamespace(enabled=False),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.daemon_threads = True
            server.native_camera = monitor
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/motion-rule/side"
                request = Request(
                    url,
                    data=json.dumps({"sensitivity": 91, "roi": None}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request) as response:
                    self.assertEqual(json.load(response)["motion"]["sensitivity"], 91)
                self.assertEqual(monitor.motion_rule("front")["sensitivity"], 70)
                self.assertEqual(monitor.motion_rule("side")["sensitivity"], 91)
                invalid = Request(
                    url,
                    data=json.dumps({"consecutive_frames": 0}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as response:
                    urlopen(invalid)
                self.assertEqual(response.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_sustained_change_triggers_after_warmup(self) -> None:
        detector = MotionDetector()
        settings = MotionSettings(70, 0.5, 3, 1.0)
        base = np.zeros((360, 640, 3), dtype=np.uint8)
        detector.process(base, settings, 100.0, 0.0)
        changed = base.copy()
        cv2.rectangle(changed, (80, 80), (220, 220), (255, 255, 255), -1)
        results = [
            detector.process(changed, settings, 102.0 + index, 0.0)
            for index in range(3)
        ]
        self.assertFalse(results[0][1])
        self.assertFalse(results[1][1])
        self.assertTrue(results[2][1])
        self.assertGreater(results[2][0], 0.5)

    def test_motion_event_archive_rejects_path_traversal(self) -> None:
        with TemporaryDirectory() as temporary:
            with patch("assistant.native_camera.APP_DIR", Path(temporary)):
                archive = MotionEventArchive(7, True)
                event = archive.record(b"jpeg", 2.5)
                filename = str(event["snapshot_url"]).rsplit("/", 1)[-1]
                self.assertIsNotNone(archive.resolve(filename))
                self.assertIsNone(archive.resolve("../faces/faces.db"))

    def test_monitor_runs_one_worker_per_enabled_camera(self) -> None:
        class FakeCapture:
            def __init__(self) -> None:
                self.opened = True

            def isOpened(self) -> bool:
                return self.opened

            def read(self):
                return True, np.full((90, 160, 3), 120, dtype=np.uint8)

            def release(self) -> None:
                self.opened = False

        config = load_config(DEFAULT_CONFIG)
        config = replace(
            config,
            native_camera_enabled=True,
            native_cameras=(
                NativeCameraConfig("front", "Front", 0, True, True),
                NativeCameraConfig("side", "Side", 1, True, False),
            ),
        )
        store = CameraFrameStore()
        monitor = NativeCameraMonitor(
            config,
            store,
            SimpleNamespace(enabled=False),
            SimpleNamespace(enabled=False),
        )
        with patch.object(monitor, "_open_camera", side_effect=lambda _: FakeCapture()):
            monitor.start()
            deadline = time.monotonic() + 1.5
            while (
                monitor.status()["active_count"] < 2
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            status = monitor.status()
            monitor.stop()
        self.assertEqual(status["configured_count"], 2)
        self.assertEqual(status["active_count"], 2)
        self.assertIsNotNone(store.latest_frame(10.0))

    def test_secondary_camera_id_returns_first_non_primary_camera(self) -> None:
        class ClosedCapture:
            def isOpened(self) -> bool:
                return False

            def release(self) -> None:
                pass

        config = load_config(DEFAULT_CONFIG)
        config = replace(
            config,
            native_camera_enabled=True,
            native_cameras=(
                NativeCameraConfig("front", "Front", 0, True, True),
                NativeCameraConfig("side", "Side", 1, True, False),
            ),
        )
        monitor = NativeCameraMonitor(
            config,
            CameraFrameStore(),
            SimpleNamespace(enabled=False),
            SimpleNamespace(enabled=False),
        )
        with patch.object(monitor, "_open_camera", return_value=ClosedCapture()):
            monitor.start()
            self.assertEqual(monitor.primary_camera_id(), "front")
            self.assertEqual(monitor.secondary_camera_id(), "side")
            monitor.stop()

    def test_secondary_camera_id_returns_none_without_secondary_camera(self) -> None:
        class ClosedCapture:
            def isOpened(self) -> bool:
                return False

            def release(self) -> None:
                pass

        config = load_config(DEFAULT_CONFIG)
        config = replace(
            config,
            native_camera_enabled=True,
            native_cameras=(NativeCameraConfig("front", "Front", 0, True, True),),
        )
        monitor = NativeCameraMonitor(
            config,
            CameraFrameStore(),
            SimpleNamespace(enabled=False),
            SimpleNamespace(enabled=False),
        )
        with patch.object(monitor, "_open_camera", return_value=ClosedCapture()):
            monitor.start()
            self.assertIsNone(monitor.secondary_camera_id())
            monitor.stop()

    def test_preview_reads_camera_while_monitoring_is_disabled(self) -> None:
        class FakeCapture:
            def __init__(self) -> None:
                self.opened = True

            def isOpened(self) -> bool:
                return self.opened

            def read(self):
                return True, np.full((90, 160, 3), 120, dtype=np.uint8)

            def release(self) -> None:
                self.opened = False

        config = load_config(DEFAULT_CONFIG)
        config = replace(
            config,
            native_camera_enabled=False,
            native_cameras=(config.native_cameras[0],),
        )
        monitor = NativeCameraMonitor(
            config,
            CameraFrameStore(),
            SimpleNamespace(enabled=False),
            SimpleNamespace(enabled=False),
        )
        with patch.object(monitor, "_open_camera", return_value=FakeCapture()):
            monitor.start()
            frame = monitor.preview_frame(config.native_cameras[0].id, timeout=1.0)
            status = monitor.status()
            monitor.stop()
        self.assertIsNotNone(frame)
        self.assertTrue(frame.startswith(b"\xff\xd8"))
        self.assertFalse(status["enabled"])

    def test_inactive_camera_never_reports_cached_frame_as_connected(self) -> None:
        config = load_config(DEFAULT_CONFIG)
        config = replace(
            config, native_camera_enabled=False,
            native_cameras=(config.native_cameras[0],),
        )
        monitor = NativeCameraMonitor(
            config, CameraFrameStore(),
            SimpleNamespace(enabled=False), SimpleNamespace(enabled=False),
        )
        camera_id = config.native_cameras[0].id
        runtime = monitor._cameras[camera_id]
        runtime.latest_jpeg = b"cached jpeg"
        runtime.last_frame_at = time.time()
        runtime.frame_sequence = 1
        runtime.active = False

        self.assertIsNone(monitor.preview_frame(camera_id, timeout=0.1))
        self.assertEqual(monitor.capture_frames(camera_id, count=1, timeout=0.1), [])

        runtime.active = True
        monitor._release_camera(runtime)
        self.assertIsNone(runtime.latest_jpeg)
        self.assertEqual(runtime.last_frame_at, 0.0)

    def test_preview_does_not_run_background_analysis_when_monitoring_is_off(self) -> None:
        config = load_config(DEFAULT_CONFIG)
        config = replace(
            config,
            native_camera_enabled=False,
            native_cameras=(config.native_cameras[0],),
        )
        presence = SimpleNamespace(enabled=True, submit=Mock())
        face = SimpleNamespace(enabled=True, submit=Mock(), pause=Mock())
        gesture = SimpleNamespace(enabled=True, submit=Mock(), pause=Mock())
        store = CameraFrameStore()
        monitor = NativeCameraMonitor(config, store, presence, face, gesture)
        runtime = next(iter(monitor._cameras.values()))

        self.assertTrue(
            monitor._process_frame(
                runtime, np.full((90, 160, 3), 120, dtype=np.uint8), time.monotonic()
            )
        )

        self.assertIsNotNone(store.latest_frame(10.0))
        presence.submit.assert_not_called()
        face.submit.assert_not_called()
        gesture.submit.assert_not_called()

        monitor.set_enabled(False)
        face.pause.assert_called_once_with()
        gesture.pause.assert_called_once_with()


class PersonPresenceMonitorTests(unittest.TestCase):
    def test_monitor_classifies_frame_in_background(self) -> None:
        store = CameraFrameStore()
        model = Mock()
        model.detect_person.return_value = False
        monitor = PersonPresenceMonitor(store, model)
        self.assertTrue(monitor.submit(b"jpeg", "baseline"))
        deadline = time.monotonic() + 1
        while store.status()["presence_checking"] and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(store.status()["presence_initialized"])
        self.assertFalse(store.status()["person_present"])
        model.detect_person.assert_called_once_with(b"jpeg")

    def test_monitor_can_be_toggled_at_runtime(self) -> None:
        store = CameraFrameStore()
        model = Mock()
        monitor = PersonPresenceMonitor(store, model, enabled=False)
        self.assertFalse(monitor.submit(b"jpeg", "baseline"))
        monitor.set_enabled(True)
        model.detect_person.return_value = False
        self.assertTrue(monitor.submit(b"jpeg", "baseline"))
        deadline = time.monotonic() + 1
        while store.status()["presence_checking"] and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(store.status()["presence_enabled"])
        model.detect_person.assert_called_once_with(b"jpeg")


class YoloPersonDetectorTests(unittest.TestCase):
    def test_detector_sends_jpeg_to_persistent_worker(self) -> None:
        config = SimpleNamespace(
            yolo_python_executable="python",
            yolo_model_path=Path("yolov8n.pt"),
            yolo_device="0",
            yolo_confidence=0.35,
            yolo_image_size=640,
            yolo_timeout_seconds=5.0,
        )
        detector = YoloPersonDetector(config)
        process = Mock()
        process.poll.return_value = None
        process.stdin = Mock()
        detector._process = process
        detector._responses.put(
            {"id": 1, "person": True, "count": 1, "confidence": 0.9}
        )
        self.assertTrue(detector.detect_person(b"jpeg"))
        request = json.loads(process.stdin.write.call_args.args[0])
        self.assertEqual(request["id"], 1)
        self.assertEqual(request["jpeg"], "anBlZw==")
        process.stdin.flush.assert_called_once()


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

    def test_chat_uses_accent_candidates_but_remembers_plain_question(self) -> None:
        self.client.ask(
            "数据库杂个优化",
            recognition_candidates=["数据库杂个优化", "数据库咋个优化"],
        )
        _, payload = self.client._stream_request.call_args.args
        user_prompt = payload["messages"][-1]["content"]
        self.assertIn("数据库咋个优化", user_prompt)
        self.assertIn("不要提及识别过程", user_prompt)
        self.assertEqual(self.client.history[-2]["content"], "数据库杂个优化")

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

    def test_vision_appends_local_identity_without_sending_name_to_model(self) -> None:
        spoken: list[str] = []
        answer = self.client.ask_vision(
            "你看到了什么", b"jpeg", spoken.append,
            local_identity_note="本地人员库识别到王二。",
        )
        payload = self.client._stream_request.call_args.args[1]
        self.assertNotIn("王二", payload["messages"][-1]["content"])
        self.assertTrue(answer.endswith("本地人员库识别到王二。"))
        self.assertIn("本地人员库识别到王二。", spoken)
        self.assertNotIn("王二", self.client.history[-1]["content"])

    def test_person_detection_uses_short_non_thinking_vision_request(self) -> None:
        self.client._stream_request = Mock(
            return_value=[{"message": {"content": "PERSON"}}]
        )
        self.assertTrue(self.client.detect_person(b"jpeg"))
        _, payload = self.client._stream_request.call_args.args
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"]["num_predict"], 8)
        self.assertEqual(payload["messages"][-1]["images"], ["anBlZw=="])

    def test_scene_description_is_short_and_does_not_change_history(self) -> None:
        self.client._stream_request = Mock(
            return_value=[{"message": {"content": "一名男子正在走进房间。"}}]
        )
        answer = self.client.describe_scene(b"jpeg")
        _, payload = self.client._stream_request.call_args.args
        self.assertEqual(answer, "一名男子正在走进房间。")
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"]["num_predict"], 80)
        self.assertEqual(payload["messages"][-1]["images"], ["anBlZw=="])
        self.assertEqual(self.client.history, [])

    def test_target_context_uses_marked_image_without_name_or_history(self) -> None:
        self.client.remember("王二在哪", "王二在画面左侧")
        self.client._request = Mock(return_value={"message": {"content": "在门口旁边。"}})
        answer = self.client.describe_target_context(b"marked")
        path, payload = self.client._request.call_args.args
        self.assertEqual(answer, "在门口旁边。")
        self.assertEqual(path, "/api/chat")
        self.assertNotIn("王二", str(payload["messages"]))
        self.assertEqual(payload["messages"][-1]["images"], ["bWFya2Vk"])
        self.assertEqual(self.client._request.call_args.kwargs["timeout_seconds"], 20.0)
        self.assertEqual(len(self.client.history), 2)

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

    def test_online_vision_does_not_receive_or_remember_local_identity(self) -> None:
        self.client._stream_request = Mock(
            return_value=[{"choices": [{"delta": {"content": "画面里有人。"}}]}]
        )
        answer = self.client.ask_vision(
            "你看到了什么", b"jpeg", local_identity_note="本地人员库识别到王二。"
        )
        payload = self.client._stream_request.call_args.args[1]
        self.assertNotIn("王二", str(payload["messages"]))
        self.assertIn("王二", answer)
        self.assertNotIn("王二", str(self.client.history))

    def test_online_person_detection_uses_short_non_thinking_request(self) -> None:
        self.client._stream_request = Mock(
            return_value=[{"choices": [{"delta": {"content": "EMPTY"}}]}]
        )
        self.assertFalse(self.client.detect_person(b"jpeg"))
        _, payload = self.client._stream_request.call_args.args
        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual(payload["max_tokens"], 8)
        image_url = payload["messages"][-1]["content"][1]["image_url"]["url"]
        self.assertEqual(image_url, "data:image/jpeg;base64,anBlZw==")

    def test_online_scene_description_does_not_change_history(self) -> None:
        self.client._stream_request = Mock(
            return_value=[{"choices": [{"delta": {"content": "桌前坐着一名男子。"}}]}]
        )
        answer = self.client.describe_scene(b"jpeg")
        _, payload = self.client._stream_request.call_args.args
        self.assertEqual(answer, "桌前坐着一名男子。")
        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual(payload["max_tokens"], 80)
        image_url = payload["messages"][-1]["content"][1]["image_url"]["url"]
        self.assertEqual(image_url, "data:image/jpeg;base64,anBlZw==")
        self.assertEqual(self.client.history, [])


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

    def test_named_person_context_stays_local_when_online_model_selected(self) -> None:
        self.router.local.describe_target_context.return_value = "在门口旁边。"
        self.assertEqual(self.router.describe_target_context(b"marked"), "在门口旁边。")
        self.router.local.describe_target_context.assert_called_once_with(b"marked")
        self.router.online.describe_target_context.assert_not_called()


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
    def test_half_duplex_mode_does_not_listen_to_its_own_speaker(self) -> None:
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.config = SimpleNamespace(barge_in_during_playback=False)
        assistant.interrupt_audio_queue = queue.Queue()
        assistant.barge_in_enabled = threading.Event()
        assistant.playback_cancel = threading.Event()
        assistant._interrupt_lock = threading.Lock()
        assistant._interrupt_action = None
        assistant._begin_interruptible_playback()
        self.assertFalse(assistant.barge_in_enabled.is_set())

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


class ExpressionClassificationTests(unittest.TestCase):
    def test_expression_thresholds(self) -> None:
        self.assertIsNone(classify_expression(None))
        self.assertIsNone(classify_expression({}))
        neutral = {"smile": 0.2, "jaw_open": 0.1, "blink": 0.2, "brow_down": 0.2}
        self.assertIsNone(classify_expression(neutral))
        self.assertEqual(classify_expression({"smile": 0.6}), "smile")
        self.assertEqual(
            classify_expression({"jaw_open": 0.7, "blink": 0.4}), "yawn"
        )
        self.assertEqual(classify_expression({"blink": 0.8}), "sleepy")
        self.assertEqual(classify_expression({"brow_down": 0.6}), "frown")

    def test_yawn_wins_over_smile(self) -> None:
        self.assertEqual(
            classify_expression({"smile": 0.7, "jaw_open": 0.8, "blink": 0.5}),
            "yawn",
        )


class GestureServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = replace(
            load_config(DEFAULT_CONFIG),
            gesture_control_enabled=True,
            gesture_confirm_frames=2,
            gesture_action_cooldown_seconds=3.0,
            gesture_expression_enabled=True,
            gesture_expression_cooldown_seconds=90.0,
            gesture_result_max_age_seconds=4.0,
        )
        self.service = GestureService(self.config)

    def test_gesture_requires_confirm_frames(self) -> None:
        self.service._track_gesture("open_palm", 100.0)
        self.assertIsNone(self.service.consume_event())
        self.service._track_gesture("open_palm", 100.5)
        event = self.service.consume_event()
        self.assertEqual(
            event, {"type": "gesture", "name": "open_palm", "at": 100.5}
        )

    def test_gesture_cooldown_blocks_and_rearms(self) -> None:
        self.service._track_gesture("open_palm", 100.0)
        self.service._track_gesture("open_palm", 100.5)
        self.assertIsNotNone(self.service.consume_event())
        # 冷却期内换手势再确认：事件不触发。
        self.service._track_gesture("peace", 101.0)
        self.service._track_gesture("open_palm", 101.5)
        self.service._track_gesture("open_palm", 102.0)
        self.assertIsNone(self.service.consume_event())
        # 冷却结束后再次确认：事件恢复触发。
        self.service._track_gesture("peace", 104.0)
        self.service._track_gesture("open_palm", 104.1)
        self.service._track_gesture("open_palm", 104.2)
        event = self.service.consume_event()
        self.assertIsNotNone(event)
        self.assertEqual(event["name"], "open_palm")

    def test_gesture_held_repeats_after_cooldown(self) -> None:
        # 复现：第一次触发后保持同一手势，冷却期满应再次触发
        # （原版 count == confirm_frames 会在拒绝后卡死，第二次不再播报）。
        base = 100.0
        self.service._track_gesture("peace", base)
        self.service._track_gesture("peace", base + 0.5)
        self.assertIsNotNone(self.service.consume_event())
        # 冷却期（3 秒）内继续确认：不重复触发。
        for step in range(1, 6):
            self.service._track_gesture("peace", base + 0.5 * step)
        self.assertIsNone(self.service.consume_event())
        # 冷却期满后继续确认：再次触发。
        for step in range(6, 10):
            self.service._track_gesture("peace", base + 0.5 * step)
        second = self.service.consume_event()
        self.assertIsNotNone(second)
        self.assertEqual(second["name"], "peace")

    def test_expression_held_repeats_after_cooldown(self) -> None:
        base = 100.0
        self.service._track_expression("smile", base)
        self.service._track_expression("smile", base + 0.5)
        self.assertIsNotNone(self.service.consume_event())
        self.service._track_expression("smile", base + 91.0)
        self.service._track_expression("smile", base + 91.5)
        second = self.service.consume_event()
        self.assertIsNotNone(second)
        self.assertEqual(second["name"], "smile")

    def test_immediate_callback_fires_on_confirm(self) -> None:
        seen: list[str] = []
        self.service.on_gesture_confirmed = seen.append
        self.service._track_gesture("thumbs_up", 100.0)
        self.assertEqual(seen, [])
        self.service._track_gesture("thumbs_up", 100.5)
        self.assertEqual(seen, ["thumbs_up"])

    def test_expression_event_carries_message(self) -> None:
        self.service._track_expression("smile", 100.0)
        self.service._track_expression("smile", 100.5)
        event = self.service.consume_event()
        self.assertIsNotNone(event)
        self.assertEqual(event["type"], "expression")
        self.assertEqual(event["name"], "smile")
        self.assertIn(event["message"], EXPRESSION_MESSAGES["smile"])

    def test_expression_disabled_skips_events(self) -> None:
        service = GestureService(
            replace(self.config, gesture_expression_enabled=False)
        )
        service._track_expression("yawn", 100.0)
        service._track_expression("yawn", 100.5)
        self.assertIsNone(service.consume_event())

    def test_disabling_clears_pending_events(self) -> None:
        self.service._track_gesture("peace", 100.0)
        self.service._track_gesture("peace", 100.5)
        self.service.set_enabled(False)
        self.assertIsNone(self.service.consume_event())

    def test_low_confidence_classification_does_not_trigger(self) -> None:
        service = GestureService(replace(
            self.config, gesture_confirm_frames=1, gesture_min_confidence=0.8
        ))
        service.worker.detect = Mock(return_value={
            "hands": [{"gesture": "open_palm", "gesture_score": 0.1}],
            "expression": None,
        })

        service._process(b"frame", service._generation)

        self.assertIsNone(service.consume_event())
        self.assertIsNone(service.status()["gesture"])

    def test_callback_error_is_contained(self) -> None:
        self.service.on_gesture_confirmed = Mock(
            side_effect=RuntimeError("callback failed")
        )
        self.service._track_gesture("open_palm", 100.0)
        self.service._track_gesture("open_palm", 100.5)
        self.assertEqual(self.service.consume_event()["name"], "open_palm")

    def test_status_expires_stale_results(self) -> None:
        with self.service._lock:
            self.service._gesture = "open_palm"
            self.service._result_time = time.time()
        fresh = self.service.status()
        self.assertEqual(fresh["gesture"], "手掌")
        with self.service._lock:
            self.service._result_time = time.time() - 100.0
        stale = self.service.status()
        self.assertIsNone(stale["gesture"])
        self.assertEqual(stale["hands"], [])


class GestureConfigTests(unittest.TestCase):
    def test_gesture_config_defaults(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            config = load_config(path)
        self.assertFalse(config.gesture_control_enabled)
        self.assertEqual(config.gesture_confirm_frames, 2)
        self.assertAlmostEqual(config.gesture_action_cooldown_seconds, 3.0)
        self.assertAlmostEqual(config.gesture_submit_interval_seconds, 0.5)
        self.assertTrue(config.gesture_expression_enabled)
        self.assertAlmostEqual(config.gesture_expression_cooldown_seconds, 90.0)
        self.assertEqual(
            config.gesture_hand_model_path,
            APP_DIR / "models" / "gesture" / "hand_landmarker.task",
        )
        self.assertEqual(
            config.gesture_face_model_path,
            APP_DIR / "models" / "gesture" / "face_landmarker.task",
        )


class WebFrontendIntegrityTests(unittest.TestCase):
    """index.html 防回归：JS 引用的元素 id、页面/JS 使用的 class 必须真实存在。

    覆盖两类历史事故：JS 引用被删掉的元素 id（运行时报 null）、
    HTML 使用了没有 CSS 定义的 class（如 usage-hint 样式漏加）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        html = (Path(__file__).resolve().parent / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        style_match = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
        script_match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
        if style_match is None or script_match is None:
            raise AssertionError("index.html 缺少 style 或 script 块")
        cls.style_text = style_match.group(1)
        cls.script_text = script_match.group(1)
        cls.markup_text = re.sub(
            r"<script>.*?</script>|<style>.*?</style>", "", html, flags=re.DOTALL
        )
        cls.markup_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', cls.markup_text))
        cls.markup_classes = {
            name
            for chunk in re.findall(r'class="([^"]*)"', cls.markup_text)
            for name in chunk.split()
        }
        cls.css_classes = set(
            re.findall(r"\.([A-Za-z_][A-Za-z0-9_-]*)", cls.style_text)
        )
        cls.js_ids = set(
            re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", cls.script_text)
        ) | set(
            re.findall(r"querySelector(?:All)?\('#([A-Za-z0-9_-]+)", cls.script_text)
        )
        cls.js_classes = set(
            re.findall(r"classList\.(?:add|toggle)\('([^']+)'", cls.script_text)
        ) | set(re.findall(r"className = '([^']+)'", cls.script_text))

    def test_js_element_ids_exist_in_markup(self) -> None:
        missing = self.js_ids - self.markup_ids
        self.assertEqual(missing, set(), f"JS 引用了不存在的元素 id：{missing}")

    def test_markup_classes_have_css_rules(self) -> None:
        missing = self.markup_classes - self.css_classes
        self.assertEqual(missing, set(), f"HTML class 缺少 CSS 定义：{missing}")

    def test_js_dynamic_classes_have_css_rules(self) -> None:
        missing = self.js_classes - self.css_classes
        self.assertEqual(missing, set(), f"JS 动态 class 缺少 CSS 定义：{missing}")

    def test_second_camera_is_counted_only_after_frame_is_ready(self) -> None:
        self.assertRegex(
            self.script_text,
            r"if \(secondaryReady\) \{\s*secondaryStream = selectedSecondaryId;",
        )
        self.assertIn("secondaryCameraPane.hidden = true", self.script_text)

    def test_camera_alert_actions_are_grouped_with_each_camera(self) -> None:
        self.assertEqual(self.markup_text.count('class="motion-run"'), 2)
        self.assertIn('启动变化告警', self.markup_text)
        self.assertIn('关闭变化告警', self.script_text)
        self.assertIn('body: JSON.stringify({ enabled: true })', self.script_text)
        self.assertNotIn('class="motion-enabled"', self.markup_text)


if __name__ == "__main__":
    unittest.main()
