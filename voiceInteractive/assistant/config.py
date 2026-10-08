"""配置文件加载与核心数据类型。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

from .paths import APP_DIR

DEFAULT_CONFIG = APP_DIR / "config.json"


@dataclass(frozen=True)
class CameraMotionRule:
    enabled: bool = True
    sensitivity: int = 70
    min_area_percent: float = 0.8
    consecutive_frames: int = 3
    confirmation_seconds: float = 0.0
    cooldown_seconds: float = 5.0
    roi: tuple[float, float, float, float] | None = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "sensitivity": self.sensitivity,
            "min_area_percent": self.min_area_percent,
            "consecutive_frames": self.consecutive_frames,
            "confirmation_seconds": self.confirmation_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "roi": (
                dict(zip(("x", "y", "width", "height"), self.roi))
                if self.roi is not None else None
            ),
        }


def parse_camera_motion_rule(raw: dict, fallback: CameraMotionRule) -> CameraMotionRule:
    """Validate API/config motion settings; ROI coordinates are frame fractions."""
    if not isinstance(raw, dict):
        raise ValueError("摄像头监控规则必须是对象")
    enabled = raw.get("enabled", fallback.enabled)
    if not isinstance(enabled, bool):
        raise ValueError("监控规则 enabled 必须是布尔值")

    def number(key: str, low: float, high: float) -> float:
        value = raw.get(key, getattr(fallback, key))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} 必须是数字")
        result = float(value)
        if not math.isfinite(result) or not low <= result <= high:
            raise ValueError(f"{key} 必须在 {low:g}～{high:g} 之间")
        return result

    sensitivity = number("sensitivity", 1, 100)
    frames = number("consecutive_frames", 1, 30)
    if not sensitivity.is_integer() or not frames.is_integer():
        raise ValueError("灵敏度和确认帧数必须是整数")
    roi_value = raw.get("roi", fallback.roi)
    if roi_value is None:
        roi = None
    else:
        if isinstance(roi_value, tuple):
            roi_value = dict(zip(("x", "y", "width", "height"), roi_value))
        if not isinstance(roi_value, dict):
            raise ValueError("监控区域必须是矩形对象或 null")
        try:
            roi = tuple(float(roi_value[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("监控区域需要 x、y、width、height") from error
        x, y, width, height = roi
        if (
            not all(math.isfinite(value) for value in roi)
            or x < 0 or y < 0
            or width < 0.05 or height < 0.05
            or x + width > 1.000001 or y + height > 1.000001
        ):
            raise ValueError("监控区域超出画面，宽高至少为画面的 5%")
        roi = tuple(round(value, 4) for value in roi)
    return CameraMotionRule(
        enabled=enabled,
        sensitivity=int(sensitivity),
        min_area_percent=number("min_area_percent", 0.05, 50),
        consecutive_frames=int(frames),
        confirmation_seconds=number("confirmation_seconds", 0, 30),
        cooldown_seconds=number("cooldown_seconds", 0.5, 3600),
        roi=roi,
    )


@dataclass(frozen=True)
class NativeCameraConfig:
    """One OpenCV camera monitored by the background service."""

    id: str
    name: str
    index: int
    enabled: bool
    primary: bool
    motion: CameraMotionRule | None = None


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
    camera_name_keywords: tuple[str, ...]
    camera_frame_max_age_seconds: float
    camera_snapshot_timeout_seconds: float
    native_camera_enabled: bool
    native_camera_index: int
    native_cameras: tuple[NativeCameraConfig, ...]
    native_camera_width: int
    native_camera_height: int
    native_camera_fps: int
    native_camera_save_snapshots: bool
    native_camera_event_retention_days: int
    native_camera_event_max_megabytes: int
    native_camera_cleanup_interval_minutes: int
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


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api: str
    sample_rate: int
    channels: int


def _load_native_cameras(raw: dict) -> tuple[NativeCameraConfig, ...]:
    legacy_rule = CameraMotionRule(
        sensitivity=max(1, min(100, int(raw.get("person_motion_sensitivity", 70)))),
        min_area_percent=max(0.05, min(50.0, float(raw.get("person_motion_min_area_percent", 0.8)))),
        consecutive_frames=max(1, min(30, int(raw.get("person_motion_consecutive_frames", 3)))),
        cooldown_seconds=max(0.5, float(raw.get("person_motion_cooldown_seconds", 5.0))),
    )
    configured = raw.get("native_cameras")
    if not isinstance(configured, list) or not configured:
        configured = [
            {
                "id": "camera_1",
                "name": "USB Camera 1",
                "index": raw.get("native_camera_index", 0),
                "enabled": True,
                "primary": True,
            }
        ]

    cameras: list[NativeCameraConfig] = []
    used_ids: set[str] = set()
    used_indexes: set[int] = set()
    for position, item in enumerate(configured, start=1):
        if not isinstance(item, dict):
            continue
        index = max(0, min(99, int(item.get("index", position - 1))))
        if index in used_indexes:
            continue
        camera_id = str(item.get("id", f"camera_{position}")).strip()
        camera_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in camera_id
        ).strip("_")
        if not camera_id:
            camera_id = f"camera_{position}"
        if camera_id in used_ids:
            camera_id = f"{camera_id}_{position}"
        used_ids.add(camera_id)
        used_indexes.add(index)
        cameras.append(
            NativeCameraConfig(
                id=camera_id,
                name=str(item.get("name", f"USB Camera {position}")).strip()
                or f"USB Camera {position}",
                index=index,
                enabled=bool(item.get("enabled", True)),
                primary=bool(item.get("primary", False)),
                motion=(
                    parse_camera_motion_rule(item["motion"], legacy_rule)
                    if isinstance(item.get("motion"), dict) else legacy_rule
                ),
            )
        )

    if not cameras:
        cameras.append(NativeCameraConfig("camera_1", "USB Camera 1", 0, True, True))
    if not any(camera.enabled and camera.primary for camera in cameras):
        first_enabled = next((camera for camera in cameras if camera.enabled), cameras[0])
        cameras = [
            NativeCameraConfig(
                camera.id,
                camera.name,
                camera.index,
                camera.enabled,
                camera.id == first_enabled.id,
                camera.motion,
            )
            for camera in cameras
        ]
    return tuple(cameras)


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    native_cameras = _load_native_cameras(raw)
    primary_camera = next(
        (camera for camera in native_cameras if camera.enabled and camera.primary),
        native_cameras[0],
    )
    yolo_model_path = Path(raw.get("yolo_model_path", "models/yolov8n.pt"))
    if not yolo_model_path.is_absolute():
        yolo_model_path = APP_DIR / yolo_model_path
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
        native_camera_enabled=bool(raw.get("native_camera_enabled", False)),
        native_camera_index=primary_camera.index,
        native_cameras=native_cameras,
        native_camera_width=max(320, int(raw.get("native_camera_width", 1280))),
        native_camera_height=max(240, int(raw.get("native_camera_height", 720))),
        native_camera_fps=max(1, min(30, int(raw.get("native_camera_fps", 5)))),
        native_camera_save_snapshots=bool(
            raw.get("native_camera_save_snapshots", True)
        ),
        native_camera_event_retention_days=max(
            1, int(raw.get("native_camera_event_retention_days", 7))
        ),
        native_camera_event_max_megabytes=max(
            50, int(raw.get("native_camera_event_max_megabytes", 1024))
        ),
        native_camera_cleanup_interval_minutes=max(
            5, int(raw.get("native_camera_cleanup_interval_minutes", 60))
        ),
        person_monitor_enabled=bool(raw.get("person_monitor_enabled", False)),
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
    )
