from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import ctypes
from dataclasses import dataclass
from datetime import datetime
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode
import urllib.request
import webbrowser
import zipfile

import edge_tts
import miniaudio
import numpy as np
import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.json"
DEFAULT_MODEL_URL = (
    "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
)
PUNCTUATION_RE = re.compile(r"[\s，。！？,.!?、：:；;‘’“”\-]+")
SPEECH_BOUNDARY_RE = re.compile(r"[。！？!?；;\n]")


@dataclass(frozen=True)
class Config:
    input_device: str | int
    output_device: str | int
    wake_phrases: tuple[str, ...]
    command_timeout_seconds: float
    conversation_history_turns: int
    asr_accent_enhancement_enabled: bool
    asr_max_alternatives: int
    tts_voice: str
    tts_rate: str
    tts_volume: str
    tts_proxy: str
    llm_provider: str
    online_api_base_url: str
    online_api_key: str
    online_api_key_env: str
    online_config_db: Path | None
    online_model: str
    online_timeout_seconds: float
    ollama_enabled: bool
    ollama_url: str
    ollama_model: str
    ollama_timeout_seconds: float
    ollama_keep_alive: str
    ollama_system_prompt: str
    internet_tools_enabled: bool
    internet_timeout_seconds: float
    internet_retry_count: int
    network_proxy: str
    weather_default_location: str
    code_run_timeout_seconds: float
    web_enabled: bool
    web_host: str
    web_port: int
    open_browser: bool
    camera_name_keywords: tuple[str, ...]
    camera_frame_max_age_seconds: float
    camera_snapshot_timeout_seconds: float
    person_monitor_enabled: bool
    person_alert_voice: bool
    person_detector: str
    person_motion_sensitivity: int
    person_motion_min_area_percent: float
    person_motion_consecutive_frames: int
    person_motion_cooldown_seconds: float
    scene_broadcast_enabled: bool
    scene_broadcast_cooldown_seconds: float
    yolo_python_executable: str
    yolo_model_path: Path
    yolo_device: str
    yolo_confidence: float
    yolo_image_size: int
    yolo_timeout_seconds: float
    model_path: Path
    model_url: str


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api: str
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class PreparedAudio:
    samples: np.ndarray
    cache_path: Path


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    model_path = Path(raw.get("model_path", "models/vosk-model-small-cn-0.22"))
    if not model_path.is_absolute():
        model_path = APP_DIR / model_path
    online_config_db_value = str(raw.get("online_config_db", "")).strip()
    online_config_db = Path(online_config_db_value) if online_config_db_value else None
    if online_config_db is not None and not online_config_db.is_absolute():
        online_config_db = APP_DIR / online_config_db
    yolo_model_path = Path(raw.get("yolo_model_path", "models/yolov8n.pt"))
    if not yolo_model_path.is_absolute():
        yolo_model_path = APP_DIR / yolo_model_path
    return Config(
        input_device=raw.get("input_device", "Deli-1080P-Camera-Audio"),
        output_device=raw.get("output_device", "Deli-1080P-Camera Audio"),
        wake_phrases=tuple(raw.get("wake_phrases", ["老 叶 老 叶", "老爷 老爷"])),
        command_timeout_seconds=float(raw.get("command_timeout_seconds", 8.0)),
        conversation_history_turns=max(
            1, int(raw.get("conversation_history_turns", 6))
        ),
        asr_accent_enhancement_enabled=bool(
            raw.get("asr_accent_enhancement_enabled", True)
        ),
        asr_max_alternatives=max(
            1, min(10, int(raw.get("asr_max_alternatives", 3)))
        ),
        tts_voice=raw.get("tts_voice", "zh-CN-YunyangNeural"),
        tts_rate=raw.get("tts_rate", "+0%"),
        tts_volume=raw.get("tts_volume", "+0%"),
        tts_proxy=str(raw.get("tts_proxy", "")).strip(),
        llm_provider=str(raw.get("llm_provider", "ollama")).strip().casefold(),
        online_api_base_url=str(raw.get("online_api_base_url", "")).rstrip("/"),
        online_api_key=str(raw.get("online_api_key", "")).strip(),
        online_api_key_env=str(raw.get("online_api_key_env", "QWEN_API_KEY")).strip(),
        online_config_db=online_config_db,
        online_model=str(raw.get("online_model", "qwen3.8-flash")).strip(),
        online_timeout_seconds=float(raw.get("online_timeout_seconds", 120.0)),
        ollama_enabled=bool(raw.get("ollama_enabled", True)),
        ollama_url=str(raw.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/"),
        ollama_model=str(raw.get("ollama_model", "qwen3.5:4b")),
        ollama_timeout_seconds=float(raw.get("ollama_timeout_seconds", 120.0)),
        ollama_keep_alive=str(raw.get("ollama_keep_alive", "10m")),
        ollama_system_prompt=str(
            raw.get(
                "ollama_system_prompt",
                "你是老叶，一个简洁、口语化的中文语音助手。",
            )
        ),
        internet_tools_enabled=bool(raw.get("internet_tools_enabled", True)),
        internet_timeout_seconds=max(
            1.0, float(raw.get("internet_timeout_seconds", 10.0))
        ),
        internet_retry_count=max(0, int(raw.get("internet_retry_count", 2))),
        network_proxy=str(raw.get("network_proxy", "")).strip(),
        weather_default_location=str(
            raw.get("weather_default_location", "Los Angeles")
        ).strip(),
        code_run_timeout_seconds=max(
            1.0, float(raw.get("code_run_timeout_seconds", 10.0))
        ),
        web_enabled=bool(raw.get("web_enabled", True)),
        web_host=str(raw.get("web_host", "127.0.0.1")),
        web_port=int(raw.get("web_port", 8765)),
        open_browser=bool(raw.get("open_browser", True)),
        camera_name_keywords=tuple(
            raw.get("camera_name_keywords", ["Deli", "1080P", "MM101S"])
        ),
        camera_frame_max_age_seconds=float(
            raw.get("camera_frame_max_age_seconds", 6.0)
        ),
        camera_snapshot_timeout_seconds=max(
            0.5, float(raw.get("camera_snapshot_timeout_seconds", 3.0))
        ),
        person_monitor_enabled=bool(raw.get("person_monitor_enabled", True)),
        person_alert_voice=bool(raw.get("person_alert_voice", True)),
        person_detector=str(raw.get("person_detector", "qwen")).strip().casefold(),
        person_motion_sensitivity=max(
            1, min(100, int(raw.get("person_motion_sensitivity", 70)))
        ),
        person_motion_min_area_percent=max(
            0.05, min(50.0, float(raw.get("person_motion_min_area_percent", 0.8)))
        ),
        person_motion_consecutive_frames=max(
            1, min(30, int(raw.get("person_motion_consecutive_frames", 3)))
        ),
        person_motion_cooldown_seconds=max(
            0.5, float(raw.get("person_motion_cooldown_seconds", 5.0))
        ),
        scene_broadcast_enabled=bool(raw.get("scene_broadcast_enabled", False)),
        scene_broadcast_cooldown_seconds=max(
            3.0, float(raw.get("scene_broadcast_cooldown_seconds", 12.0))
        ),
        yolo_python_executable=str(raw.get("yolo_python_executable", "python")).strip(),
        yolo_model_path=yolo_model_path,
        yolo_device=str(raw.get("yolo_device", "0")).strip(),
        yolo_confidence=max(
            0.05, min(0.95, float(raw.get("yolo_confidence", 0.35)))
        ),
        yolo_image_size=max(320, min(1280, int(raw.get("yolo_image_size", 640)))),
        yolo_timeout_seconds=max(
            2.0, float(raw.get("yolo_timeout_seconds", 30.0))
        ),
        model_path=model_path,
        model_url=raw.get("model_url", DEFAULT_MODEL_URL),
    )


def build_proxy_opener(proxy_url: str):
    normalized = str(proxy_url).strip()
    proxy_mapping = {"http": normalized, "https": normalized} if normalized else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxy_mapping))


def configure_windows_console() -> None:
    """Configure UTF-8 and a CJK-capable font in the legacy Windows console."""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

        if os.environ.get("WT_SESSION") or not sys.stdout.isatty():
            return

        class Coord(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class ConsoleFontInfoEx(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("nFont", ctypes.c_ulong),
                ("dwFontSize", Coord),
                ("FontFamily", ctypes.c_uint),
                ("FontWeight", ctypes.c_uint),
                ("FaceName", ctypes.c_wchar * 32),
            ]

        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel32.GetStdHandle(-11)
        info = ConsoleFontInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
            if info.FaceName.casefold() in {
                "consolas",
                "lucida console",
                "terminal",
            }:
                info.FaceName = "NSimSun"
                kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info))
    except Exception:
        # Console setup is cosmetic and must never prevent voice interaction.
        pass


def _host_api_name(index: int) -> str:
    return str(sd.query_hostapis(index)["name"])


def list_audio_devices() -> None:
    print("可用录音设备：")
    for index, device in enumerate(sd.query_devices()):
        if int(device["max_input_channels"]) > 0:
            print(f"  [{index:>2}] {device['name']} ({_host_api_name(device['hostapi'])})")
    print("\n可用播放设备：")
    for index, device in enumerate(sd.query_devices()):
        if int(device["max_output_channels"]) > 0:
            print(f"  [{index:>2}] {device['name']} ({_host_api_name(device['hostapi'])})")


def find_audio_device(selector: str | int, direction: str) -> AudioDevice:
    if direction not in {"input", "output"}:
        raise ValueError(f"未知设备方向：{direction}")
    devices = sd.query_devices()
    channel_key = f"max_{direction}_channels"

    if isinstance(selector, int) or str(selector).strip().isdigit():
        index = int(selector)
        if index < 0 or index >= len(devices):
            raise RuntimeError(f"音频设备编号 {index} 不存在")
        candidates = [(index, devices[index])]
    else:
        needle = str(selector).strip().casefold()
        candidates = [
            (index, device)
            for index, device in enumerate(devices)
            if needle in str(device["name"]).casefold()
        ]

    candidates = [item for item in candidates if int(item[1][channel_key]) > 0]
    if not candidates:
        kind = "录音" if direction == "input" else "播放"
        raise RuntimeError(
            f"找不到匹配 {selector!r} 的{kind}设备。运行 --list-devices 查看设备名。"
        )

    def score(item: tuple[int, object]) -> tuple[int, float]:
        device = item[1]
        api = _host_api_name(int(device["hostapi"]))
        api_score = {
            "Windows WASAPI": 4,
            "Windows DirectSound": 3,
            "MME": 2,
            "Windows WDM-KS": 1,
        }.get(api, 0)
        latency = float(device[f"default_low_{direction}_latency"])
        return api_score, -latency

    index, device = max(candidates, key=score)
    return AudioDevice(
        index=index,
        name=str(device["name"]),
        host_api=_host_api_name(int(device["hostapi"])),
        sample_rate=int(round(float(device["default_samplerate"]))),
        channels=int(device[channel_key]),
    )


def normalize_text(text: str) -> str:
    return PUNCTUATION_RE.sub("", text).casefold()


def take_speech_segments(text: str, flush: bool = False) -> tuple[list[str], str]:
    """Return complete, speakable pieces while keeping an unfinished suffix."""
    segments: list[str] = []
    remaining = text
    while remaining:
        boundary = SPEECH_BOUNDARY_RE.search(remaining)
        if boundary:
            cut = boundary.end()
        elif len(remaining) >= 42:
            comma = max(remaining.rfind("，", 12, 42), remaining.rfind(",", 12, 42))
            cut = comma + 1 if comma >= 0 else 36
        else:
            break
        segment = remaining[:cut].strip()
        remaining = remaining[cut:]
        if segment:
            segments.append(segment)
    if flush and remaining.strip():
        segments.append(remaining.strip())
        remaining = ""
    return segments, remaining


def contains_wake_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    normalized = normalize_text(text)
    return any(normalize_text(phrase) in normalized for phrase in phrases)


def is_time_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in ("现在几点", "几点了", "几点"))


def is_weather_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in (
            "天气",
            "气温",
            "温度",
            "多少度",
            "冷不冷",
            "热不热",
            "下雨",
            "降雨",
            "带伞",
            "会下雨",
            "会不会下雨",
            "天气预报",
        )
    )


def is_rain_question(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in ("下雨", "降雨", "有雨", "带伞"))


def is_desktop_command(text: str) -> bool:
    normalized = normalize_text(text)
    application = any(name in normalized for name in ("计算器", "记事本"))
    action = any(
        verb in normalized for verb in ("打开", "启动", "运行", "写入", "输入", "写")
    )
    return application and action


def is_desktop_follow_up(text: str) -> bool:
    normalized = normalize_text(text)
    return (
        "保存到桌面" in normalized
        or "存到桌面" in normalized
        or "另存到桌面" in normalized
        or "运行代码" in normalized
        or "运行程序" in normalized
        or normalized in {"运行", "打开运行", "执行", "执行代码"}
        or (
            "代码" in normalized
            and any(verb in normalized for verb in ("写", "生成", "输入"))
        )
    )


def strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    match = re.fullmatch(r"```[^\n]*\n([\s\S]*?)\n?```", cleaned)
    return match.group(1).strip() if match else cleaned


def parse_person_presence(text: str) -> bool:
    normalized = normalize_text(text).casefold()
    if normalized in {"person", "有人", "检测到人", "画面有人"}:
        return True
    if normalized in {"empty", "无人", "没有人", "未检测到人", "画面无人"}:
        return False
    raise RuntimeError(f"人物检测返回了无法识别的结果：{text.strip()}")


def is_weather_follow_up(text: str) -> bool:
    normalized = normalize_text(text).removesuffix("呢")
    return normalized in {
        "今天",
        "现在",
        "明天",
        "后天",
        "未来三天",
        "三天",
        "会下雨",
        "会不会下雨",
        "下雨吗",
        "有雨吗",
        "要带伞吗",
        "多少度",
        "冷不冷",
        "热不热",
    }


def parse_weather_query(text: str, default_location: str) -> tuple[str, list[int]]:
    normalized = normalize_text(text)
    if "未来三天" in normalized or "三天天气" in normalized:
        day_indexes = [0, 1, 2]
    elif "后天" in normalized:
        day_indexes = [2]
    elif "明天" in normalized:
        day_indexes = [1]
    else:
        day_indexes = [0]

    # Vosk commonly inserts spaces between Chinese words or even characters.
    # Use normalized text for extraction so "查 询 成 都 天 气" becomes "成都".
    location = normalized
    removable = (
        "帮我",
        "给我",
        "我要",
        "我想",
        "想知道",
        "麻烦",
        "请问",
        "请",
        "查询",
        "查一下",
        "查查",
        "搜索",
        "搜一下",
        "看一下",
        "看看",
        "用",
        "一下",
        "未来三天",
        "三天",
        "今天",
        "明天",
        "后天",
        "现在",
        "当地",
        "天气预报",
        "天气",
        "气温",
        "温度",
        "多少度",
        "冷不冷",
        "热不热",
        "会不会下雨",
        "会下雨吗",
        "下雨吗",
        "降雨",
        "有雨吗",
        "有雨",
        "要不要带伞",
        "要带伞吗",
        "带伞",
        "怎么样",
        "如何",
        "情况",
        "预报",
        "的",
        "吗",
        "呢",
    )
    for phrase in sorted(removable, key=len, reverse=True):
        location = location.replace(phrase, "")
    location = re.sub(r"[\s，。！？,.!?、：:；;]+", " ", location).strip()
    return location or default_location, day_indexes


def is_exit_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in (
            "退出助手",
            "关闭助手",
            "停止助手",
            "退出老叶",
            "关闭老叶",
            "停止老叶",
        )
    )


def is_end_conversation_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in (
            "不用了",
            "没事了",
            "结束对话",
            "结束聊天",
            "休息吧",
            "再见",
        )
    )


