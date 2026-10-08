"""配置文件加载与核心数据类型。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .paths import APP_DIR

DEFAULT_CONFIG = APP_DIR / "config.json"


@dataclass(frozen=True)
class Config:
    ollama_enabled: bool
    ollama_url: str
    ollama_model: str
    ollama_timeout_seconds: float
    ollama_keep_alive: str
    ollama_system_prompt: str
    tts_voice: str
    tts_rate: str
    tts_volume: str
    tts_proxy: str
    web_enabled: bool
    web_host: str
    web_port: int
    open_browser: bool
    tray_enabled: bool
    tray_notifications_enabled: bool
    remote_camera_enabled: bool
    remote_camera_api_base: str
    remote_camera_account: str
    remote_camera_password: str
    remote_camera_srs_host: str
    vision_api_enabled: bool
    vision_api_base: str
    vision_api_model: str


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Config(
        ollama_enabled=bool(raw.get("ollama_enabled", True)),
        ollama_url=str(raw.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/"),
        ollama_model=str(raw.get("ollama_model", "qwen3.5:4b")),
        ollama_timeout_seconds=float(raw.get("ollama_timeout_seconds", 120.0)),
        ollama_keep_alive=str(raw.get("ollama_keep_alive", "10m")),
        ollama_system_prompt=str(raw.get("ollama_system_prompt", "")),
        tts_voice=raw.get("tts_voice", "zh-CN-YunyangNeural"),
        tts_rate=raw.get("tts_rate", "+0%"),
        tts_volume=raw.get("tts_volume", "+0%"),
        tts_proxy=str(raw.get("tts_proxy", "")).strip(),
        web_enabled=bool(raw.get("web_enabled", True)),
        web_host=str(raw.get("web_host", "127.0.0.1")),
        web_port=int(raw.get("web_port", 8765)),
        open_browser=bool(raw.get("open_browser", True)),
        tray_enabled=bool(raw.get("tray_enabled", True)),
        tray_notifications_enabled=bool(raw.get("tray_notifications_enabled", True)),
        remote_camera_enabled=bool(raw.get("remote_camera_enabled", False)),
        remote_camera_api_base=str(raw.get("remote_camera_api_base", "")).rstrip("/"),
        remote_camera_account=str(raw.get("remote_camera_account", "")),
        remote_camera_password=str(raw.get("remote_camera_password", "")),
        remote_camera_srs_host=str(raw.get("remote_camera_srs_host", "")),
        vision_api_enabled=bool(raw.get("vision_api_enabled", False)),
        vision_api_base=str(raw.get("vision_api_base", "")).rstrip("/"),
        vision_api_model=str(raw.get("vision_api_model", "qwen3-vl-8b")),
    )
