"""配置文件加载与核心数据类型。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .paths import APP_DIR

DEFAULT_CONFIG = APP_DIR / "config.json"


@dataclass(frozen=True)
class Config:
    config_path: Path
    input_device: str | int
    output_device: str | int
    wake_phrases: tuple[str, ...]
    command_timeout_seconds: float
    conversation_history_turns: int
    asr_accent_enhancement_enabled: bool
    asr_max_alternatives: int
    barge_in_during_playback: bool
    tts_voice: str
    tts_rate: str
    tts_volume: str
    tts_proxy: str
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
    code_run_timeout_seconds: float
    web_enabled: bool
    web_host: str
    web_port: int
    open_browser: bool
    tray_enabled: bool
    tray_notifications_enabled: bool


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api: str
    sample_rate: int
    channels: int


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Config(
        config_path=path.resolve(),
        input_device=raw.get("input_device", "Deli-1080P-Camera-Audio"),
        output_device=raw.get("output_device", "Deli-1080P-Camera Audio"),
        wake_phrases=tuple(raw.get("wake_phrases", ["叮咚叮咚", "丁冬丁冬", "丁东丁东"])),
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
        barge_in_during_playback=bool(
            raw.get("barge_in_during_playback", False)
        ),
        tts_voice=raw.get("tts_voice", "zh-CN-YunyangNeural"),
        tts_rate=raw.get("tts_rate", "+0%"),
        tts_volume=raw.get("tts_volume", "+0%"),
        tts_proxy=str(raw.get("tts_proxy", "")).strip(),
        ollama_enabled=bool(raw.get("ollama_enabled", True)),
        ollama_url=str(raw.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/"),
        ollama_model=str(raw.get("ollama_model", "qwen3.5:4b")),
        ollama_timeout_seconds=float(raw.get("ollama_timeout_seconds", 120.0)),
        ollama_keep_alive=str(raw.get("ollama_keep_alive", "10m")),
        ollama_system_prompt=str(
            raw.get(
                "ollama_system_prompt",
                "你是叮咚，一个简洁、口语化的中文语音助手。",
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
        code_run_timeout_seconds=max(
            1.0, float(raw.get("code_run_timeout_seconds", 10.0))
        ),
        web_enabled=bool(raw.get("web_enabled", True)),
        web_host=str(raw.get("web_host", "127.0.0.1")),
        web_port=int(raw.get("web_port", 8765)),
        open_browser=bool(raw.get("open_browser", True)),
        tray_enabled=bool(raw.get("tray_enabled", True)),
        tray_notifications_enabled=bool(
            raw.get("tray_notifications_enabled", True)
        ),
    )