def is_stop_speaking_command(text: str) -> bool:
    normalized = normalize_text(text)
    for prefix in ("麻烦你", "麻烦", "请你", "请"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    for suffix in ("可以吗", "好吗", "谢谢", "吧"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized in {
        "停",
        "停下",
        "停下来",
        "停一下",
        "别说了",
        "别讲了",
        "别播了",
        "别说话了",
        "不要说了",
        "不要讲了",
        "停止回答",
        "停止播报",
        "安静",
    }


def interruption_action(text: str, wake_phrases: tuple[str, ...]) -> str | None:
    normalized = normalize_text(text)
    for prefix in ("那个", "喂", "哎"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    for phrase in wake_phrases:
        wake_phrase = normalize_text(phrase)
        if normalized == wake_phrase:
            return "wake"
        if normalized.startswith(wake_phrase):
            remainder = normalized[len(wake_phrase) :]
            if is_stop_speaking_command(remainder):
                return "stop"
            # Calling the wake word during playback always stops the current
            # answer and returns to command listening.
            return "wake"
    if is_stop_speaking_command(text):
        return "stop"
    return None


def is_vision_command(text: str) -> bool:
    normalized = normalize_text(text)
    return ("看到" in normalized and "什么" in normalized) or any(
        phrase in normalized
        for phrase in (
            "你看到了什么",
            "你看到什么",
            "你看见了什么",
            "你看见什么",
            "画面里有什么",
            "摄像头里有什么",
            "看看前面",
            "看一下前面",
            "你能看到什么",
        )
    )


def is_vision_follow_up(text: str) -> bool:
    """Recognize short references that usually point at the last camera answer."""
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in (
            "这个",
            "那个",
            "这位",
            "那位",
            "画面",
            "镜头",
            "左边",
            "右边",
            "前面",
            "后面",
            "穿着",
            "戴着",
            "什么颜色",
            "几个人",
            "多少人",
            "男的",
            "女的",
            "老人",
            "年轻人",
            "小孩",
            "孩子",
            "他是",
            "她是",
            "他们",
            "她们",
        )
    )


def select_actionable_recognition(
    alternatives: list[str], wake_phrases: tuple[str, ...], state: str
) -> str:
    if not alternatives:
        return ""

    def is_actionable(candidate: str) -> bool:
        if state == "waiting":
            return contains_wake_phrase(candidate, wake_phrases)
        return any(
            checker(candidate)
            for checker in (
                is_desktop_command,
                is_time_command,
                is_weather_command,
                is_exit_command,
                is_end_conversation_command,
                is_vision_command,
            )
        )

    if is_actionable(alternatives[0]):
        return alternatives[0]
    return next(
        (candidate for candidate in alternatives[1:] if is_actionable(candidate)),
        alternatives[0],
    )


def build_accent_aware_question(question: str, alternatives: list[str]) -> str:
    unique_alternatives: list[str] = []
    seen = {normalize_text(question)}
    for alternative in alternatives:
        normalized = normalize_text(alternative)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_alternatives.append(alternative.strip())
    if not unique_alternatives:
        return question
    candidates = "；".join(unique_alternatives[:2])
    return (
        f"用户使用带四川口音的普通话。首选识别为：{question}。"
        f"其他识别候选为：{candidates}。"
        "请结合中文语义和对话上下文判断真实问题并直接回答，不要提及识别过程。"
    )


def chinese_number(value: int) -> str:
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    if value < 20:
        return "十" + (digits[value % 10] if value % 10 else "")
    return digits[value // 10] + "十" + (digits[value % 10] if value % 10 else "")


def format_time_zh(now: datetime) -> str:
    if now.hour < 5:
        period = "凌晨"
    elif now.hour < 9:
        period = "早上"
    elif now.hour < 12:
        period = "上午"
    elif now.hour < 14:
        period = "中午"
    elif now.hour < 18:
        period = "下午"
    else:
        period = "晚上"

    hour = now.hour % 12 or 12
    if now.minute == 0:
        minute_text = "整"
    elif now.minute < 10:
        minute_text = f"零{chinese_number(now.minute)}分"
    else:
        minute_text = f"{chinese_number(now.minute)}分"
    return f"现在是{period}{chinese_number(hour)}点{minute_text}。"


def _safe_extract_zip(archive: zipfile.ZipFile, target: Path) -> None:
    target_resolved = target.resolve()
    for member in archive.infolist():
        destination = (target / member.filename).resolve()
        if target_resolved not in destination.parents and destination != target_resolved:
            raise RuntimeError(f"模型压缩包包含不安全路径：{member.filename}")
    archive.extractall(target)


def ensure_model(model_path: Path, model_url: str) -> Path:
    if model_path.is_dir() and (model_path / "conf" / "model.conf").exists():
        return model_path

    model_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path = model_path.parent / f"{model_path.name}.zip.part"
    print(f"首次运行：正在下载离线中文识别模型（约 42 MB）\n{model_url}")

    last_percent = -1

    def show_progress(blocks: int, block_size: int, total: int) -> None:
        nonlocal last_percent
        if total > 0:
            percent = min(100, blocks * block_size * 100 // total)
            if percent != last_percent:
                print(f"\r下载进度：{percent:3d}%", end="", flush=True)
                last_percent = percent

    try:
        urllib.request.urlretrieve(model_url, archive_path, show_progress)
        print("\n正在解压模型……")
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract_zip(archive, model_path.parent)
    finally:
        archive_path.unlink(missing_ok=True)

    if not (model_path / "conf" / "model.conf").exists():
        raise RuntimeError(f"模型解压后未出现在预期目录：{model_path}")
    return model_path


def _recognizer(model: Model, sample_rate: int, phrases: list[str]) -> KaldiRecognizer:
    grammar = json.dumps([*phrases, "[unk]"], ensure_ascii=False)
    return KaldiRecognizer(model, sample_rate, grammar)


def _result_text(payload: str, key: str) -> str:
    try:
        return str(json.loads(payload).get(key, ""))
    except json.JSONDecodeError:
        return ""


def recognition_alternatives(payload: str) -> list[str]:
    try:
        result = json.loads(payload)
    except json.JSONDecodeError:
        return []
    raw_alternatives = result.get("alternatives", [])
    candidates: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_alternatives, list):
        for item in raw_alternatives:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            normalized = normalize_text(text)
            if text and normalized not in seen:
                seen.add(normalized)
                candidates.append(text)
    direct_text = str(result.get("text", "")).strip()
    if direct_text and normalize_text(direct_text) not in seen:
        candidates.insert(0, direct_text)
    return candidates


def resample_pcm(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or samples.size == 0:
        return samples
    frames = samples.shape[0]
    target_frames = max(1, round(frames * target_rate / source_rate))
    source_x = np.arange(frames, dtype=np.float64)
    target_x = np.linspace(0, frames - 1, target_frames)
    if samples.ndim == 1:
        return np.interp(target_x, source_x, samples).astype(samples.dtype)
    channels = [
        np.interp(target_x, source_x, samples[:, channel])
        for channel in range(samples.shape[1])
    ]
    return np.column_stack(channels).astype(samples.dtype)


class Speaker:
    CACHE_DIR = APP_DIR / ".tts-cache"

    def __init__(self, device: AudioDevice, config: Config) -> None:
        self.device = device
        self.config = config

    @classmethod
    def clear_cache(cls) -> int:
        if not cls.CACHE_DIR.is_dir():
            return 0
        removed = 0
        for cache_path in cls.CACHE_DIR.glob("*.mp3"):
            try:
                cache_path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
        return removed

    def chime(self, success: bool = True) -> None:
        duration = 0.23
        sample_rate = self.device.sample_rate
        timeline = np.arange(round(duration * sample_rate)) / sample_rate
        frequency = 880 if success else 330
        wave = np.sin(2 * np.pi * frequency * timeline)
        if success:
            wave += 0.45 * np.sin(2 * np.pi * frequency * 1.5 * timeline)
        envelope = np.exp(-8 * timeline)
        mono = (0.16 * wave * envelope).astype(np.float32)
        stereo = np.column_stack((mono, mono))
        sd.play(stereo, sample_rate, device=self.device.index, blocking=True)

    async def _synthesize(self, text: str) -> bytes:
        communicate = edge_tts.Communicate(
            text,
            self.config.tts_voice,
            rate=self.config.tts_rate,
            volume=self.config.tts_volume,
            proxy=self.config.tts_proxy or None,
        )
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    def _cache_path(self, text: str) -> Path:
        cache_key = "|".join(
            (self.config.tts_voice, self.config.tts_rate, self.config.tts_volume, text)
        )
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        return self.CACHE_DIR / f"{digest}.mp3"

    def prepare(
        self, text: str, cancel_event: threading.Event | None = None
    ) -> PreparedAudio | None:
        if cancel_event is not None and cancel_event.is_set():
            return None
        cache_path = self._cache_path(text)
        try:
            if cache_path.exists():
                audio_bytes = cache_path.read_bytes()
            else:
                audio_bytes = asyncio.run(self._synthesize(text))
                if audio_bytes:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(audio_bytes)
            if not audio_bytes:
                raise RuntimeError("语音合成没有返回音频")
            if cancel_event is not None and cancel_event.is_set():
                cache_path.unlink(missing_ok=True)
                return None
            decoded = miniaudio.decode(
                audio_bytes, output_format=miniaudio.SampleFormat.SIGNED16
            )
            samples = np.asarray(decoded.samples, dtype=np.int16)
            samples = samples.reshape(-1, decoded.nchannels)
            samples = resample_pcm(
                samples, decoded.sample_rate, self.device.sample_rate
            )
            if samples.shape[1] == 1:
                samples = np.repeat(samples, 2, axis=1)
            return PreparedAudio(samples=samples, cache_path=cache_path)
        except Exception:
            cache_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def discard_prepared(prepared: PreparedAudio) -> None:
        prepared.cache_path.unlink(missing_ok=True)

    def play_prepared(
        self,
        prepared: PreparedAudio,
        cancel_event: threading.Event | None = None,
    ) -> None:
        try:
            if cancel_event is not None and cancel_event.is_set():
                return
            sd.play(
                prepared.samples,
                self.device.sample_rate,
                device=self.device.index,
                blocking=True,
            )
        finally:
            self.discard_prepared(prepared)

    def say(self, text: str, cancel_event: threading.Event | None = None) -> None:
        prepared = self.prepare(text, cancel_event)
        if prepared is not None:
            self.play_prepared(prepared, cancel_event)


class OnlineSearchTools:
    WEATHER_URL = "https://uapis.cn/api/v1/misc/weather"
    WEATHER_CACHE_SECONDS = 5 * 60
    WEATHER_STALE_SECONDS = 30 * 60

    def __init__(self, config: Config) -> None:
        self.config = config
        self.last_weather_location = ""
        self._forecast_cache: dict[str, tuple[float, dict]] = {}
        self._opener = build_proxy_opener(getattr(config, "network_proxy", ""))

    def start_conversation(self) -> None:
        self.last_weather_location = ""

    def _get_json(self, url: str, parameters: dict) -> dict:
        request = urllib.request.Request(
            f"{url}?{urlencode(parameters)}",
            headers={
                "User-Agent": "LaoyeVoiceAssistant/1.0",
                "Connection": "close",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.config.internet_retry_count + 1):
            try:
                with self._opener.open(
                    request, timeout=self.config.internet_timeout_seconds
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as error:
                last_error = error
                if attempt < self.config.internet_retry_count:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"请求失败：{last_error}") from last_error

    @staticmethod
    def _number(value) -> int:
        return int(round(float(value)))

    def _get_forecast(self, location: str) -> tuple[dict, bool]:
        cache_key = normalize_text(location)
        cached = self._forecast_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] <= self.WEATHER_CACHE_SECONDS:
            return cached[1], False
        try:
            forecast = self._get_json(
                self.WEATHER_URL,
                {
                    "city": location,
                    "forecast": "true",
                    "extended": "true",
                    "hourly": "true",
                    "lang": "zh",
                },
            )
        except Exception:
            if cached and now - cached[0] <= self.WEATHER_STALE_SECONDS:
                return cached[1], True
            raise
        cache_entry = (now, forecast)
        self._forecast_cache[cache_key] = cache_entry
        canonical_city = normalize_text(str(forecast.get("city", "")))
        if canonical_city:
            self._forecast_cache[canonical_city] = cache_entry
        return forecast, False

    def search_weather(self, question: str) -> str:
        if not self.config.internet_tools_enabled:
            raise RuntimeError("工具已在配置中关闭")
        location_query, day_indexes = parse_weather_query(
            question,
            self.last_weather_location or self.config.weather_default_location,
        )
        if not location_query:
            return "请告诉我需要查询哪个城市的天气。"

        forecast, used_stale_cache = self._get_forecast(location_query)
        city = str(forecast.get("city", location_query)).strip()
        if not city or "weather" not in forecast:
            message = str(forecast.get("message") or forecast.get("error") or "")
            raise RuntimeError(message or f"没有找到地点“{location_query}”")
        self.last_weather_location = city
        future_days = forecast.get("forecast", [])
        day_names = ("今天", "明天", "后天")
        descriptions: list[str] = []
        rain_question = is_rain_question(question)
        for day_index in day_indexes:
            daily = None
            if day_index < len(future_days):
                daily = future_days[day_index]
            if rain_question:
                descriptions.append(
                    self._describe_rain(forecast, daily, day_index, day_names[day_index])
                )
                continue
            if day_index == 0:
                try:
                    condition = str(forecast["weather"])
                    temperature = self._number(forecast["temperature"])
                    apparent = self._number(forecast["feels_like"])
                    high = self._number(forecast["temp_max"])
                    low = self._number(forecast["temp_min"])
                    humidity = self._number(forecast["humidity"])
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError("国内天气服务没有返回完整实况") from error
                wind = (
                    f"{forecast.get('wind_direction', '')}"
                    f"{forecast.get('wind_power', '')}"
                ).strip()
                air_quality = str(forecast.get("aqi_category", "")).strip()
                descriptions.append(
                    f"今天{condition}，当前{temperature}度，体感{apparent}度，"
                    f"最高{high}度，最低{low}度，湿度{humidity}%"
                    + (f"，{wind}" if wind else "")
                    + (f"，空气质量{air_quality}" if air_quality else "")
                )
            else:
                try:
                    if daily is None:
                        raise IndexError
                    high = self._number(daily["temp_max"])
                    low = self._number(daily["temp_min"])
                    rain_probability = self._number(daily["pop"])
                    day_weather = str(daily["weather_day"])
                    night_weather = str(daily["weather_night"])
                except (IndexError, KeyError, TypeError, ValueError) as error:
                    raise RuntimeError("国内天气服务返回的预报不完整") from error
                condition = (
                    day_weather
                    if day_weather == night_weather
                    else f"{day_weather}转{night_weather}"
                )
                descriptions.append(
                    f"{day_names[day_index]}{condition}，最高{high}度，"
                    f"最低{low}度，降雨概率{rain_probability}%"
                )

        province = str(forecast.get("province", "")).strip()
        location_name = city
        if province and province not in city:
            location_name = f"{city}，{province}"
        answer = f"{location_name}：" + "；".join(descriptions) + "。"
        if used_stale_cache:
            answer += "天气服务刚刚连接不稳，这是最近一次成功查询的数据。"
        return answer

    @staticmethod
    def _contains_rain(value) -> bool:
        return "雨" in str(value)

    def _describe_rain(
        self, forecast: dict, daily: dict | None, day_index: int, day_name: str
    ) -> str:
        daily = daily or {}
        probabilities: list[int] = []
        try:
            probabilities.append(self._number(daily.get("pop", 0)))
        except (TypeError, ValueError):
            pass

        conditions = [
            str(daily.get("weather_day", "")),
            str(daily.get("weather_night", "")),
        ]
        try:
            precipitation = float(daily.get("precip", 0) or 0)
        except (TypeError, ValueError):
            precipitation = 0.0

        rain_hours: list[tuple[int, str]] = []
        if day_index == 0:
            target_date = str(daily.get("date", ""))
            for hour in forecast.get("hourly_forecast", []) or []:
                time_text = str(hour.get("time", ""))
                if target_date and not time_text.startswith(target_date):
                    continue
                try:
                    probability = self._number(hour.get("pop", 0) or 0)
                except (TypeError, ValueError):
                    probability = 0
                probabilities.append(probability)
                try:
                    hourly_precipitation = float(hour.get("precip", 0) or 0)
                except (TypeError, ValueError):
                    hourly_precipitation = 0.0
                condition = str(hour.get("weather", ""))
                if (
                    probability >= 30
                    or hourly_precipitation > 0
                    or self._contains_rain(condition)
                ):
                    match = re.search(r"\s(\d{1,2}):", time_text)
                    if match:
                        rain_hours.append((int(match.group(1)), condition))

        max_probability = max(probabilities, default=0)
        has_rain = (
            bool(rain_hours)
            or precipitation > 0
            or any(self._contains_rain(condition) for condition in conditions)
            or max_probability >= 30
        )
        if not has_rain:
            probability_text = (
                f"，最高降雨概率{max_probability}%" if probabilities else ""
            )
            return f"{day_name}大概率不会下雨{probability_text}，通常不用带伞"

        time_text = ""
        if rain_hours:
            first_hour = rain_hours[0][0]
            last_hour = rain_hours[-1][0]
            period = (
                f"{first_hour}点前后"
                if first_hour == last_hour
                else f"{first_hour}点到{last_hour}点"
            )
            hourly_conditions = [item[1] for item in rain_hours if item[1]]
            rain_type = next(
                (item for item in hourly_conditions if self._contains_rain(item)), "降雨"
            )
            time_text = f"，预计{period}有{rain_type}"
        elif conditions[0] and conditions[1]:
            if conditions[0] == conditions[1]:
                time_text = f"，预计有{conditions[0]}"
            else:
                time_text = f"，白天{conditions[0]}、夜间{conditions[1]}"

        probability_text = (
            f"，最高降雨概率{max_probability}%" if probabilities else ""
        )
        return f"{day_name}会下雨{time_text}{probability_text}，建议带伞"


class DesktopTools:
    """Small, explicit allowlist of local desktop actions."""

    BLOCKED_PYTHON_MODULES = {
        "ctypes",
        "os",
        "pathlib",
        "shutil",
        "socket",
        "subprocess",
        "winreg",
    }
    BLOCKED_PYTHON_CALLS = {"compile", "eval", "exec", "open", "__import__"}

    def __init__(
        self,
        model_router=None,
        notes_dir: Path | None = None,
        desktop_dir: Path | None = None,
        run_timeout_seconds: float = 10.0,
    ) -> None:
        self.model_router = model_router
        self.notes_dir = notes_dir or Path(tempfile.gettempdir()) / "LaoyeVoiceAssistant"
        self.desktop_dir = desktop_dir
        self.run_timeout_seconds = max(1.0, float(run_timeout_seconds))
        self.current_document: Path | None = None
        self.current_code_suffix = ""
        self.current_code_request = ""
        self.last_run_error = ""
        self.pending_fix_confirmation = False
        self.pending_run_confirmation = False

    @property
    def context_active(self) -> bool:
        return self.current_document is not None

    def start_conversation(self) -> None:
        self.current_document = None
        self.current_code_suffix = ""
        self.current_code_request = ""
        self.last_run_error = ""
        self.pending_fix_confirmation = False
        self.pending_run_confirmation = False

    def can_handle_follow_up(self, text: str) -> bool:
        normalized = normalize_text(text)
        confirmation = normalized in {
            "好",
            "好的",
            "可以",
            "确认",
            "是",
            "是的",
            "修改吧",
            "确认修改",
            "运行吧",
            "确认运行",
        }
        cancellation = normalized in {"不用", "不要", "取消", "不用了", "先不修改"}
        return is_desktop_follow_up(text) or (
            (self.pending_fix_confirmation or self.pending_run_confirmation)
            and (confirmation or cancellation)
        )

    @staticmethod
    def _wants_code(text: str) -> bool:
        normalized = normalize_text(text)
        return "代码" in normalized and any(
            verb in normalized for verb in ("写", "生成", "输入")
        )

    @staticmethod
    def _literal_text(text: str) -> str:
        for pattern in (r"写\s*入", r"输\s*入", r"写\s*上", r"记\s*下"):
            match = re.search(pattern, text)
            if match:
                return text[match.end() :].strip(" ，。！？,.!?、：:；;")
        normalized = normalize_text(text)
        if "写" in normalized and "代码" not in normalized:
            return normalized.split("写", 1)[1].strip()
        return ""

    @staticmethod
    def _code_suffix(text: str) -> str:
        normalized = normalize_text(text).casefold()
        if "sql" in normalized or "数据库" in normalized:
            return ".sql"
        if "javascript" in normalized or "js代码" in normalized:
            return ".js"
        if "java代码" in normalized:
            return ".java"
        return ".py"

    @staticmethod
    def _code_request(text: str) -> str:
        request = re.sub(
            r"请?\s*(?:打开|启动|运行)?\s*(?:在\s*)?记\s*事\s*本(?:里|中)?"
            r"[\s，,、。；;]*(?:然后)?",
            "",
            text,
            count=1,
        ).strip(" ，。！？,.!?、：:；;")
        generic_requests = {
            "写代码",
            "写几行代码",
            "写一段代码",
            "生成代码",
            "输入代码",
        }
        if normalize_text(request) in generic_requests or not request:
            return "使用Python写一个简短的问候程序，并打印当前时间"
        return request

    def _new_document(self, text: str = "") -> Path:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        note_path = self.notes_dir / f"laoye_note_{timestamp}.txt"
        note_path.write_text(text, encoding="utf-8")
        self.current_document = note_path
        self.current_code_suffix = ""
        self.current_code_request = ""
        self.last_run_error = ""
        self.pending_fix_confirmation = False
        self.pending_run_confirmation = False
        return note_path

    @staticmethod
    def _open_notepad(note_path: Path) -> None:
        subprocess.Popen(["notepad.exe", str(note_path)])

    def _write_current(self, text: str, code_suffix: str = "") -> Path:
        note_path = self.current_document or self._new_document()
        note_path.write_text(text, encoding="utf-8")
        if code_suffix:
            self.current_code_suffix = code_suffix
        self.last_run_error = ""
        self.pending_fix_confirmation = False
        self.pending_run_confirmation = False
        self._open_notepad(note_path)
        return note_path

    def _desktop_directory(self) -> Path:
        if self.desktop_dir is not None:
            return self.desktop_dir
        buffer = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
        if result != 0 or not buffer.value:
            raise RuntimeError("无法找到桌面目录")
        return Path(buffer.value)

    def _requested_filename(self, command: str, suffix: str) -> str:
        match = re.search(
            r"(?:文件\s*名(?:叫|为|是)?|命名为)\s*([^，。！？,.!?、；;]+)",
            command,
        )
        if not match:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return f"老叶代码_{timestamp}{suffix}"
        filename = match.group(1).strip().replace("点py", ".py").replace("点txt", ".txt")
        filename = re.sub(r'[<>:"/\\|?*]', "_", Path(filename).name).rstrip(". ")
        if not filename:
            raise RuntimeError("没有识别到有效文件名")
        if not Path(filename).suffix:
            filename += suffix
        return filename

    def _save_to_desktop(self, command: str) -> str:
        if self.current_document is None or not self.current_document.exists():
            raise RuntimeError("当前没有可保存的记事本内容")
        desktop = self._desktop_directory()
        desktop.mkdir(parents=True, exist_ok=True)
        suffix = self.current_code_suffix or self.current_document.suffix or ".txt"
        requested_name = self._requested_filename(command, suffix)
        destination = desktop / requested_name
        stem = destination.stem
        destination_suffix = destination.suffix
        counter = 2
        while destination.exists():
            destination = desktop / f"{stem}_{counter}{destination_suffix}"
            counter += 1
        shutil.copy2(self.current_document, destination)
        self.current_document = destination
        self._open_notepad(destination)
        return f"已保存到桌面，文件名是{destination.name}。"

    @classmethod
    def _validate_python_for_run(cls, code: str) -> None:
        try:
            tree = ast.parse(code)
        except SyntaxError as error:
            raise RuntimeError(f"Python代码存在语法错误：{error.msg}") from error
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [alias.name.split(".", 1)[0] for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [str(node.module or "").split(".", 1)[0]]
                )
                if any(module in cls.BLOCKED_PYTHON_MODULES for module in modules):
                    raise RuntimeError("代码包含文件、网络或系统操作，已阻止运行")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in cls.BLOCKED_PYTHON_CALLS:
                    raise RuntimeError("代码包含动态执行或文件操作，已阻止运行")

    @staticmethod
    def _short_output(value: str, limit: int = 240) -> str:
        cleaned = re.sub(r"\s+", " ", value).strip()
        return cleaned if len(cleaned) <= limit else cleaned[:limit].rstrip() + "，后面省略"

    def _run_current(self) -> str:
        if self.current_document is None or not self.current_document.exists():
            raise RuntimeError("当前没有可以运行的代码")
        suffix = self.current_code_suffix or self.current_document.suffix.casefold()
        if suffix != ".py":
            raise RuntimeError("目前只支持直接运行Python代码")
        code = self.current_document.read_text(encoding="utf-8")
        self._validate_python_for_run(code)
        environment = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(self.current_document)],
                cwd=str(self.current_document.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.run_timeout_seconds,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            self.last_run_error = f"运行超过{self.run_timeout_seconds:g}秒，已停止"
            self.pending_fix_confirmation = True
            self.pending_run_confirmation = False
            return f"代码运行超时，已停止。要我分析并修改吗？"

        output = self._short_output(result.stdout)
        error_output = self._short_output(result.stderr)
        if result.returncode == 0:
            self.last_run_error = ""
            self.pending_fix_confirmation = False
            self.pending_run_confirmation = False
            return f"代码运行成功，输出是：{output or '没有输出'}。"

        self.last_run_error = error_output or output or f"退出代码{result.returncode}"
        self.pending_fix_confirmation = True
        self.pending_run_confirmation = False
        return f"代码运行失败：{self.last_run_error}。要我分析并修改吗？"

    def _fix_current(self) -> str:
        if self.model_router is None:
            raise RuntimeError("代码生成模型未连接")
        if self.current_document is None or not self.current_document.exists():
            raise RuntimeError("当前没有可以修改的代码")
        current_code = self.current_document.read_text(encoding="utf-8")
        fixed_code = strip_code_fence(
            self.model_router.fix_code(
                self.current_code_request,
                current_code,
                self.last_run_error,
            )
        )
        if not fixed_code:
            raise RuntimeError("模型没有返回修复后的代码")
        self._write_current(fixed_code, self.current_code_suffix or ".py")
        self.pending_run_confirmation = True
        return "代码已经修改并写回记事本。要重新运行吗？"

    def execute(self, command: str) -> str:
        normalized = normalize_text(command)
        confirmations = {
            "好",
            "好的",
            "可以",
            "确认",
            "是",
            "是的",
            "修改吧",
            "确认修改",
            "运行吧",
            "确认运行",
        }
        cancellations = {"不用", "不要", "取消", "不用了", "先不修改"}
        if normalized in cancellations and (
            self.pending_fix_confirmation or self.pending_run_confirmation
        ):
            self.pending_fix_confirmation = False
            self.pending_run_confirmation = False
            return "好的，已取消。"
        if self.pending_fix_confirmation and normalized in confirmations:
            return self._fix_current()
        if self.pending_run_confirmation and normalized in confirmations:
            self.pending_run_confirmation = False
            return self._run_current()
        if any(
            phrase in normalized for phrase in ("保存到桌面", "存到桌面", "另存到桌面")
        ):
            return self._save_to_desktop(command)
        if (
            "运行代码" in normalized
            or "运行程序" in normalized
            or normalized in {"运行", "打开运行", "执行", "执行代码"}
        ):
            return self._run_current()
        if "计算器" in normalized:
            subprocess.Popen(["calc.exe"])
            return "计算器已打开。"

        if self._wants_code(command):
            if self.model_router is None:
                raise RuntimeError("代码生成模型未连接")
            code_request = self._code_request(command)
            generated = strip_code_fence(self.model_router.generate_code(code_request))
            if not generated:
                raise RuntimeError("没有生成可写入的代码")
            self.current_code_request = code_request
            self._write_current(generated, self._code_suffix(command))
            self.current_code_request = code_request
            return "记事本已打开，代码已经写好了。"

        literal_text = self._literal_text(command)
        if literal_text:
            self._write_current(literal_text)
            return "记事本已打开，内容已经写好了。"

        if "记事本" in normalized:
            note_path = self._new_document()
            self._open_notepad(note_path)
            return "记事本已打开。"
        raise ValueError("不支持的电脑操作")


class OllamaClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.history: list[dict[str, str]] = []

    @property
    def _history_message_limit(self) -> int:
        return self.config.conversation_history_turns * 2

    def start_conversation(self) -> None:
        """Start a fresh wake-word session without leaking an older topic."""
        self.history.clear()

    def end_conversation(self) -> None:
        self.history.clear()

    def remember(self, question: str, answer: str) -> None:
        self.history.extend(
            (
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            )
        )
        self.history = self.history[-self._history_message_limit :]

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            f"{self.config.ollama_url}{path}", data=data, headers=headers
        )
        with urllib.request.urlopen(
            request, timeout=self.config.ollama_timeout_seconds
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def _stream_request(self, path: str, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.ollama_url}{path}",
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(
            request, timeout=self.config.ollama_timeout_seconds
        ) as response:
            for raw_line in response:
                line = raw_line.strip()
                if line:
                    yield json.loads(line.decode("utf-8"))

    def check(self) -> tuple[bool, str]:
        if not self.config.ollama_enabled:
            return False, "配置中已关闭"
        try:
            response = self._request("/api/tags")
            names = {
                str(model.get("name", "")) for model in response.get("models", [])
            }
            if self.config.ollama_model not in names:
                return False, f"未安装模型 {self.config.ollama_model}"
            return True, self.config.ollama_model
        except Exception as error:
            return False, str(error)

    def warm_up(self) -> None:
        """Load the model before the first spoken question to avoid cold-start delay."""
        self._request(
            "/api/generate",
            {
                "model": self.config.ollama_model,
                "prompt": "",
                "stream": False,
                "think": False,
                "keep_alive": self.config.ollama_keep_alive,
            },
        )

    def _chat(
        self,
        messages: list[dict],
        temperature: float,
        num_predict: int,
        on_segment=None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        payload = {
            "model": self.config.ollama_model,
            "messages": messages,
            "stream": True,
            "think": False,
            "keep_alive": self.config.ollama_keep_alive,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        answer_parts: list[str] = []
        speech_buffer = ""
        for chunk in self._stream_request("/api/chat", payload):
            if cancel_event is not None and cancel_event.is_set():
                break
            content = str(chunk.get("message", {}).get("content", ""))
            if not content:
                continue
            answer_parts.append(content)
            speech_buffer += content
            segments, speech_buffer = take_speech_segments(speech_buffer)
            if on_segment:
                for segment in segments:
                    on_segment(segment)
        if cancel_event is None or not cancel_event.is_set():
            segments, _ = take_speech_segments(speech_buffer, flush=True)
            if on_segment:
                for segment in segments:
                    on_segment(segment)
        answer = "".join(answer_parts).strip()
        if not answer and (cancel_event is None or not cancel_event.is_set()):
            raise RuntimeError("Ollama 没有返回回答")
        return answer

    def ask(
        self,
        question: str,
        on_segment=None,
        cancel_event: threading.Event | None = None,
        recognition_candidates: list[str] | None = None,
    ) -> str:
        spoken_question = build_accent_aware_question(
            question, recognition_candidates or []
        )
        messages = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {"role": "user", "content": spoken_question},
        ]
        answer = self._chat(messages, 0.4, 160, on_segment, cancel_event)
        if cancel_event is None or not cancel_event.is_set():
            self.remember(question, answer)
        return answer

    def generate_code(self, request: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是代码生成器。只输出简短、可运行的纯代码，不要Markdown代码块，"
                    "不要解释。用户没有指定语言时使用Python。不要生成用于打开应用、"
                    "执行Shell命令或控制电脑的代码，除非编程需求明确要求这些功能。"
                ),
            },
            {"role": "user", "content": request},
        ]
        return self._chat(messages, 0.2, 400)

    def fix_code(self, request: str, code: str, error: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是代码修复器。只输出修复后的完整纯代码，不要Markdown代码块，"
                    "不要解释。不要添加文件、网络、Shell或电脑控制操作。"
                ),
            },
            {
                "role": "user",
                "content": f"原始需求：{request}\n当前代码：\n{code}\n运行错误：\n{error}",
            },
        ]
        return self._chat(messages, 0.1, 600)

    def ask_vision(
        self,
        question: str,
        image_bytes: bytes,
        on_segment=None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages: list[dict] = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {
                "role": "user",
                "content": (
                    f"{question}\n请根据这张摄像头的当前画面直接回答。"
                    "只描述确实能看到的内容，不确定的地方要明确说明。"
                ),
                "images": [image_base64],
            },
        ]
        answer = self._chat(messages, 0.2, 180, on_segment, cancel_event)
        if cancel_event is None or not cancel_event.is_set():
            self.remember(question, answer)
        return answer

    def detect_person(self, image_bytes: bytes) -> bool:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "system",
                "content": (
                    "判断图像中是否出现真实的人，包括只露出部分身体的人。"
                    "有人只回答PERSON，无人只回答EMPTY，不要解释。"
                ),
            },
            {"role": "user", "content": "检查画面。", "images": [image_base64]},
        ]
        return parse_person_presence(self._chat(messages, 0.0, 8))

    def describe_scene(self, image_bytes: bytes) -> str:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "system",
                "content": (
                    "你是摄像头画面播报器。只描述当前图片中明确可见的主体、动作和变化，"
                    "用一句自然中文，不超过60个汉字，不要猜测，不要Markdown。"
                ),
            },
            {
                "role": "user",
                "content": "简短播报这张画面。",
                "images": [image_base64],
            },
        ]
        return self._chat(messages, 0.2, 80).strip()


class OnlineQwenClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.history: list[dict[str, str]] = []
        self._opener = build_proxy_opener(getattr(config, "network_proxy", ""))
        self.base_url = config.online_api_base_url
        self.api_key = config.online_api_key
        self._connection_error = ""
        try:
            self.base_url, self.api_key = self._resolve_connection()
        except Exception as error:
            self._connection_error = str(error)

    @property
    def _history_message_limit(self) -> int:
        return self.config.conversation_history_turns * 2

    def _resolve_connection(self) -> tuple[str, str]:
        environment_key = (
            os.environ.get(self.config.online_api_key_env, "").strip()
            if self.config.online_api_key_env
            else ""
        )
        api_key = environment_key or self.api_key
        base_url = self.base_url
        if api_key and base_url:
            return base_url, api_key

        database_path = self.config.online_config_db
        if database_path is None or not database_path.is_file():
            raise RuntimeError("没有找到在线模型 API 配置或密钥")
        database_uri = database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as database:
            rows = dict(
                database.execute(
                    "SELECT key, value FROM config "
                    "WHERE key IN ('openai.api_base_urls', 'openai.api_keys')"
                )
            )
        urls = json.loads(rows.get("openai.api_base_urls", "[]"))
        keys = json.loads(rows.get("openai.api_keys", "[]"))
        if not urls or not keys:
            raise RuntimeError("qwenchat 中没有可用的在线模型连接")
        if base_url:
            normalized_base = base_url.rstrip("/")
            for index, candidate in enumerate(urls):
                if str(candidate).rstrip("/") == normalized_base and index < len(keys):
                    return normalized_base, str(keys[index])
            raise RuntimeError("qwenchat 中没有找到匹配的在线接口地址")
        return str(urls[0]).rstrip("/"), str(keys[0])

    def _require_connection(self) -> None:
        if self._connection_error:
            raise RuntimeError(self._connection_error)
        if not self.base_url or not self.api_key:
            raise RuntimeError("在线模型连接配置不完整")

    def _request(self, path: str, payload: dict | None = None) -> dict:
        self._require_connection()
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with self._opener.open(
            request, timeout=self.config.online_timeout_seconds
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def _stream_request(self, path: str, payload: dict):
        self._require_connection()
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "text/event-stream",
            },
        )
        with self._opener.open(
            request, timeout=self.config.online_timeout_seconds
        ) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                event_data = line[5:].strip()
                if not event_data or event_data == "[DONE]":
                    continue
                yield json.loads(event_data)

    def check(self) -> tuple[bool, str]:
        try:
            response = self._request("/models")
            model_ids = {
                str(model.get("id", "")) for model in response.get("data", [])
            }
            if self.config.online_model not in model_ids:
                return False, f"服务端没有模型 {self.config.online_model}"
            return True, self.config.online_model
        except Exception as error:
            return False, str(error)

    def warm_up(self) -> None:
        # Online models do not need a paid warm-up request.
        return None

    def start_conversation(self) -> None:
        self.history.clear()

    def end_conversation(self) -> None:
        self.history.clear()

    def remember(self, question: str, answer: str) -> None:
        self.history.extend(
            (
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            )
        )
        self.history = self.history[-self._history_message_limit :]

    def _chat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        on_segment=None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        payload = {
            "model": self.config.online_model,
            "messages": messages,
            "stream": True,
            "enable_thinking": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        answer_parts: list[str] = []
        speech_buffer = ""
        for chunk in self._stream_request("/chat/completions", payload):
            if cancel_event is not None and cancel_event.is_set():
                break
            choices = chunk.get("choices", [])
            if not choices:
                continue
            content = str(choices[0].get("delta", {}).get("content") or "")
            if not content:
                continue
            answer_parts.append(content)
            speech_buffer += content
            segments, speech_buffer = take_speech_segments(speech_buffer)
            if on_segment:
                for segment in segments:
                    on_segment(segment)
        if cancel_event is None or not cancel_event.is_set():
            segments, _ = take_speech_segments(speech_buffer, flush=True)
            if on_segment:
                for segment in segments:
                    on_segment(segment)
        answer = "".join(answer_parts).strip()
        if not answer and (cancel_event is None or not cancel_event.is_set()):
            raise RuntimeError("在线 Qwen 没有返回回答")
        return answer

    def ask(
        self,
        question: str,
        on_segment=None,
        cancel_event: threading.Event | None = None,
        recognition_candidates: list[str] | None = None,
    ) -> str:
        spoken_question = build_accent_aware_question(
            question, recognition_candidates or []
        )
        messages = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {"role": "user", "content": spoken_question},
        ]
        answer = self._chat(messages, 0.4, 160, on_segment, cancel_event)
        if cancel_event is None or not cancel_event.is_set():
            self.remember(question, answer)
        return answer

    def generate_code(self, request: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是代码生成器。只输出简短、可运行的纯代码，不要Markdown代码块，"
                    "不要解释。用户没有指定语言时使用Python。不要生成用于打开应用、"
                    "执行Shell命令或控制电脑的代码，除非编程需求明确要求这些功能。"
                ),
            },
            {"role": "user", "content": request},
        ]
        return self._chat(messages, 0.2, 400)

    def fix_code(self, request: str, code: str, error: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是代码修复器。只输出修复后的完整纯代码，不要Markdown代码块，"
                    "不要解释。不要添加文件、网络、Shell或电脑控制操作。"
                ),
            },
            {
                "role": "user",
                "content": f"原始需求：{request}\n当前代码：\n{code}\n运行错误：\n{error}",
            },
        ]
        return self._chat(messages, 0.1, 600)

    def ask_vision(
        self,
        question: str,
        image_bytes: bytes,
        on_segment=None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages: list[dict] = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{question}\n请根据这张摄像头的当前画面直接回答。"
                            "只描述确实能看到的内容，不确定的地方要明确说明。"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        },
                    },
                ],
            },
        ]
        answer = self._chat(messages, 0.2, 180, on_segment, cancel_event)
        if cancel_event is None or not cancel_event.is_set():
            self.remember(question, answer)
        return answer

    def detect_person(self, image_bytes: bytes) -> bool:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "system",
                "content": (
                    "判断图像中是否出现真实的人，包括只露出部分身体的人。"
                    "有人只回答PERSON，无人只回答EMPTY，不要解释。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "检查画面。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                ],
            },
        ]
        return parse_person_presence(self._chat(messages, 0.0, 8))

    def describe_scene(self, image_bytes: bytes) -> str:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "system",
                "content": (
                    "你是摄像头画面播报器。只描述当前图片中明确可见的主体、动作和变化，"
                    "用一句自然中文，不超过60个汉字，不要猜测，不要Markdown。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "简短播报这张画面。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                ],
            },
        ]
        return self._chat(messages, 0.2, 80).strip()


