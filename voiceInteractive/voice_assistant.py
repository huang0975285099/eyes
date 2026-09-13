from __future__ import annotations

import argparse
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
import socket
import sys
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
    tts_voice: str
    tts_rate: str
    tts_volume: str
    ollama_enabled: bool
    ollama_url: str
    ollama_model: str
    ollama_timeout_seconds: float
    ollama_keep_alive: str
    ollama_system_prompt: str
    internet_tools_enabled: bool
    internet_timeout_seconds: float
    internet_retry_count: int
    weather_default_location: str
    web_enabled: bool
    web_host: str
    web_port: int
    open_browser: bool
    camera_name_keywords: tuple[str, ...]
    camera_frame_max_age_seconds: float
    camera_snapshot_timeout_seconds: float
    model_path: Path
    model_url: str


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api: str
    sample_rate: int
    channels: int


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    model_path = Path(raw.get("model_path", "models/vosk-model-small-cn-0.22"))
    if not model_path.is_absolute():
        model_path = APP_DIR / model_path
    return Config(
        input_device=raw.get("input_device", "Deli-1080P-Camera-Audio"),
        output_device=raw.get("output_device", "Deli-1080P-Camera Audio"),
        wake_phrases=tuple(raw.get("wake_phrases", ["老 叶 老 叶", "老爷 老爷"])),
        command_timeout_seconds=float(raw.get("command_timeout_seconds", 8.0)),
        conversation_history_turns=max(
            1, int(raw.get("conversation_history_turns", 6))
        ),
        tts_voice=raw.get("tts_voice", "zh-CN-YunyangNeural"),
        tts_rate=raw.get("tts_rate", "+0%"),
        tts_volume=raw.get("tts_volume", "+0%"),
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
        weather_default_location=str(
            raw.get("weather_default_location", "Los Angeles")
        ).strip(),
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
        model_path=model_path,
        model_url=raw.get("model_url", DEFAULT_MODEL_URL),
    )


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
            "会下雨",
            "会不会下雨",
            "天气预报",
        )
    )


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

    location = text
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
    def __init__(self, device: AudioDevice, config: Config) -> None:
        self.device = device
        self.config = config

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
        return APP_DIR / ".tts-cache" / f"{digest}.mp3"

    def prepare(
        self, text: str, cancel_event: threading.Event | None = None
    ) -> np.ndarray | None:
        if cancel_event is not None and cancel_event.is_set():
            return None
        cache_path = self._cache_path(text)
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
            return None
        decoded = miniaudio.decode(
            audio_bytes, output_format=miniaudio.SampleFormat.SIGNED16
        )
        samples = np.asarray(decoded.samples, dtype=np.int16)
        samples = samples.reshape(-1, decoded.nchannels)
        samples = resample_pcm(samples, decoded.sample_rate, self.device.sample_rate)
        if samples.shape[1] == 1:
            samples = np.repeat(samples, 2, axis=1)
        return samples

    def play_prepared(
        self,
        samples: np.ndarray,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            return
        sd.play(
            samples,
            self.device.sample_rate,
            device=self.device.index,
            blocking=True,
        )

    def say(self, text: str, cancel_event: threading.Event | None = None) -> None:
        samples = self.prepare(text, cancel_event)
        if samples is not None:
            self.play_prepared(samples, cancel_event)


class OnlineSearchTools:
    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    # The configured home city is used most often. Keeping its coordinates
    # locally avoids a second network dependency before every weather request.
    KNOWN_LOCATIONS = {
        "成都": {
            "name": "成都",
            "admin1": "四川",
            "latitude": 30.5728,
            "longitude": 104.0668,
        },
        "成都市": {
            "name": "成都",
            "admin1": "四川",
            "latitude": 30.5728,
            "longitude": 104.0668,
        },
        "四川省成都市": {
            "name": "成都",
            "admin1": "四川",
            "latitude": 30.5728,
            "longitude": 104.0668,
        },
        "中华人民共和国四川省成都市": {
            "name": "成都",
            "admin1": "四川",
            "latitude": 30.5728,
            "longitude": 104.0668,
        },
    }

    WEATHER_CODES = {
        0: "晴",
        1: "晴间多云",
        2: "多云",
        3: "阴",
        45: "有雾",
        48: "有雾凇",
        51: "有轻微毛毛雨",
        53: "有毛毛雨",
        55: "有较强毛毛雨",
        56: "有冻毛毛雨",
        57: "有较强冻毛毛雨",
        61: "有小雨",
        63: "有中雨",
        65: "有大雨",
        66: "有冻雨",
        67: "有较强冻雨",
        71: "有小雪",
        73: "有中雪",
        75: "有大雪",
        77: "有米雪",
        80: "有小阵雨",
        81: "有中等阵雨",
        82: "有强阵雨",
        85: "有小阵雪",
        86: "有强阵雪",
        95: "有雷雨",
        96: "有雷雨和冰雹",
        99: "有强雷雨和冰雹",
    }

    def __init__(self, config: Config) -> None:
        self.config = config
        self.last_weather_location = ""
        self._location_cache: dict[str, dict] = {}

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
                with urllib.request.urlopen(
                    request, timeout=self.config.internet_timeout_seconds
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as error:
                last_error = error
                if attempt < self.config.internet_retry_count:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"联网请求失败：{last_error}") from last_error

    @staticmethod
    def _number(value) -> int:
        return int(round(float(value)))

    @staticmethod
    def _location_candidates(query: str) -> list[str]:
        candidates = [query.strip()]
        shortened = re.sub(r"^(中华人民共和国|中国)", "", query).strip()
        if shortened:
            candidates.append(shortened)
        city_match = re.search(r"([^省自治区]+市)", shortened)
        if city_match:
            candidates.append(city_match.group(1).removesuffix("市"))
        if "省" in shortened:
            province_tail = shortened.rsplit("省", 1)[-1]
            candidates.append(province_tail.split("市", 1)[0])
        candidates.append(re.sub(r"(市|省|区|县)$", "", shortened))
        return list(dict.fromkeys(candidate for candidate in candidates if candidate))

    def _find_location(self, query: str) -> dict:
        cache_key = query.casefold()
        if cache_key in self._location_cache:
            return self._location_cache[cache_key]
        for candidate in self._location_candidates(query):
            known_location = self.KNOWN_LOCATIONS.get(candidate)
            if known_location:
                location = dict(known_location)
                self._location_cache[cache_key] = location
                self._location_cache[str(location["name"]).casefold()] = location
                return location
            response = self._get_json(
                self.GEOCODING_URL,
                {
                    "name": candidate,
                    "count": 1,
                    "language": "zh",
                    "format": "json",
                },
            )
            results = response.get("results", [])
            if results:
                location = results[0]
                self._location_cache[cache_key] = location
                canonical_name = str(location.get("name", "")).casefold()
                if canonical_name:
                    self._location_cache[canonical_name] = location
                return location
        raise RuntimeError(f"没有找到地点“{query}”")

    def search_weather(self, question: str) -> str:
        if not self.config.internet_tools_enabled:
            raise RuntimeError("联网工具已在配置中关闭")
        location_query, day_indexes = parse_weather_query(
            question,
            self.last_weather_location or self.config.weather_default_location,
        )
        if not location_query:
            return "请告诉我需要查询哪个城市的天气。"

        location = self._find_location(location_query)
        self.last_weather_location = str(location.get("name", location_query))
        forecast = self._get_json(
            self.FORECAST_URL,
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": (
                    "temperature_2m,apparent_temperature,weather_code,"
                    "wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "timezone": "auto",
                "forecast_days": 3,
            },
        )
        daily = forecast.get("daily", {})
        day_names = ("今天", "明天", "后天")
        descriptions: list[str] = []
        for day_index in day_indexes:
            try:
                weather_code = int(daily["weather_code"][day_index])
                high = self._number(daily["temperature_2m_max"][day_index])
                low = self._number(daily["temperature_2m_min"][day_index])
                rain_probability = self._number(
                    daily["precipitation_probability_max"][day_index]
                )
            except (IndexError, KeyError, TypeError, ValueError) as error:
                raise RuntimeError("天气服务返回的数据不完整") from error
            condition = self.WEATHER_CODES.get(weather_code, "天气状况未知")
            if day_index == 0 and len(day_indexes) == 1:
                current = forecast.get("current", {})
                try:
                    temperature = self._number(current["temperature_2m"])
                    apparent = self._number(current["apparent_temperature"])
                    wind = self._number(current["wind_speed_10m"])
                    current_code = int(current["weather_code"])
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError("天气服务没有返回当前天气") from error
                condition = self.WEATHER_CODES.get(current_code, condition)
                descriptions.append(
                    f"今天{condition}，当前{temperature}度，体感{apparent}度，"
                    f"最高{high}度，最低{low}度，降雨概率{rain_probability}%，"
                    f"风速{wind}公里每小时"
                )
            else:
                descriptions.append(
                    f"{day_names[day_index]}{condition}，最高{high}度，"
                    f"最低{low}度，降雨概率{rain_probability}%"
                )

        location_name = str(location.get("name", location_query))
        admin1 = str(location.get("admin1", ""))
        if admin1 and admin1 != location_name:
            location_name = f"{location_name}，{admin1}"
        return f"{location_name}：" + "；".join(descriptions) + "。"


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
    ) -> str:
        messages = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {"role": "user", "content": question},
        ]
        answer = self._chat(messages, 0.4, 160, on_segment, cancel_event)
        if cancel_event is None or not cancel_event.is_set():
            self.remember(question, answer)
        return answer

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
                "assistant_status": self._assistant_status,
                "last_answer": self._last_answer,
            }

    def request_shutdown(self) -> None:
        with self._snapshot_ready:
            self._assistant_status = "正在关闭语音助手"
            self.shutdown_event.set()
            self._snapshot_ready.notify_all()


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, store: CameraFrameStore, config: Config):
        super().__init__(address, handler)
        self.store = store
        self.config = config


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
                {"camera_name_keywords": self.server.config.camera_name_keywords}
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
        if request_path not in {"/api/frame", "/api/snapshot"}:
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
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = CameraFrameStore()
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
        ollama: OllamaClient,
        online_tools: OnlineSearchTools,
        dashboard: CameraDashboard | None,
    ) -> None:
        self.config = config
        self.model = model
        self.input_device = input_device
        self.speaker = speaker
        self.ollama = ollama
        self.online_tools = online_tools
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

    def run(self) -> None:
        sample_rate = self.input_device.sample_rate
        wake_recognizer = _recognizer(
            self.model, sample_rate, list(self.config.wake_phrases)
        )
        command_recognizer = KaldiRecognizer(self.model, sample_rate)
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
        vision_context_active = False
        weather_context_active = False

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
                if state == "command" and time.monotonic() > command_deadline:
                    print("[会话结束] 一段时间没有继续提问，重新等待唤醒。")
                    self.ollama.end_conversation()
                    self.online_tools.start_conversation()
                    vision_context_active = False
                    weather_context_active = False
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
                    text = _result_text(recognizer.Result(), "text")
                else:
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
                    vision_context_active = False
                    weather_context_active = False
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

                if state == "command" and is_time_command(text):
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
                    print(f"[联网天气查询] {text}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("正在联网查询天气")
                    try:
                        answer = self.online_tools.search_weather(text)
                        weather_context_active = True
                        weather_succeeded = True
                    except Exception as error:
                        print(f"[联网天气查询失败] {error}", file=sys.stderr)
                        if "配置中关闭" in str(error):
                            answer = "天气查询功能暂时关闭。"
                        elif "没有找到地点" in str(error):
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
                    self._barge_in_stop.set()
                    vision_context_active = False
                    weather_context_active = False
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
                    vision_context_active = False
                    weather_context_active = False
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
                                text, on_segment, self.playback_cancel
                            )
                        )
                        print(f"[Ollama] {answer}")
                        if self.dashboard:
                            self.dashboard.store.set_assistant_status("回答完成", answer)
                    except Exception as error:
                        interrupt_action = None
                        print(f"[Ollama 调用失败] {error}", file=sys.stderr)
                        try:
                            self._play(
                                self.speaker.say,
                                "本地模型暂时无法回答，请确认奥拉马已经启动。",
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
    if args.test_speaker:
        speaker.say("老叶语音助手已连接成功。")
        print("扬声器测试完成。")
        return 0

    SetLogLevel(-1)
    ollama = OllamaClient(config)
    online_tools = OnlineSearchTools(config)
    ollama_ready, ollama_status = ollama.check()
    if ollama_ready:
        print(f"Ollama：已连接 / {ollama_status}")
        try:
            print("正在预热 Ollama 模型……")
            ollama.warm_up()
            print("Ollama：模型已预热 / thinking 已关闭")
        except Exception as error:
            print(f"Ollama：模型预热失败，将在首次提问时重试 / {error}")
    else:
        print(f"Ollama：不可用 / {ollama_status}")
    print("正在加载离线中文识别模型……")
    model = Model(str(model_path))
    dashboard = CameraDashboard(config) if config.web_enabled else None
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
