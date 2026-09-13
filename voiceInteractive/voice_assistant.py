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
        wake_phrases=tuple(raw.get("wake_phrases", ["小布 小布", "小步 小步"])),
        command_timeout_seconds=float(raw.get("command_timeout_seconds", 8.0)),
        conversation_history_turns=max(
            1, int(raw.get("conversation_history_turns", 6))
        ),
        tts_voice=raw.get("tts_voice", "zh-CN-XiaoxiaoNeural"),
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
                "你是小布，一个简洁、口语化的中文语音助手。",
            )
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


def contains_wake_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    normalized = normalize_text(text)
    return any(normalize_text(phrase) in normalized for phrase in phrases)


def is_time_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in ("现在几点", "几点了", "几点"))


def is_exit_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in (
            "退出助手",
            "关闭助手",
            "停止助手",
            "退出小布",
            "关闭小布",
            "停止小布",
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

    def say(self, text: str) -> None:
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
        decoded = miniaudio.decode(
            audio_bytes, output_format=miniaudio.SampleFormat.SIGNED16
        )
        samples = np.asarray(decoded.samples, dtype=np.int16)
        samples = samples.reshape(-1, decoded.nchannels)
        samples = resample_pcm(samples, decoded.sample_rate, self.device.sample_rate)
        if samples.shape[1] == 1:
            samples = np.repeat(samples, 2, axis=1)
        sd.play(
            samples,
            self.device.sample_rate,
            device=self.device.index,
            blocking=True,
        )


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

    def ask(self, question: str) -> str:
        messages = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {"role": "user", "content": question},
        ]
        response = self._request(
            "/api/chat",
            {
                "model": self.config.ollama_model,
                "messages": messages,
                "stream": False,
                "think": False,
                "keep_alive": self.config.ollama_keep_alive,
                "options": {"temperature": 0.4, "num_predict": 160},
            },
        )
        answer = str(response.get("message", {}).get("content", "")).strip()
        if not answer:
            raise RuntimeError("Ollama 没有返回回答")
        self.remember(question, answer)
        return answer

    def ask_vision(self, question: str, image_bytes: bytes) -> str:
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
        response = self._request(
            "/api/chat",
            {
                "model": self.config.ollama_model,
                "messages": messages,
                "stream": False,
                "think": False,
                "keep_alive": self.config.ollama_keep_alive,
                "options": {"temperature": 0.2, "num_predict": 180},
            },
        )
        answer = str(response.get("message", {}).get("content", "")).strip()
        if not answer:
            raise RuntimeError("Ollama 没有返回画面说明")
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
        dashboard: CameraDashboard | None,
    ) -> None:
        self.config = config
        self.model = model
        self.input_device = input_device
        self.speaker = speaker
        self.ollama = ollama
        self.dashboard = dashboard
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=80)
        self.speaking = threading.Event()

    def _audio_callback(self, indata, frames, timing, status) -> None:
        if status:
            print(f"\n音频提示：{status}", file=sys.stderr)
        if self.speaking.is_set():
            return
        try:
            self.audio_queue.put_nowait(bytes(indata))
        except queue.Full:
            try:
                self.audio_queue.get_nowait()
                self.audio_queue.put_nowait(bytes(indata))
            except queue.Empty:
                pass

    def _clear_audio(self) -> None:
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                return

    def _play(self, action, *args) -> None:
        self.speaking.set()
        self._clear_audio()
        try:
            action(*args)
        finally:
            self._clear_audio()
            self.speaking.clear()

    def run(self) -> None:
        sample_rate = self.input_device.sample_rate
        wake_recognizer = _recognizer(
            self.model, sample_rate, list(self.config.wake_phrases)
        )
        command_recognizer = KaldiRecognizer(self.model, sample_rate)
        state = "waiting"
        command_deadline = 0.0
        last_partial = ""
        vision_context_active = False

        print("\n已启动。请说：小布小布")
        print("听到“我在”后开始提问（按 Ctrl+C 退出）\n")
        if self.dashboard:
            self.dashboard.store.set_assistant_status("等待“小布小布”唤醒")

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
                    vision_context_active = False
                    state = "waiting"
                    wake_recognizer.Reset()
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("等待“小布小布”唤醒")

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
                    vision_context_active = False
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
                    response = format_time_zh(datetime.now().astimezone())
                    self.ollama.remember(text, response)
                    print(f"[回答] {response}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("回答完成", response)
                    try:
                        self._play(self.speaker.say, response)
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
                            "可以继续提问，无需再次唤醒", response
                        )
                elif state == "command" and is_final and is_exit_command(text):
                    self.ollama.end_conversation()
                    vision_context_active = False
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
                    vision_context_active = False
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            "等待“小布小布”唤醒", answer
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
                            answer = self.ollama.ask_vision(text, image_bytes)
                            vision_context_active = True
                        except Exception as error:
                            print(f"[视觉分析失败] {error}", file=sys.stderr)
                            answer = "当前画面分析失败，请确认奥拉马视觉模型可以使用。"
                            vision_context_active = False
                    print(f"[视觉回答] {answer}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("视觉回答完成", answer)
                    try:
                        self._play(self.speaker.say, answer)
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
                            "可以继续提问，无需再次唤醒", answer
                        )
                elif state == "command" and is_final:
                    vision_context_active = False
                    print(f"[问题] {text}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("正在生成回答")
                    try:
                        answer = self.ollama.ask(text)
                        print(f"[Ollama] {answer}")
                        if self.dashboard:
                            self.dashboard.store.set_assistant_status("回答完成", answer)
                        self._play(self.speaker.say, answer)
                    except Exception as error:
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
                            "可以继续提问，无需再次唤醒"
                        )
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
        speaker.say("小布语音助手已连接成功。")
        print("扬声器测试完成。")
        return 0

    SetLogLevel(-1)
    ollama = OllamaClient(config)
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
        VoiceAssistant(config, model, input_device, speaker, ollama, dashboard).run()
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