class ModelRouter:
    PROVIDER_ALIASES = {
        "online": "online",
        "qwen": "online",
        "openai": "online",
        "ollama": "ollama",
        "local": "ollama",
    }

    def __init__(self, config: Config, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.online = OnlineQwenClient(config)
        self.local = OllamaClient(config)
        self._lock = threading.RLock()
        self._provider = self.PROVIDER_ALIASES.get(config.llm_provider, "ollama")

    def _client(self) -> OllamaClient | OnlineQwenClient:
        with self._lock:
            return self.online if self._provider == "online" else self.local

    def info(self) -> dict[str, str]:
        with self._lock:
            if self._provider == "online":
                return {
                    "provider": "online",
                    "model": self.config.online_model,
                    "label": self.config.online_model,
                }
            return {
                "provider": "ollama",
                "model": self.config.ollama_model,
                "label": self.config.ollama_model,
            }

    def check(self) -> tuple[bool, str]:
        return self._client().check()

    def warm_up(self) -> None:
        self._client().warm_up()

    def start_conversation(self) -> None:
        self._client().start_conversation()

    def end_conversation(self) -> None:
        self._client().end_conversation()

    def remember(self, question: str, answer: str) -> None:
        self._client().remember(question, answer)

    def ask(
        self,
        question: str,
        on_segment=None,
        cancel_event: threading.Event | None = None,
        recognition_candidates: list[str] | None = None,
    ) -> str:
        return self._client().ask(
            question, on_segment, cancel_event, recognition_candidates
        )

    def generate_code(self, request: str) -> str:
        return self._client().generate_code(request)

    def fix_code(self, request: str, code: str, error: str) -> str:
        return self._client().fix_code(request, code, error)

    def ask_vision(
        self,
        question: str,
        image_bytes: bytes,
        on_segment=None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        return self._client().ask_vision(
            question, image_bytes, on_segment, cancel_event
        )

    def detect_person(self, image_bytes: bytes) -> bool:
        return self._client().detect_person(image_bytes)

    def describe_scene(self, image_bytes: bytes) -> str:
        return self._client().describe_scene(image_bytes)

    def switch(self, provider: str) -> dict[str, str]:
        normalized = self.PROVIDER_ALIASES.get(provider.strip().casefold())
        if normalized is None:
            raise ValueError("不支持的模型类型")
        target = self.online if normalized == "online" else self.local
        ready, status = target.check()
        if not ready:
            raise RuntimeError(status)
        with self._lock:
            if normalized != self._provider:
                self.online.end_conversation()
                self.local.end_conversation()
                self._provider = normalized
                self._persist_provider(normalized)
            return self.info()

    def _persist_provider(self, provider: str) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["llm_provider"] = provider
        self.config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


class CameraFrameStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot_ready = threading.Condition(self._lock)
        self.shutdown_event = threading.Event()
        self._frame: bytes | None = None
        self._frame_time = 0.0
        self._snapshot_request_id = 0
        self._pending_snapshot_request_id: int | None = None
        self._analysis_snapshot_id = 0
        self._analysis_snapshot: bytes | None = None
        self._assistant_status = "等待摄像头连接"
        self._last_answer = ""
        self._presence_enabled = True
        self._presence_initialized = False
        self._person_present = False
        self._presence_checking = False
        self._presence_status = "等待建立画面基线"
        self._presence_error = ""
        self._presence_event_id = 0
        self._presence_event_time = ""
        self._presence_snapshot: bytes | None = None
        self._pending_presence_alert = False
        self._scene_broadcast_enabled = False
        self._scene_broadcast_status = "动态画面播报已关闭"
        self._scene_broadcast_error = ""
        self._scene_broadcast_request_id = 0
        self._pending_scene_broadcast: tuple[int, bytes] | None = None
        self._processing_scene_broadcast_id: int | None = None
        self._scene_broadcast_last_requested_at = 0.0
        self._scene_broadcast_event_id = 0
        self._scene_broadcast_event_time = ""
        self._scene_broadcast_snapshot: bytes | None = None
        self._last_scene_description = ""

    def update_frame(self, frame: bytes) -> None:
        with self._lock:
            self._frame = frame
            self._frame_time = time.time()

    def latest_frame(self, max_age_seconds: float) -> bytes | None:
        with self._lock:
            if self._frame is None or time.time() - self._frame_time > max_age_seconds:
                return None
            return self._frame

    def request_snapshot(self, timeout_seconds: float) -> bytes | None:
        """Ask the browser for a new frame and wait only for that exact capture."""
        deadline = time.monotonic() + timeout_seconds
        with self._snapshot_ready:
            self._snapshot_request_id += 1
            request_id = self._snapshot_request_id
            self._pending_snapshot_request_id = request_id
            self._snapshot_ready.notify_all()
            while self._analysis_snapshot_id != request_id:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.shutdown_event.is_set():
                    if self._pending_snapshot_request_id == request_id:
                        self._pending_snapshot_request_id = None
                    return None
                self._snapshot_ready.wait(remaining)
            return self._analysis_snapshot

    def submit_snapshot(self, request_id: int, frame: bytes) -> bool:
        with self._snapshot_ready:
            if request_id != self._pending_snapshot_request_id:
                return False
            self._analysis_snapshot = frame
            self._analysis_snapshot_id = request_id
            self._pending_snapshot_request_id = None
            self._snapshot_ready.notify_all()
            return True

    def analysis_snapshot(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._analysis_snapshot_id, self._analysis_snapshot

    def begin_presence_check(self) -> bool:
        with self._lock:
            if not self._presence_enabled or self._presence_checking:
                return False
            self._presence_checking = True
            self._presence_status = "正在确认画面是否有人"
            self._presence_error = ""
            return True

    def finish_presence_check(
        self, person_present: bool, frame: bytes, reason: str
    ) -> bool:
        with self._lock:
            if not self._presence_enabled:
                self._presence_checking = False
                return False
            was_initialized = self._presence_initialized
            previous = self._person_present
            self._presence_initialized = True
            self._person_present = person_present
            self._presence_checking = False
            self._presence_error = ""
            triggered = (
                reason != "baseline"
                and was_initialized
                and not previous
                and person_present
            )
            if triggered:
                self._presence_event_id += 1
                self._presence_event_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._presence_snapshot = frame
                self._pending_presence_alert = True
                self._presence_status = "检测到有人进入画面"
            else:
                self._presence_status = "画面中有人" if person_present else "画面中无人"
            return triggered

    def fail_presence_check(self, error: str) -> None:
        with self._lock:
            self._presence_checking = False
            if not self._presence_enabled:
                return
            self._presence_error = error
            self._presence_status = "人物检测暂时不可用"

    def set_presence_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._presence_enabled = enabled
            self._presence_initialized = False
            self._person_present = False
            self._presence_checking = False
            self._presence_error = ""
            self._pending_presence_alert = False
            self._presence_status = (
                "等待建立画面基线" if enabled else "动态人物监测已关闭"
            )

    def presence_snapshot(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._presence_event_id, self._presence_snapshot

    def consume_presence_alert(self) -> int | None:
        with self._lock:
            if not self._pending_presence_alert:
                return None
            self._pending_presence_alert = False
            return self._presence_event_id

    def set_scene_broadcast_enabled(self, enabled: bool) -> None:
        with self._lock:
            if self._scene_broadcast_enabled == enabled:
                return
            self._scene_broadcast_enabled = enabled
            self._pending_scene_broadcast = None
            self._processing_scene_broadcast_id = None
            self._scene_broadcast_last_requested_at = 0.0
            self._scene_broadcast_error = ""
            self._scene_broadcast_status = (
                "等待画面变化" if enabled else "动态画面播报已关闭"
            )

    def scene_broadcast_enabled(self) -> bool:
        with self._lock:
            return self._scene_broadcast_enabled

    def submit_scene_broadcast(
        self, frame: bytes, cooldown_seconds: float
    ) -> bool:
        with self._lock:
            now = time.monotonic()
            busy = (
                self._pending_scene_broadcast is not None
                or self._processing_scene_broadcast_id is not None
            )
            cooling_down = (
                now - self._scene_broadcast_last_requested_at < cooldown_seconds
            )
            if not self._scene_broadcast_enabled or busy or cooling_down:
                return False
            self._scene_broadcast_request_id += 1
            request_id = self._scene_broadcast_request_id
            self._pending_scene_broadcast = (request_id, frame)
            self._scene_broadcast_last_requested_at = now
            self._scene_broadcast_error = ""
            self._scene_broadcast_status = "已捕获变化画面，等待分析"
            return True

    def consume_scene_broadcast(self) -> tuple[int, bytes] | None:
        with self._lock:
            if not self._scene_broadcast_enabled:
                self._pending_scene_broadcast = None
                return None
            pending = self._pending_scene_broadcast
            if pending is None:
                return None
            self._pending_scene_broadcast = None
            self._processing_scene_broadcast_id = pending[0]
            self._scene_broadcast_status = "正在分析变化画面"
            return pending

    def finish_scene_broadcast(
        self, request_id: int, frame: bytes, description: str
    ) -> bool:
        with self._lock:
            if (
                not self._scene_broadcast_enabled
                or self._processing_scene_broadcast_id != request_id
            ):
                return False
            self._processing_scene_broadcast_id = None
            self._scene_broadcast_event_id = request_id
            self._scene_broadcast_event_time = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            self._scene_broadcast_snapshot = frame
            self._last_scene_description = description
            self._scene_broadcast_error = ""
            self._scene_broadcast_status = "画面内容已播报"
            return True

    def fail_scene_broadcast(self, request_id: int, error: str) -> None:
        with self._lock:
            if (
                not self._scene_broadcast_enabled
                or self._processing_scene_broadcast_id != request_id
            ):
                return
            self._processing_scene_broadcast_id = None
            self._scene_broadcast_error = error
            self._scene_broadcast_status = "画面播报暂时不可用"

    def scene_broadcast_snapshot(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._scene_broadcast_event_id, self._scene_broadcast_snapshot

    def set_assistant_status(self, status: str, answer: str | None = None) -> None:
        with self._lock:
            self._assistant_status = status
            if answer is not None:
                self._last_answer = answer

    def status(self) -> dict:
        with self._lock:
            age = time.time() - self._frame_time if self._frame else None
            return {
                "camera_ready": self._frame is not None and age is not None and age < 4.0,
                "frame_age_seconds": round(age, 1) if age is not None else None,
                "snapshot_request_id": self._snapshot_request_id,
                "analysis_snapshot_id": self._analysis_snapshot_id,
                "snapshot_pending": self._pending_snapshot_request_id is not None,
                "presence_enabled": self._presence_enabled,
                "presence_initialized": self._presence_initialized,
                "person_present": self._person_present,
                "presence_checking": self._presence_checking,
                "presence_status": self._presence_status,
                "presence_error": self._presence_error,
                "presence_event_id": self._presence_event_id,
                "presence_event_time": self._presence_event_time,
                "scene_broadcast_enabled": self._scene_broadcast_enabled,
                "scene_broadcast_busy": (
                    self._pending_scene_broadcast is not None
                    or self._processing_scene_broadcast_id is not None
                ),
                "scene_broadcast_status": self._scene_broadcast_status,
                "scene_broadcast_error": self._scene_broadcast_error,
                "scene_broadcast_event_id": self._scene_broadcast_event_id,
                "scene_broadcast_event_time": self._scene_broadcast_event_time,
                "last_scene_description": self._last_scene_description,
                "assistant_status": self._assistant_status,
                "last_answer": self._last_answer,
            }

    def request_shutdown(self) -> None:
        with self._snapshot_ready:
            self._assistant_status = "正在关闭语音助手"
            self.shutdown_event.set()
            self._snapshot_ready.notify_all()


class YoloPersonDetector:
    def __init__(self, config: Config) -> None:
        self.python_executable = config.yolo_python_executable
        self.model_path = config.yolo_model_path
        self.device = config.yolo_device
        self.confidence = config.yolo_confidence
        self.image_size = config.yolo_image_size
        self.timeout_seconds = config.yolo_timeout_seconds
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._responses: queue.Queue[dict] = queue.Queue()
        self._stderr_file = None
        self._request_id = 0

    def _read_responses(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self._responses.put(payload)
        self._responses.put({"error": "YOLO检测进程已经退出"})

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._stderr_file is not None:
            self._stderr_file.close()
            self._stderr_file = None
        self._responses = queue.Queue()

    def _start_process(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if not self.python_executable:
            raise RuntimeError("没有配置YOLO Python解释器")
        if not self.model_path.is_file():
            raise RuntimeError(f"没有找到YOLO模型：{self.model_path}")
        worker_path = APP_DIR / "yolo_person_worker.py"
        if not worker_path.is_file():
            raise RuntimeError("没有找到YOLO检测进程脚本")

        logs_dir = APP_DIR / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_file = (logs_dir / "yolo-error.log").open(
            "a", encoding="utf-8"
        )
        self._process = subprocess.Popen(
            [
                self.python_executable,
                "-u",
                str(worker_path),
                "--model",
                str(self.model_path),
                "--device",
                self.device,
                "--confidence",
                str(self.confidence),
                "--image-size",
                str(self.image_size),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(
            target=self._read_responses,
            args=(self._process,),
            name="yolo-response-reader",
            daemon=True,
        ).start()
        try:
            ready = self._responses.get(timeout=self.timeout_seconds)
        except queue.Empty as error:
            self._stop_process()
            raise RuntimeError("YOLO模型加载超时") from error
        if not ready.get("ready"):
            message = str(ready.get("error") or "YOLO模型加载失败")
            self._stop_process()
            raise RuntimeError(message)
        print(f"YOLO人物检测：已加载 / GPU {self.device} / {self.model_path.name}")

    def detect_person(self, image_bytes: bytes) -> bool:
        with self._lock:
            self._start_process()
            process = self._process
            if process is None or process.stdin is None:
                raise RuntimeError("YOLO检测进程不可用")
            self._request_id += 1
            request_id = self._request_id
            request = {
                "id": request_id,
                "jpeg": base64.b64encode(image_bytes).decode("ascii"),
            }
            try:
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self._stop_process()
                raise RuntimeError("无法向YOLO检测进程发送图像") from error

            deadline = time.monotonic() + self.timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_process()
                    raise RuntimeError("YOLO人物检测超时")
                try:
                    response = self._responses.get(timeout=remaining)
                except queue.Empty as error:
                    self._stop_process()
                    raise RuntimeError("YOLO人物检测超时") from error
                if response.get("id") not in {None, request_id}:
                    continue
                if response.get("error"):
                    message = str(response["error"])
                    self._stop_process()
                    raise RuntimeError(message)
                return bool(response.get("person"))

    def close(self) -> None:
        with self._lock:
            self._stop_process()


class PersonPresenceMonitor:
    def __init__(
        self,
        store: CameraFrameStore,
        detector,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.detector = detector
        self.enabled = enabled
        self.store.set_presence_enabled(enabled)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.store.set_presence_enabled(enabled)

    def submit(self, frame: bytes, reason: str) -> bool:
        if not self.enabled or not self.store.begin_presence_check():
            return False
        normalized_reason = "baseline" if reason == "baseline" else "motion"
        threading.Thread(
            target=self._detect,
            args=(frame, normalized_reason),
            name="person-presence-check",
            daemon=True,
        ).start()
        return True

    def _detect(self, frame: bytes, reason: str) -> None:
        try:
            person_present = self.detector.detect_person(frame)
            triggered = self.store.finish_presence_check(
                person_present, frame, reason
            )
        except Exception as error:
            print(f"[动态监测失败] {error}", file=sys.stderr)
            self.store.fail_presence_check(str(error))
            return
        state = "person" if person_present else "empty"
        transition = " / entered" if triggered else ""
        print(f"[presence monitor] {state}{transition}")

    def close(self) -> None:
        close = getattr(self.detector, "close", None)
        if close is not None:
            close()


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        handler,
        store: CameraFrameStore,
        config: Config,
        model_router: ModelRouter,
        presence_monitor: PersonPresenceMonitor,
    ):
        super().__init__(address, handler)
        self.store = store
        self.config = config
        self.model_router = model_router
        self.presence_monitor = presence_monitor


class DashboardHTTPServerV6(DashboardHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer
    MAX_FRAME_BYTES = 5 * 1024 * 1024

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        request_path = self.path.partition("?")[0]
        if request_path in {"/", "/index.html"}:
            body = (APP_DIR / "web" / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if request_path == "/api/config":
            self._send_json(
                {
                    "camera_name_keywords": self.server.config.camera_name_keywords,
                    "model": self.server.model_router.info(),
                    "model_options": [
                        {
                            "provider": "online",
                            "label": self.server.config.online_model,
                        },
                        {
                            "provider": "ollama",
                            "label": self.server.config.ollama_model,
                        },
                    ],
                    "person_monitor": {
                        "enabled": self.server.presence_monitor.enabled,
                        "detector": self.server.config.person_detector,
                        "sensitivity": self.server.config.person_motion_sensitivity,
                        "min_area_percent": self.server.config.person_motion_min_area_percent,
                        "consecutive_frames": self.server.config.person_motion_consecutive_frames,
                        "cooldown_seconds": self.server.config.person_motion_cooldown_seconds,
                    },
                    "scene_broadcast": {
                        "enabled": self.server.store.scene_broadcast_enabled(),
                        "cooldown_seconds": self.server.config.scene_broadcast_cooldown_seconds,
                    },
                }
            )
            return
        if request_path == "/api/status":
            self._send_json(self.server.store.status())
            return
        if request_path == "/api/snapshot":
            snapshot_id, frame = self.server.store.analysis_snapshot()
            if frame is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Snapshot-Id", str(snapshot_id))
            self.end_headers()
            self.wfile.write(frame)
            return
        if request_path == "/api/presence-snapshot":
            event_id, frame = self.server.store.presence_snapshot()
            if frame is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Presence-Event-Id", str(event_id))
            self.end_headers()
            self.wfile.write(frame)
            return
        if request_path == "/api/scene-snapshot":
            event_id, frame = self.server.store.scene_broadcast_snapshot()
            if frame is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Scene-Event-Id", str(event_id))
            self.end_headers()
            self.wfile.write(frame)
            return
        if request_path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        request_path = self.path.partition("?")[0]
        if request_path == "/api/shutdown":
            self.server.store.request_shutdown()
            self._send_json({"accepted": True}, HTTPStatus.ACCEPTED)
            return
        if request_path == "/api/model-provider":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 1024:
                    raise ValueError("无效的请求内容")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                model_info = self.server.model_router.switch(
                    str(payload.get("provider", ""))
                )
                self.server.store.set_assistant_status(
                    f"已切换为{model_info['label']}"
                )
                self._send_json({"ok": True, "model": model_info})
            except (ValueError, RuntimeError, json.JSONDecodeError) as error:
                self._send_json(
                    {"ok": False, "error": str(error)},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if request_path == "/api/person-monitor":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 1024:
                    raise ValueError("无效的请求内容")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必须是布尔值")
                self.server.presence_monitor.set_enabled(enabled)
                self._send_json({"ok": True, "enabled": enabled})
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json(
                    {"ok": False, "error": str(error)},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if request_path == "/api/scene-broadcast":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 1024:
                    raise ValueError("无效的请求内容")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必须是布尔值")
                self.server.store.set_scene_broadcast_enabled(enabled)
                queued = False
                if enabled:
                    current_frame = self.server.store.latest_frame(
                        self.server.config.camera_frame_max_age_seconds
                    )
                    if current_frame is not None:
                        queued = self.server.store.submit_scene_broadcast(
                            current_frame,
                            self.server.config.scene_broadcast_cooldown_seconds,
                        )
                self._send_json(
                    {"ok": True, "enabled": enabled, "queued": queued}
                )
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json(
                    {"ok": False, "error": str(error)},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if request_path not in {
            "/api/frame",
            "/api/snapshot",
            "/api/presence-check",
            "/api/scene-description",
        }:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > self.MAX_FRAME_BYTES:
            self._send_json({"error": "invalid frame size"}, HTTPStatus.BAD_REQUEST)
            return
        frame = self.rfile.read(content_length)
        if not (frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")):
            self._send_json({"error": "JPEG required"}, HTTPStatus.BAD_REQUEST)
            return
        if request_path == "/api/presence-check":
            accepted = self.server.presence_monitor.submit(
                frame, self.headers.get("X-Presence-Reason", "motion")
            )
            self._send_json(
                {"accepted": accepted},
                HTTPStatus.ACCEPTED if accepted else HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        if request_path == "/api/scene-description":
            accepted = self.server.store.submit_scene_broadcast(
                frame, self.server.config.scene_broadcast_cooldown_seconds
            )
            self._send_json(
                {"accepted": accepted},
                HTTPStatus.ACCEPTED if accepted else HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        if request_path == "/api/snapshot":
            try:
                request_id = int(self.headers.get("X-Snapshot-Request-Id", "0"))
            except ValueError:
                request_id = 0
            if request_id <= 0:
                self._send_json(
                    {"error": "snapshot request id required"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if not self.server.store.submit_snapshot(request_id, frame):
                self._send_json(
                    {"error": "snapshot request expired"}, HTTPStatus.CONFLICT
                )
                return
        else:
            self.server.store.update_frame(frame)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


class CameraDashboard:
    def __init__(self, config: Config, model_router: ModelRouter) -> None:
        self.config = config
        self.model_router = model_router
        self.store = CameraFrameStore()
        self.store.set_scene_broadcast_enabled(config.scene_broadcast_enabled)
        if config.person_detector == "yolo":
            person_detector = YoloPersonDetector(config)
        elif config.person_detector == "qwen":
            person_detector = model_router
        else:
            raise ValueError(
                f"不支持的人物检测器：{config.person_detector}，请使用yolo或qwen"
            )
        self.presence_monitor = PersonPresenceMonitor(
            self.store, person_detector, config.person_monitor_enabled
        )
        self.server: DashboardHTTPServer | None = None
        self.servers: list[DashboardHTTPServer] = []
        self.threads: list[threading.Thread] = []

    @property
    def url(self) -> str:
        port = self.server.server_address[1] if self.server else self.config.web_port
        display_host = (
            "localhost"
            if self.config.web_host in {"127.0.0.1", "::1", "localhost"}
            else self.config.web_host
        )
        return f"http://{display_host}:{port}/"

    def start(self) -> None:
        self.server = DashboardHTTPServer(
            (self.config.web_host, self.config.web_port),
            DashboardHandler,
            self.store,
            self.config,
            self.model_router,
            self.presence_monitor,
        )
        self.servers.append(self.server)
        thread = threading.Thread(
            target=self.server.serve_forever, name="camera-dashboard", daemon=True
        )
        self.threads.append(thread)
        thread.start()

        if self.config.web_host in {"127.0.0.1", "localhost"}:
            try:
                ipv6_server = DashboardHTTPServerV6(
                    ("::1", self.server.server_address[1], 0, 0),
                    DashboardHandler,
                    self.store,
                    self.config,
                    self.model_router,
                    self.presence_monitor,
                )
                self.servers.append(ipv6_server)
                ipv6_thread = threading.Thread(
                    target=ipv6_server.serve_forever,
                    name="camera-dashboard-ipv6",
                    daemon=True,
                )
                self.threads.append(ipv6_thread)
                ipv6_thread.start()
            except OSError as error:
                print(f"摄像头页面 IPv6 监听不可用：{error}", file=sys.stderr)
        print(f"摄像头页面：{self.url}")
        if self.config.open_browser and os.environ.get("XIAOBU_NO_BROWSER") != "1":
            threading.Timer(0.8, lambda: webbrowser.open(self.url)).start()

    def stop(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.presence_monitor.close()


class VoiceAssistant:
    COMMAND_PHRASES = [
        "现在 几点 了",
        "现在 几点",
        "几点 了",
        "几点",
        "不用 了",
        "没事 了",
        "结束 对话",
        "结束 聊天",
        "休息 吧",
        "再见",
        "退出 助手",
        "关闭 助手",
        "停止 助手",
    ]
    def __init__(
        self,
        config: Config,
        model: Model,
        input_device: AudioDevice,
        speaker: Speaker,
        ollama: OllamaClient | OnlineQwenClient | ModelRouter,
        online_tools: OnlineSearchTools,
        dashboard: CameraDashboard | None,
    ) -> None:
        self.config = config
        self.model = model
        self.input_device = input_device
        self.speaker = speaker
        self.ollama = ollama
        self.online_tools = online_tools
        self.desktop_tools = DesktopTools(
            ollama, run_timeout_seconds=config.code_run_timeout_seconds
        )
        self.dashboard = dashboard
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=80)
        self.interrupt_audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=40)
        self.speaking = threading.Event()
        self.barge_in_enabled = threading.Event()
        self.playback_cancel = threading.Event()
        self._barge_in_stop = threading.Event()
        self._interrupt_lock = threading.Lock()
        self._interrupt_action: str | None = None

    @staticmethod
    def _put_latest(target_queue: queue.Queue[bytes], data: bytes) -> None:
        try:
            target_queue.put_nowait(data)
        except queue.Full:
            try:
                target_queue.get_nowait()
                target_queue.put_nowait(data)
            except queue.Empty:
                pass

    def _audio_callback(self, indata, frames, timing, status) -> None:
        if status:
            print(f"\n音频提示：{status}", file=sys.stderr)
        if self.barge_in_enabled.is_set():
            self._put_latest(self.interrupt_audio_queue, bytes(indata))
            return
        if self.speaking.is_set():
            return
        self._put_latest(self.audio_queue, bytes(indata))

    def _clear_audio(self) -> None:
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                return

    def _clear_interrupt_audio(self) -> None:
        while True:
            try:
                self.interrupt_audio_queue.get_nowait()
            except queue.Empty:
                return

    def _set_interrupt_action(self, action: str) -> None:
        with self._interrupt_lock:
            if self._interrupt_action is None:
                self._interrupt_action = action

    def _consume_interrupt_action(self) -> str | None:
        with self._interrupt_lock:
            action = self._interrupt_action
            self._interrupt_action = None
            return action

    def _barge_in_loop(self, sample_rate: int) -> None:
        # An unrestricted recognizer prevents arbitrary speech or speaker echo from
        # being forced into one of a tiny number of interruption commands.
        recognizer = KaldiRecognizer(self.model, sample_rate)
        while not self._barge_in_stop.is_set():
            try:
                data = self.interrupt_audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if not self.barge_in_enabled.is_set():
                recognizer.Reset()
                continue
            is_final = recognizer.AcceptWaveform(data)
            result = recognizer.Result() if is_final else recognizer.PartialResult()
            text = _result_text(result, "text" if is_final else "partial")
            if not text or text == "[unk]":
                continue
            action = interruption_action(text, self.config.wake_phrases)
            if action is None:
                continue
            print(f"[打断播报] {text}", flush=True)
            self._set_interrupt_action(action)
            self.playback_cancel.set()
            if self.dashboard:
                self.dashboard.store.set_assistant_status(
                    self._continuation_status(action)
                )
            try:
                sd.stop()
            except Exception as error:
                print(f"[停止播放失败] {error}", file=sys.stderr)
            recognizer.Reset()
            self._clear_interrupt_audio()

    def _play(self, action, *args) -> None:
        self.speaking.set()
        self._clear_audio()
        try:
            action(*args)
        finally:
            self._clear_audio()
            self.speaking.clear()

    def _begin_interruptible_playback(self) -> None:
        self._consume_interrupt_action()
        self.playback_cancel.clear()
        self._clear_interrupt_audio()
        self.barge_in_enabled.set()

    def _end_interruptible_playback(self) -> str | None:
        self.barge_in_enabled.clear()
        self._clear_interrupt_audio()
        return self._consume_interrupt_action()

    def _play_interruptible(self, action, *args) -> str | None:
        self._begin_interruptible_playback()
        try:
            self._play(action, *args)
        finally:
            interrupt_action = self._end_interruptible_playback()
        return interrupt_action

    def _speak_streamed_answer(self, request) -> tuple[str, str | None]:
        speech_queue: queue.Queue[str | None] = queue.Queue()
        prepared_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=2)
        generation_result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        playback_errors: list[Exception] = []
        displayed_segments: list[str] = []
        self._begin_interruptible_playback()

        def on_segment(segment: str) -> None:
            if self.playback_cancel.is_set():
                return
            displayed_segments.append(segment)
            if self.dashboard:
                self.dashboard.store.set_assistant_status(
                    "正在回答", "".join(displayed_segments)
                )
            speech_queue.put(segment)

        def generation_worker() -> None:
            try:
                generation_result.put(("answer", request(on_segment)))
            except Exception as error:
                generation_result.put(("error", error))
            finally:
                speech_queue.put(None)

        def preparation_worker() -> None:
            try:
                while True:
                    segment = speech_queue.get()
                    if segment is None:
                        break
                    if self.playback_cancel.is_set():
                        continue
                    try:
                        samples = self.speaker.prepare(
                            segment, self.playback_cancel
                        )
                        if samples is not None:
                            prepared_queue.put(("audio", samples))
                    except Exception as error:
                        prepared_queue.put(("error", error))
            finally:
                prepared_queue.put(("done", None))

        worker = threading.Thread(
            target=generation_worker, name="streaming-model", daemon=True
        )
        preparation = threading.Thread(
            target=preparation_worker, name="streaming-tts", daemon=True
        )
        worker.start()
        preparation.start()
        playback_failed = False
        try:
            while True:
                item_kind, item_value = prepared_queue.get()
                if item_kind == "done":
                    break
                if item_kind == "error":
                    if isinstance(item_value, Exception):
                        playback_errors.append(item_value)
                    else:
                        playback_errors.append(RuntimeError(str(item_value)))
                    playback_failed = True
                    try:
                        self._play(self.speaker.chime, False)
                    except Exception:
                        pass
                    continue
                if playback_failed or self.playback_cancel.is_set():
                    discard = getattr(self.speaker, "discard_prepared", None)
                    if discard is not None:
                        discard(item_value)
                    continue
                try:
                    # Synthesis is prefetched in streaming-tts, but WASAPI playback
                    # stays on the main voice thread for reliable Windows output.
                    self._play(
                        self.speaker.play_prepared,
                        item_value,
                        self.playback_cancel,
                    )
                except Exception as error:
                    playback_errors.append(error)
                    playback_failed = True
                    try:
                        self._play(self.speaker.chime, False)
                    except Exception:
                        pass
        finally:
            worker.join()
            preparation.join()
            interrupt_action = self._end_interruptible_playback()
        result_kind, result_value = generation_result.get()
        if result_kind == "error":
            if isinstance(result_value, Exception):
                raise result_value
            raise RuntimeError(str(result_value))
        if playback_errors:
            print(f"[语音合成失败] {playback_errors[0]}", file=sys.stderr)
        return str(result_value), interrupt_action

    @staticmethod
    def _continuation_status(interrupt_action: str | None) -> str:
        if interrupt_action == "wake":
            return "回答已打断，请直接说新问题"
        if interrupt_action == "stop":
            return "已停止播报，可以继续提问"
        return "可以继续提问，无需再次唤醒"

    def _broadcast_pending_scene(self) -> bool:
        if not self.dashboard:
            return False
        pending = self.dashboard.store.consume_scene_broadcast()
        if pending is None:
            return False
        request_id, frame = pending
        try:
            description = self.ollama.describe_scene(frame)
        except Exception as error:
            print(f"[动态画面分析失败] {error}", file=sys.stderr)
            self.dashboard.store.fail_scene_broadcast(request_id, str(error))
            return False
        if not self.dashboard.store.finish_scene_broadcast(
            request_id, frame, description
        ):
            return False
        print(f"[动态画面播报] {description}")
        self.dashboard.store.set_assistant_status("动态画面播报", description)
        try:
            interrupt_action = self._play_interruptible(
                self.speaker.say, description, self.playback_cancel
            )
        except Exception as error:
            print(f"[动态画面播报失败] {error}", file=sys.stderr)
            self._play(self.speaker.chime, False)
            return True
        if interrupt_action is not None:
            self.dashboard.store.set_assistant_status(
                self._continuation_status(interrupt_action)
            )
        return True

    def run(self) -> None:
        sample_rate = self.input_device.sample_rate
        wake_recognizer = _recognizer(
            self.model, sample_rate, list(self.config.wake_phrases)
        )
        command_recognizer = KaldiRecognizer(self.model, sample_rate)
        if self.config.asr_accent_enhancement_enabled:
            command_recognizer.SetMaxAlternatives(
                self.config.asr_max_alternatives
            )
        self._barge_in_stop.clear()
        barge_in_thread = threading.Thread(
            target=self._barge_in_loop,
            args=(sample_rate,),
            name="barge-in-recognizer",
            daemon=True,
        )
        barge_in_thread.start()
        state = "waiting"
        command_deadline = 0.0
        last_partial = ""
        final_recognition_candidates: list[str] = []
        vision_context_active = False
        weather_context_active = False
        desktop_context_active = False

        print("\n已启动。请说：老叶老叶")
        print("听到“我在”后开始提问（按 Ctrl+C 退出）\n")
        if self.dashboard:
            self.dashboard.store.set_assistant_status("等待“老叶老叶”唤醒")

        with sd.RawInputStream(
            samplerate=sample_rate,
            blocksize=4000,
            device=self.input_device.index,
            dtype="int16",
            channels=1,
            callback=self._audio_callback,
        ):
            while not (
                self.dashboard and self.dashboard.store.shutdown_event.is_set()
            ):
                if self.dashboard:
                    scene_broadcasted = self._broadcast_pending_scene()
                    if scene_broadcasted and state == "command":
                        command_deadline = (
                            time.monotonic()
                            + self.config.command_timeout_seconds
                        )
                    presence_event_id = self.dashboard.store.consume_presence_alert()
                    if presence_event_id is not None:
                        alert = "检测到有人进入画面。"
                        print(f"[动态监测提醒] 事件 {presence_event_id} / {alert}")
                        if not self.dashboard.store.scene_broadcast_enabled():
                            self.dashboard.store.set_assistant_status(
                                "动态监测提醒", alert
                            )
                        if (
                            self.config.person_alert_voice
                            and not self.dashboard.store.scene_broadcast_enabled()
                        ):
                            try:
                                self._play(self.speaker.say, alert)
                            except Exception as error:
                                print(f"[动态提醒播报失败] {error}", file=sys.stderr)
                                self._play(self.speaker.chime, False)
                        if state == "command":
                            command_deadline = (
                                time.monotonic()
                                + self.config.command_timeout_seconds
                            )
                if state == "command" and time.monotonic() > command_deadline:
                    print("[会话结束] 一段时间没有继续提问，重新等待唤醒。")
                    self.ollama.end_conversation()
                    self.online_tools.start_conversation()
                    self.desktop_tools.start_conversation()
                    vision_context_active = False
                    weather_context_active = False
                    desktop_context_active = False
                    state = "waiting"
                    wake_recognizer.Reset()
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("等待“老叶老叶”唤醒")

                try:
                    data = self.audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                recognizer = wake_recognizer if state == "waiting" else command_recognizer
                is_final = recognizer.AcceptWaveform(data)
                if is_final:
                    result_payload = recognizer.Result()
                    final_recognition_candidates = recognition_alternatives(
                        result_payload
                    )
                    text = select_actionable_recognition(
                        final_recognition_candidates,
                        self.config.wake_phrases,
                        state,
                    )
                    if (
                        final_recognition_candidates
                        and text != final_recognition_candidates[0]
                    ):
                        print(
                            "[口音纠错] "
                            f"{final_recognition_candidates[0]} -> {text}"
                        )
                    if (
                        self.config.asr_accent_enhancement_enabled
                        and len(final_recognition_candidates) > 1
                    ):
                        print(
                            "[识别候选] "
                            + " / ".join(final_recognition_candidates)
                        )
                else:
                    final_recognition_candidates = []
                    text = _result_text(recognizer.PartialResult(), "partial")
                    if text == last_partial:
                        continue
                    last_partial = text

                if not text:
                    continue
                if text == "[unk]":
                    continue
                print(f"[{state}] {text}")

                if state == "command":
                    command_deadline = max(command_deadline, time.monotonic() + 3.0)

                if state == "waiting" and contains_wake_phrase(
                    text, self.config.wake_phrases
                ):
                    print("[已唤醒] 正在听……")
                    print("[回答] 我在")
                    self.ollama.start_conversation()
                    self.online_tools.start_conversation()
                    self.desktop_tools.start_conversation()
                    vision_context_active = False
                    weather_context_active = False
                    desktop_context_active = False
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("我在，请开始提问", "我在")
                    try:
                        self._play(self.speaker.say, "我在。")
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, True)
                    state = "command"
                    command_deadline = time.monotonic() + self.config.command_timeout_seconds
                    command_recognizer.Reset()
                    last_partial = ""
                    continue

                if (
                    state == "command"
                    and is_final
                    and (
                        is_desktop_command(text)
                        or (
                            desktop_context_active
                            and self.desktop_tools.can_handle_follow_up(text)
                        )
                    )
                ):
                    vision_context_active = False
                    weather_context_active = False
                    print(f"[电脑操作] {text}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("正在执行电脑操作")
                    try:
                        response = self.desktop_tools.execute(text)
                        action_succeeded = True
                        desktop_context_active = self.desktop_tools.context_active
                    except Exception as error:
                        print(f"[电脑操作失败] {error}", file=sys.stderr)
                        reason = str(error).strip().rstrip("。")
                        response = f"电脑操作没有成功，{reason or '请稍后再试'}。"
                        action_succeeded = False
                    self.ollama.remember(text, response)
                    print(f"[电脑操作回答] {response}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            "电脑操作完成" if action_succeeded else "电脑操作失败",
                            response,
                        )
                    try:
                        interrupt_action = self._play_interruptible(
                            self.speaker.say, response, self.playback_cancel
                        )
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, False)
                        interrupt_action = None
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action), response
                        )
                elif state == "command" and is_time_command(text):
                    vision_context_active = False
                    weather_context_active = False
                    response = format_time_zh(datetime.now().astimezone())
                    self.ollama.remember(text, response)
                    print(f"[回答] {response}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("回答完成", response)
                    try:
                        interrupt_action = self._play_interruptible(
                            self.speaker.say, response, self.playback_cancel
                        )
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, False)
                        interrupt_action = None
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action), response
                        )
                elif (
                    state == "command"
                    and is_final
                    and (
                        is_weather_command(text)
                        or (
                            weather_context_active
                            and is_weather_follow_up(text)
                        )
                    )
                ):
                    vision_context_active = False
                    print(f"[天气查询] {text}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("正在查询天气")
                    try:
                        answer = self.online_tools.search_weather(text)
                        weather_context_active = True
                        weather_succeeded = True
                    except Exception as error:
                        print(f"[天气查询失败] {error}", file=sys.stderr)
                        if "配置中关闭" in str(error):
                            answer = "天气查询功能暂时关闭。"
                        elif any(
                            phrase in str(error)
                            for phrase in ("没有找到地点", "城市不存在", "未找到")
                        ):
                            answer = "没有找到这个城市，请重新说城市名，比如成都天气。"
                        else:
                            answer = "天气服务暂时连接失败，请稍后再试。"
                        weather_context_active = False
                        weather_succeeded = False
                    self.ollama.remember(text, answer)
                    print(f"[天气回答] {answer}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            "天气查询完成" if weather_succeeded else "天气查询失败",
                            answer,
                        )
                    try:
                        interrupt_action = self._play_interruptible(
                            self.speaker.say, answer, self.playback_cancel
                        )
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, False)
                        interrupt_action = None
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action), answer
                        )
                elif state == "command" and is_final and is_exit_command(text):
                    self.ollama.end_conversation()
                    self.online_tools.start_conversation()
                    self.desktop_tools.start_conversation()
                    self._barge_in_stop.set()
                    vision_context_active = False
                    weather_context_active = False
                    desktop_context_active = False
                    print("再见。")
                    try:
                        self._play(self.speaker.say, "再见。")
                    except Exception:
                        self._play(self.speaker.chime, True)
                    return
                elif (
                    state == "command"
                    and is_final
                    and is_end_conversation_command(text)
                ):
                    answer = "好的，需要时再叫我。"
                    print(f"[会话结束] {answer}")
                    self.ollama.end_conversation()
                    self.online_tools.start_conversation()
                    self.desktop_tools.start_conversation()
                    vision_context_active = False
                    weather_context_active = False
                    desktop_context_active = False
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            "等待“老叶老叶”唤醒", answer
                        )
                    try:
                        self._play(self.speaker.say, answer)
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, True)
                    state = "waiting"
                    wake_recognizer.Reset()
                    command_recognizer.Reset()
                    last_partial = ""
                elif state == "command" and (
                    is_vision_command(text)
                    or (
                        is_final
                        and vision_context_active
                        and is_vision_follow_up(text)
                    )
                ):
                    weather_context_active = False
                    answer_was_streamed = False
                    interrupt_action = None
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            "正在拍摄本次分析快照"
                        )
                        image_bytes = self.dashboard.store.request_snapshot(
                            self.config.camera_snapshot_timeout_seconds
                        )
                    else:
                        image_bytes = None
                    if image_bytes is None:
                        answer = "没有拍到同步画面，请确认摄像头网页已打开并允许权限。"
                        self.ollama.remember(text, answer)
                        vision_context_active = False
                    else:
                        print("[视觉] 正在分析当前画面……")
                        if self.dashboard:
                            self.dashboard.store.set_assistant_status("正在分析当前画面")
                        try:
                            answer, interrupt_action = self._speak_streamed_answer(
                                lambda on_segment: self.ollama.ask_vision(
                                    text,
                                    image_bytes,
                                    on_segment,
                                    self.playback_cancel,
                                )
                            )
                            answer_was_streamed = True
                            vision_context_active = True
                        except Exception as error:
                            print(f"[视觉分析失败] {error}", file=sys.stderr)
                            answer = "当前画面分析失败，请确认奥拉马视觉模型可以使用。"
                            vision_context_active = False
                    print(f"[视觉回答] {answer}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("视觉回答完成", answer)
                    if not answer_was_streamed:
                        try:
                            interrupt_action = self._play_interruptible(
                                self.speaker.say, answer, self.playback_cancel
                            )
                        except Exception as error:
                            print(f"[语音合成失败] {error}", file=sys.stderr)
                            self._play(self.speaker.chime, False)
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action), answer
                        )
                elif state == "command" and is_final:
                    vision_context_active = False
                    weather_context_active = False
                    print(f"[问题] {text}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("正在生成回答")
                    try:
                        answer, interrupt_action = self._speak_streamed_answer(
                            lambda on_segment: self.ollama.ask(
                                text,
                                on_segment,
                                self.playback_cancel,
                                (
                                    final_recognition_candidates
                                    if self.config.asr_accent_enhancement_enabled
                                    else None
                                ),
                            )
                        )
                        print(f"[模型回答] {answer}")
                        if self.dashboard:
                            self.dashboard.store.set_assistant_status("回答完成", answer)
                    except Exception as error:
                        interrupt_action = None
                        print(f"[模型调用失败] {error}", file=sys.stderr)
                        try:
                            self._play(
                                self.speaker.say,
                                "模型服务暂时无法回答，请稍后再试。",
                            )
                        except Exception:
                            self._play(self.speaker.chime, False)
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action)
                        )
        self._barge_in_stop.set()
        print("已收到关闭请求，语音助手安全退出。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MM101S USB 摄像头语音交互")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径"
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="列出 PortAudio 音频设备"
    )
    parser.add_argument(
        "--download-model", action="store_true", help="只下载并校验识别模型"
    )
    parser.add_argument(
        "--test-speaker", action="store_true", help="从配置的扬声器播放测试语音"
    )
    return parser


def main() -> int:
    configure_windows_console()
    args = build_parser().parse_args()
    if args.list_devices:
        list_audio_devices()
        return 0

    config = load_config(args.config.resolve())
    model_path = ensure_model(config.model_path, config.model_url)
    if args.download_model:
        print(f"模型已就绪：{model_path}")
        return 0

    input_device = find_audio_device(config.input_device, "input")
    output_device = find_audio_device(config.output_device, "output")
    print(
        f"录音：[{input_device.index}] {input_device.name} / "
        f"{input_device.host_api} / {input_device.sample_rate} Hz"
    )
    print(
        f"播放：[{output_device.index}] {output_device.name} / "
        f"{output_device.host_api} / {output_device.sample_rate} Hz"
    )
    speaker = Speaker(output_device, config)
    removed_audio_files = speaker.clear_cache()
    if removed_audio_files:
        print(f"已清理上次遗留的语音缓存：{removed_audio_files} 个文件")
    if args.test_speaker:
        speaker.say("老叶语音助手已连接成功。")
        print("扬声器测试完成。")
        return 0

    SetLogLevel(-1)
    ollama = ModelRouter(config, args.config.resolve())
    model_info = ollama.info()
    model_service_name = model_info["label"]
    online_tools = OnlineSearchTools(config)
    ollama_ready, ollama_status = ollama.check()
    if ollama_ready:
        print(f"{model_service_name}：已连接 / {ollama_status}")
        if config.llm_provider not in {"online", "qwen", "openai"}:
            try:
                print("正在预热 Ollama 模型……")
                ollama.warm_up()
                print("Ollama：模型已预热 / thinking 已关闭")
            except Exception as error:
                print(f"Ollama：模型预热失败，将在首次提问时重试 / {error}")
    else:
        print(f"{model_service_name}：不可用 / {ollama_status}")
    print("正在加载离线中文识别模型……")
    model = Model(str(model_path))
    dashboard = CameraDashboard(config, ollama) if config.web_enabled else None
    if dashboard:
        dashboard.start()
    try:
        VoiceAssistant(
            config,
            model,
            input_device,
            speaker,
            ollama,
            online_tools,
            dashboard,
        ).run()
    finally:
        if dashboard:
            dashboard.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已退出。")
        raise SystemExit(0)
    except Exception as error:
        print(f"\n启动失败：{error}", file=sys.stderr)
        raise SystemExit(1)
