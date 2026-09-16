"""配置文件加载与核心数据类型。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .paths import APP_DIR

DEFAULT_CONFIG = APP_DIR / "config.json"
DEFAULT_MODEL_URL = (
    "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
)


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
    tray_enabled: bool
    tray_notifications_enabled: bool
    camera_name_keywords: tuple[str, ...]
    camera_frame_max_age_seconds: float
    camera_snapshot_timeout_seconds: float
    native_camera_enabled: bool
    native_camera_index: int
    native_camera_fallback_seconds: float
    native_camera_width: int
    native_camera_height: int
    native_camera_fps: int
    native_camera_save_snapshots: bool
    native_camera_event_retention_days: int
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
    face_recognition_enabled: bool
    face_python_executable: str
    face_detector_model_path: Path
    face_recognizer_model_path: Path
    face_database_path: Path
    face_detector_score_threshold: float
    face_match_threshold: float
    face_match_margin: float
    face_min_size: int
    face_min_blur: float
    face_result_max_age_seconds: float
    face_timeout_seconds: float
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
    online_config_db_value = str(raw.get("online_config_db", "")).strip()
    online_config_db = Path(online_config_db_value) if online_config_db_value else None
    if online_config_db is not None and not online_config_db.is_absolute():
        online_config_db = APP_DIR / online_config_db
    yolo_model_path = Path(raw.get("yolo_model_path", "models/yolov8n.pt"))
    if not yolo_model_path.is_absolute():
        yolo_model_path = APP_DIR / yolo_model_path
    face_detector_model_path = Path(
        raw.get(
            "face_detector_model_path",
            "models/face/face_detection_yunet_2023mar.onnx",
        )
    )
    if not face_detector_model_path.is_absolute():
        face_detector_model_path = APP_DIR / face_detector_model_path
    face_recognizer_model_path = Path(
        raw.get(
            "face_recognizer_model_path",
            "models/face/face_recognition_sface_2021dec.onnx",
        )
    )
    if not face_recognizer_model_path.is_absolute():
        face_recognizer_model_path = APP_DIR / face_recognizer_model_path
    face_database_path = Path(raw.get("face_database_path", "data/faces/faces.db"))
    if not face_database_path.is_absolute():
        face_database_path = APP_DIR / face_database_path
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
        tray_enabled=bool(raw.get("tray_enabled", True)),
        tray_notifications_enabled=bool(
            raw.get("tray_notifications_enabled", True)
        ),
        camera_name_keywords=tuple(
            raw.get("camera_name_keywords", ["Deli", "1080P", "MM101S"])
        ),
        camera_frame_max_age_seconds=float(
            raw.get("camera_frame_max_age_seconds", 6.0)
        ),
        camera_snapshot_timeout_seconds=max(
            0.5, float(raw.get("camera_snapshot_timeout_seconds", 3.0))
        ),
        native_camera_enabled=bool(raw.get("native_camera_enabled", True)),
        native_camera_index=max(0, min(9, int(raw.get("native_camera_index", 0)))),
        native_camera_fallback_seconds=max(
            2.0, float(raw.get("native_camera_fallback_seconds", 5.0))
        ),
        native_camera_width=max(320, int(raw.get("native_camera_width", 1280))),
        native_camera_height=max(240, int(raw.get("native_camera_height", 720))),
        native_camera_fps=max(1, min(30, int(raw.get("native_camera_fps", 5)))),
        native_camera_save_snapshots=bool(
            raw.get("native_camera_save_snapshots", True)
        ),
        native_camera_event_retention_days=max(
            1, int(raw.get("native_camera_event_retention_days", 7))
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
        face_recognition_enabled=bool(raw.get("face_recognition_enabled", False)),
        face_python_executable=str(
            raw.get("face_python_executable", raw.get("yolo_python_executable", "python"))
        ).strip(),
        face_detector_model_path=face_detector_model_path,
        face_recognizer_model_path=face_recognizer_model_path,
        face_database_path=face_database_path,
        face_detector_score_threshold=max(
            0.5, min(0.99, float(raw.get("face_detector_score_threshold", 0.88)))
        ),
        face_match_threshold=max(
            0.1, min(0.95, float(raw.get("face_match_threshold", 0.48)))
        ),
        face_match_margin=max(
            0.0, min(0.5, float(raw.get("face_match_margin", 0.05)))
        ),
        face_min_size=max(40, int(raw.get("face_min_size", 80))),
        face_min_blur=max(0.0, float(raw.get("face_min_blur", 35.0))),
        face_result_max_age_seconds=max(
            2.0, float(raw.get("face_result_max_age_seconds", 5.0))
        ),
        face_timeout_seconds=max(
            2.0, float(raw.get("face_timeout_seconds", 20.0))
        ),
        model_path=model_path,
        model_url=raw.get("model_url", DEFAULT_MODEL_URL),
    )
