"""语音助手入口。

原有 4000 余行的单文件实现已拆分至 assistant 包（各子模块职责单一），
本文件保留为兼容入口：

- run.ps1 / assistant-host.ps1 直接以本文件为进程入口启动；
- test_voice_assistant.py 从本模块导入符号，并 patch 本模块上的
  sd / edge_tts / urllib.request / subprocess / time 共享模块属性，
  因此这些 import 必须保留在模块顶层。
"""

from __future__ import annotations

import subprocess  # noqa: F401 - 供测试 patch voice_assistant.subprocess.*
import sys
import time  # noqa: F401 - 供测试 patch voice_assistant.time.*
import urllib.request  # noqa: F401 - 供测试 patch voice_assistant.urllib.request.*

import edge_tts  # noqa: F401 - 供测试 patch voice_assistant.edge_tts.Communicate
import sounddevice as sd  # noqa: F401 - 供测试 patch voice_assistant.sd.play

from assistant.camera import CameraFrameStore, PersonPresenceMonitor, YoloPersonDetector
from assistant.config import (
    AudioDevice,
    Config,
    DEFAULT_CONFIG,
    DEFAULT_MODEL_URL,
    load_config,
)
from assistant.core import VoiceAssistant
from assistant.dashboard import (
    CameraDashboard,
    DashboardHandler,
    DashboardHTTPServer,
    DashboardHTTPServerV6,
)
from assistant.llm import ModelRouter, OllamaClient, OnlineQwenClient
from assistant.main import build_parser, main
from assistant.paths import APP_DIR
from assistant.platform_utils import (
    _host_api_name,
    build_proxy_opener,
    configure_windows_console,
    find_audio_device,
    list_audio_devices,
)
from assistant.speaker import PreparedAudio, Speaker
from assistant.textutils import (
    _recognizer,
    _result_text,
    _safe_extract_zip,
    PUNCTUATION_RE,
    SPEECH_BOUNDARY_RE,
    build_accent_aware_question,
    chinese_number,
    contains_wake_phrase,
    ensure_model,
    format_time_zh,
    interruption_action,
    is_desktop_command,
    is_desktop_follow_up,
    is_end_conversation_command,
    is_exit_command,
    is_person_identity_query,
    is_person_location_query,
    is_rain_question,
    is_stop_speaking_command,
    is_time_command,
    is_vision_command,
    is_vision_follow_up,
    is_weather_command,
    is_weather_follow_up,
    normalize_text,
    parse_person_presence,
    parse_weather_query,
    recognition_alternatives,
    resample_pcm,
    select_actionable_recognition,
    strip_code_fence,
    take_speech_segments,
    vision_camera_hint,
)
from assistant.tools import DesktopTools, OnlineSearchTools

__all__ = [
    "APP_DIR",
    "AudioDevice",
    "CameraDashboard",
    "CameraFrameStore",
    "Config",
    "DashboardHandler",
    "DashboardHTTPServer",
    "DashboardHTTPServerV6",
    "DEFAULT_CONFIG",
    "DEFAULT_MODEL_URL",
    "DesktopTools",
    "ModelRouter",
    "OllamaClient",
    "OnlineQwenClient",
    "OnlineSearchTools",
    "PersonPresenceMonitor",
    "PreparedAudio",
    "PUNCTUATION_RE",
    "SPEECH_BOUNDARY_RE",
    "Speaker",
    "VoiceAssistant",
    "YoloPersonDetector",
    "build_accent_aware_question",
    "build_parser",
    "build_proxy_opener",
    "chinese_number",
    "configure_windows_console",
    "contains_wake_phrase",
    "ensure_model",
    "find_audio_device",
    "format_time_zh",
    "interruption_action",
    "is_desktop_command",
    "is_desktop_follow_up",
    "is_end_conversation_command",
    "is_exit_command",
    "is_person_identity_query",
    "is_person_location_query",
    "is_rain_question",
    "is_stop_speaking_command",
    "is_time_command",
    "is_vision_command",
    "is_vision_follow_up",
    "is_weather_command",
    "is_weather_follow_up",
    "list_audio_devices",
    "load_config",
    "main",
    "normalize_text",
    "parse_person_presence",
    "parse_weather_query",
    "recognition_alternatives",
    "resample_pcm",
    "select_actionable_recognition",
    "strip_code_fence",
    "take_speech_segments",
    "vision_camera_hint",
]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已退出。")
        raise SystemExit(0)
    except Exception as error:
        print(f"\n启动失败：{error}", file=sys.stderr)
        raise SystemExit(1)
