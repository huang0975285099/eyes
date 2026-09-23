"""浏览器关闭后接管一个或多个 USB 摄像头的本机后台监控。"""

from __future__ import annotations

import threading
import time
import json
import os
import tempfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import cv2
import numpy as np

from .config import (
    CameraMotionRule,
    Config,
    NativeCameraConfig,
    parse_camera_motion_rule,
)
from .paths import APP_DIR

if TYPE_CHECKING:
    from .camera import CameraFrameStore, PersonPresenceMonitor
    from .face import FaceRecognitionService
    from .gesture import GestureService


def list_native_cameras(max_index: int = 9) -> None:
    """Probe OpenCV indexes for the camera configuration diagnostic."""
    found = 0
    print("OpenCV 摄像头索引：")
    for index in range(max_index + 1):
        capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not capture.isOpened():
            capture.release()
            continue
        ok, frame = capture.read()
        if ok and frame is not None:
            height, width = frame.shape[:2]
            print(f"  [{index}] 可用 / {width}x{height}")
            found += 1
        capture.release()
    if not found:
        print("  未找到可读取的摄像头；请先关闭占用摄像头的浏览器或程序。")


@dataclass(frozen=True)
class MotionSettings:
    sensitivity: int
    min_area_percent: float
    consecutive_frames: int
    cooldown_seconds: float
    confirmation_seconds: float = 0.0


class MotionDetector:
    """背景差分检测器；连续多帧变化后才触发，降低光照噪声误报。"""

    def __init__(self) -> None:
        self.background: np.ndarray | None = None
        self.background_started_at = 0.0
        self.changed_frames = 0
        self.change_started_at = 0.0

    def reset(self) -> None:
        self.background = None
        self.background_started_at = 0.0
        self.changed_frames = 0
        self.change_started_at = 0.0

    def process(
        self,
        frame: np.ndarray,
        settings: MotionSettings,
        now: float,
        last_alert_at: float,
        roi: tuple[float, float, float, float] | None = None,
    ) -> tuple[float, bool]:
        height, width = frame.shape[:2]
        if height <= 0 or width <= 0:
            return 0.0, False
        if roi is not None:
            x, y, roi_width, roi_height = roi
            left = min(width - 1, max(0, int(round(x * width))))
            top = min(height - 1, max(0, int(round(y * height))))
            right = min(width, max(left + 1, int(round((x + roi_width) * width))))
            bottom = min(height, max(top + 1, int(round((y + roi_height) * height))))
            frame = frame[top:bottom, left:right]
            height, width = frame.shape[:2]
        scale = min(1.0, 640.0 / float(width))
        small = (
            cv2.resize(frame, None, fx=scale, fy=scale)
            if scale < 1.0
            else frame.copy()
        )
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.background is None:
            self.background = gray.astype("float")
            self.background_started_at = now
            return 0.0, False

        delta = cv2.absdiff(gray, cv2.convertScaleAbs(self.background))
        threshold = int(round(42 - (settings.sensitivity - 1) * 30 / 99))
        mask = cv2.threshold(delta, threshold, 255, cv2.THRESH_BINARY)[1]
        mask = cv2.dilate(mask, None, iterations=2)
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        total_area = float(small.shape[0] * small.shape[1])
        # Area percentage is relative to the selected region, even for a
        # small ROI; a fixed full-frame pixel floor would override the rule.
        minimum_area = max(1.0, total_area * settings.min_area_percent / 100.0)
        changed_area = sum(
            cv2.contourArea(contour)
            for contour in contours
            if cv2.contourArea(contour) >= minimum_area
        )
        significant = changed_area > 0
        if significant:
            if self.changed_frames == 0:
                self.change_started_at = now
            self.changed_frames += 1
        else:
            self.changed_frames = 0
            self.change_started_at = 0.0
        triggered = (
            now - self.background_started_at >= 1.0
            and self.changed_frames >= settings.consecutive_frames
            and now - self.change_started_at >= settings.confirmation_seconds
            and now - last_alert_at >= settings.cooldown_seconds
        )
        if triggered:
            self.changed_frames = 0
            self.change_started_at = 0.0
            self.background = gray.astype("float")
            self.background_started_at = now
        else:
            cv2.accumulateWeighted(
                gray, self.background, 0.002 if significant else 0.025
            )
        return min(100.0, changed_area / total_area * 100.0), triggered


class MotionEventArchive:
    """保存变化快照，并按保留天数与容量上限定期清理。"""

    def __init__(
        self,
        retention_days: int,
        enabled: bool,
        max_megabytes: int = 1024,
        cleanup_interval_minutes: int = 60,
    ) -> None:
        self.directory = APP_DIR / "data" / "events"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.enabled = enabled
        self.max_bytes = max_megabytes * 1024 * 1024
        self.cleanup_interval_seconds = cleanup_interval_minutes * 60.0
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._last_cleanup_at = 0.0
        self._events: deque[dict] = deque(maxlen=50)
        self.cleanup(force=True)
        paths = sorted(
            self.directory.glob("motion_*.jpg"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:50]
        for path in paths:
            timestamp = datetime.fromtimestamp(path.stat().st_mtime)
            self._events.append(
                {
                    "id": path.stem.removeprefix("motion_"),
                    "display_time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "snapshot_url": f"/api/motion-events/{path.name}",
                }
            )

    def cleanup(self, force: bool = False) -> int:
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - self._last_cleanup_at
            < self.cleanup_interval_seconds
        ):
            return 0
        if not self._cleanup_lock.acquire(blocking=False):
            return 0
        try:
            self._last_cleanup_at = now_monotonic
            cutoff = datetime.now() - timedelta(days=self.retention_days)
            removed = 0
            retained: list[tuple[Path, float, int]] = []
            for path in self.directory.glob("motion_*.jpg"):
                try:
                    stat = path.stat()
                    if datetime.fromtimestamp(stat.st_mtime) < cutoff:
                        path.unlink()
                        removed += 1
                    else:
                        retained.append((path, stat.st_mtime, stat.st_size))
                except OSError:
                    continue

            total_bytes = sum(item[2] for item in retained)
            if total_bytes > self.max_bytes:
                for path, _, size in sorted(retained, key=lambda item: item[1]):
                    try:
                        path.unlink()
                        total_bytes -= size
                        removed += 1
                    except OSError:
                        continue
                    if total_bytes <= self.max_bytes:
                        break
            return removed
        finally:
            self._cleanup_lock.release()

    def record(
        self,
        jpeg: bytes,
        score: float,
        camera_id: str = "camera_1",
        camera_name: str = "USB Camera 1",
    ) -> dict:
        now = datetime.now()
        event_id = f"{camera_id}_{now.strftime('%Y%m%d_%H%M%S_%f')}"
        event = {
            "id": event_id,
            "display_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "motion_score": round(score, 2),
            "camera_id": camera_id,
            "camera_name": camera_name,
            "snapshot_url": None,
        }
        if self.enabled:
            filename = f"motion_{event_id}.jpg"
            try:
                (self.directory / filename).write_bytes(jpeg)
                event["snapshot_url"] = f"/api/motion-events/{filename}"
            except OSError:
                # 磁盘只读或空间不足时，监控和通知仍应继续工作。
                pass
        with self._lock:
            self._events.appendleft(event)
        # 这里只做廉价的时间判断；实际目录扫描默认每小时最多一次。
        self.cleanup()
        return event

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    def resolve(self, filename: str) -> Path | None:
        if not filename.startswith("motion_") or not filename.endswith(".jpg"):
            return None
        if Path(filename).name != filename:
            return None
        path = (self.directory / filename).resolve()
        if path.parent != self.directory.resolve() or not path.is_file():
            return None
        return path


@dataclass
class _CameraRuntime:
    config: NativeCameraConfig
    motion_rule: CameraMotionRule = field(default_factory=CameraMotionRule)
    detector: MotionDetector = field(default_factory=MotionDetector)
    capture: cv2.VideoCapture | None = None
    thread: threading.Thread | None = None
    state: str = "等待后台监控"
    error: str = ""
    active: bool = False
    fps: float = 0.0
    motion_score: float = 0.0
    last_alert_at: float = 0.0
    last_frame_at: float = 0.0
    last_face_submit_at: float = 0.0
    last_gesture_submit_at: float = 0.0
    latest_jpeg: bytes | None = None
    frame_sequence: int = 0
    preview_clients: int = 0
    black_frames: int = 0


class NativeCameraMonitor:
    """网页画面中断后接管配置中的全部 USB 摄像头。"""

    def __init__(
        self,
        config: Config,
        store: CameraFrameStore,
        presence_monitor: PersonPresenceMonitor,
        face_service: FaceRecognitionService,
        gesture_service: GestureService | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.presence_monitor = presence_monitor
        self.face_service = face_service
        self.gesture_service = gesture_service
        self.archive = MotionEventArchive(
            config.native_camera_event_retention_days,
            config.native_camera_save_snapshots,
            config.native_camera_event_max_megabytes,
            config.native_camera_cleanup_interval_minutes,
        )
        self.settings = MotionSettings(
            config.person_motion_sensitivity,
            config.person_motion_min_area_percent,
            config.person_motion_consecutive_frames,
            config.person_motion_cooldown_seconds,
        )
        self._enabled = config.native_camera_enabled
        self._lock = threading.RLock()
        self._motion_update_lock = threading.Lock()
        self._frame_ready = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._listeners: list[Callable[[dict], None]] = []
        self._cameras = {
            camera.id: _CameraRuntime(
                camera,
                motion_rule=camera.motion or CameraMotionRule(
                    sensitivity=self.settings.sensitivity,
                    min_area_percent=self.settings.min_area_percent,
                    consecutive_frames=self.settings.consecutive_frames,
                    confirmation_seconds=0.0,
                    cooldown_seconds=self.settings.cooldown_seconds,
                ),
            )
            for camera in config.native_cameras
            if camera.enabled
        }

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def has_camera(self, camera_id: str) -> bool:
        with self._lock:
            return camera_id in self._cameras

    def motion_rule(self, camera_id: str) -> dict:
        with self._lock:
            runtime = self._cameras.get(camera_id)
            if runtime is None:
                raise KeyError(camera_id)
            return runtime.motion_rule.to_dict()

    def update_motion_rule(self, camera_id: str, payload: dict) -> dict:
        """Persist one camera's rule and apply it without restarting capture."""
        with self._motion_update_lock:
            with self._lock:
                runtime = self._cameras.get(camera_id)
                if runtime is None:
                    raise ValueError("没有找到该摄像头")
                new_rule = parse_camera_motion_rule(payload, runtime.motion_rule)
            config_path = self.config.config_path
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            cameras = raw.get("native_cameras")
            if not isinstance(cameras, list):
                raise ValueError("配置文件缺少 native_cameras")
            target = next(
                (camera for camera in cameras if isinstance(camera, dict) and camera.get("id") == camera_id),
                None,
            )
            if target is None:
                raise ValueError("配置文件中没有找到该摄像头")
            target["motion"] = new_rule.to_dict()
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=config_path.parent,
                    prefix=f".{config_path.name}.", suffix=".tmp", delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    json.dump(raw, temporary, ensure_ascii=False, indent=2)
                    temporary.write("\n")
                os.replace(temporary_path, config_path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            with self._lock:
                runtime.motion_rule = new_rule
                runtime.detector.reset()
                runtime.motion_score = 0.0
                runtime.last_alert_at = 0.0
            return new_rule.to_dict()

    def primary_camera_id(self) -> str | None:
        with self._lock:
            for camera_id, runtime in self._cameras.items():
                if runtime.config.primary:
                    return camera_id
            return next(iter(self._cameras), None)

    def secondary_camera_id(self) -> str | None:
        """返回第一个非主摄像头的已启用摄像头 ID（右眼）。"""
        with self._lock:
            for camera_id, runtime in self._cameras.items():
                if not runtime.config.primary:
                    return camera_id
            return None

    def add_event_listener(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def set_enabled(self, enabled: bool) -> None:
        with self._frame_ready:
            self._enabled = enabled
            for runtime in self._cameras.values():
                runtime.state = "等待后台监控" if enabled else "无人值守监控已关闭"
                runtime.error = ""
            self._frame_ready.notify_all()
        if enabled:
            resume_face = getattr(self.face_service, "resume", None)
            if callable(resume_face):
                resume_face()
            if self.gesture_service is not None:
                resume_gesture = getattr(self.gesture_service, "resume", None)
                if callable(resume_gesture):
                    resume_gesture()
        else:
            # Preview may keep the camera open, but must not keep queued or
            # in-flight recognition actions alive after monitoring is stopped.
            pause_face = getattr(self.face_service, "pause", None)
            if callable(pause_face):
                pause_face()
            if self.gesture_service is not None:
                pause_gesture = getattr(self.gesture_service, "pause", None)
                if callable(pause_gesture):
                    pause_gesture()

    def ensure_enabled(self) -> None:
        """视觉子功能依赖后台帧源，调用方开启时顺带拉起监控（已开启则不动作）。"""
        if not self.enabled:
            self.set_enabled(True)

    def preview_frames(self, camera_id: str):
        """Yield only the newest JPEG for an MJPEG client; old frames never queue."""
        with self._frame_ready:
            runtime = self._cameras.get(camera_id)
            if runtime is None:
                raise KeyError(camera_id)
            runtime.preview_clients += 1
            last_sequence = (
                runtime.frame_sequence - 1
                if runtime.latest_jpeg is not None
                else runtime.frame_sequence
            )
            self._frame_ready.notify_all()
        try:
            while not self._stop_event.is_set():
                with self._frame_ready:
                    self._frame_ready.wait_for(
                        lambda: self._stop_event.is_set()
                        or runtime.frame_sequence > last_sequence,
                        timeout=2.0,
                    )
                    if self._stop_event.is_set():
                        return
                    if runtime.latest_jpeg is None or runtime.frame_sequence <= last_sequence:
                        if not runtime.error:
                            continue
                        placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
                        cv2.putText(
                            placeholder,
                            "Camera signal unavailable",
                            (120, 225),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.8,
                            (110, 140, 132),
                            2,
                            cv2.LINE_AA,
                        )
                        encoded_ok, encoded = cv2.imencode(".jpg", placeholder)
                        if not encoded_ok:
                            continue
                        frame = encoded.tobytes()
                    else:
                        last_sequence = runtime.frame_sequence
                        frame = runtime.latest_jpeg
                yield frame
        finally:
            with self._frame_ready:
                runtime.preview_clients = max(0, runtime.preview_clients - 1)
                self._frame_ready.notify_all()

    def preview_frame(self, camera_id: str, timeout: float = 4.0) -> bytes | None:
        """Return a recent frame, starting capture temporarily when required."""
        with self._frame_ready:
            runtime = self._cameras.get(camera_id)
            if runtime is None:
                raise KeyError(camera_id)
            runtime.preview_clients += 1
            recent = (
                runtime.active
                and runtime.latest_jpeg is not None
                and time.time() - runtime.last_frame_at < 2.0
            )
            initial_sequence = runtime.frame_sequence - 1 if recent else runtime.frame_sequence
            self._frame_ready.notify_all()
            try:
                self._frame_ready.wait_for(
                    lambda: self._stop_event.is_set()
                    or (
                        runtime.active
                        and time.time() - runtime.last_frame_at < 2.0
                        and runtime.latest_jpeg is not None
                        and runtime.frame_sequence > initial_sequence
                    ),
                    timeout=max(0.1, timeout),
                )
                return (
                    runtime.latest_jpeg
                    if runtime.active
                    and runtime.frame_sequence > initial_sequence
                    and time.time() - runtime.last_frame_at < 2.0
                    else None
                )
            finally:
                runtime.preview_clients = max(0, runtime.preview_clients - 1)
                self._frame_ready.notify_all()

    def capture_frames(
        self, camera_id: str, count: int = 3, timeout: float = 4.0
    ) -> list[bytes]:
        """Capture distinct recent frames without leaving monitoring enabled."""
        deadline = time.monotonic() + max(0.1, timeout)
        with self._frame_ready:
            runtime = self._cameras.get(camera_id)
            if runtime is None:
                raise KeyError(camera_id)
            runtime.preview_clients += 1
            recent = (
                runtime.active
                and runtime.latest_jpeg is not None
                and time.time() - runtime.last_frame_at < 2.0
            )
            last_sequence = runtime.frame_sequence - 1 if recent else runtime.frame_sequence
            self._frame_ready.notify_all()
            frames: list[bytes] = []
            try:
                while len(frames) < max(1, count):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._frame_ready.wait_for(
                        lambda: self._stop_event.is_set()
                        or (
                            runtime.active
                            and runtime.latest_jpeg is not None
                            and runtime.frame_sequence > last_sequence
                            and time.time() - runtime.last_frame_at < 2.0
                        ),
                        timeout=remaining,
                    )
                    if self._stop_event.is_set():
                        break
                    if not runtime.active or runtime.frame_sequence <= last_sequence:
                        continue
                    frames.append(runtime.latest_jpeg)
                    last_sequence = runtime.frame_sequence
                return frames
            finally:
                runtime.preview_clients = max(0, runtime.preview_clients - 1)
                self._frame_ready.notify_all()

    def start(self) -> None:
        self._stop_event.clear()
        for runtime in self._cameras.values():
            if runtime.thread and runtime.thread.is_alive():
                continue
            runtime.thread = threading.Thread(
                target=self._run_camera,
                args=(runtime,),
                name=f"native-camera-{runtime.config.id}",
                daemon=True,
            )
            runtime.thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        for runtime in self._cameras.values():
            if runtime.thread:
                runtime.thread.join(timeout=4)
        for runtime in self._cameras.values():
            self._release_camera(runtime)

    def status(self) -> dict:
        with self._lock:
            cameras = [
                {
                    "id": runtime.config.id,
                    "name": runtime.config.name,
                    "index": runtime.config.index,
                    "primary": runtime.config.primary,
                    "active": runtime.active,
                    "state": runtime.state,
                    "error": runtime.error,
                    "fps": round(runtime.fps, 1),
                    "motion_score": round(runtime.motion_score, 2),
                    "motion": runtime.motion_rule.to_dict(),
                    "last_frame_at": runtime.last_frame_at,
                    "preview_clients": runtime.preview_clients,
                }
                for runtime in self._cameras.values()
            ]
            active_count = sum(1 for camera in cameras if camera["active"])
            monitoring_count = sum(1 for camera in cameras if camera["motion"]["enabled"])
            active_monitoring_count = sum(
                1 for camera in cameras
                if self._enabled and camera["active"] and camera["motion"]["enabled"]
            )
            errors = [camera["error"] for camera in cameras if camera["error"]]
            if not self._enabled:
                state = "无人值守监控已关闭"
            elif active_count:
                state = f"后台正在采集 {active_count}/{len(cameras)} 个摄像头"
            elif cameras:
                state = cameras[0]["state"]
            else:
                state = "没有启用后台摄像头"
            return {
                "enabled": self._enabled,
                "active": active_count > 0,
                "active_count": active_count,
                "monitoring_count": monitoring_count,
                "active_monitoring_count": active_monitoring_count,
                "configured_count": len(cameras),
                "state": state,
                "error": "；".join(dict.fromkeys(errors)),
                "camera_index": self.config.native_camera_index,
                "fps": round(sum(camera["fps"] for camera in cameras), 1),
                "motion_score": round(
                    max((camera["motion_score"] for camera in cameras), default=0.0),
                    2,
                ),
                "last_frame_at": max(
                    (camera["last_frame_at"] for camera in cameras), default=0.0
                ),
                "cameras": cameras,
                "events": self.archive.events(),
            }

    def _open_camera(self, camera: NativeCameraConfig) -> cv2.VideoCapture:
        mjpg = cv2.VideoWriter_fourcc(*"MJPG")
        format_params = [
            cv2.CAP_PROP_FOURCC,
            mjpg,
            cv2.CAP_PROP_FRAME_WIDTH,
            self.config.native_camera_width,
            cv2.CAP_PROP_FRAME_HEIGHT,
            self.config.native_camera_height,
        ]
        # 打开阶段直接协商 MJPG 压缩格式：两路默认的未压缩画面会超出同一 USB
        # 控制器的带宽，导致同型号双摄中的第二路打不开。
        capture = cv2.VideoCapture(camera.index, cv2.CAP_DSHOW, format_params)
        if not capture.isOpened():
            capture.release()
            capture = cv2.VideoCapture(camera.index, cv2.CAP_MSMF, format_params)
        if not capture.isOpened():
            # 个别设备不接受打开时指定格式，退回默认协商流程。
            capture.release()
            capture = cv2.VideoCapture(camera.index, cv2.CAP_DSHOW)
        if not capture.isOpened():
            capture.release()
            capture = cv2.VideoCapture(camera.index, cv2.CAP_MSMF)
        if capture.isOpened():
            capture.set(cv2.CAP_PROP_FOURCC, mjpg)
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.native_camera_width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.native_camera_height)
            capture.set(cv2.CAP_PROP_FPS, self.config.native_camera_fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture

    def _release_camera(self, runtime: _CameraRuntime) -> None:
        capture = runtime.capture
        runtime.capture = None
        if capture is not None:
            capture.release()
        runtime.detector.reset()
        with self._frame_ready:
            runtime.active = False
            runtime.fps = 0.0
            runtime.motion_score = 0.0
            runtime.black_frames = 0
            runtime.latest_jpeg = None
            runtime.last_frame_at = 0.0
            self._frame_ready.notify_all()

    def _dispatch_event(self, event: dict) -> None:
        for callback in tuple(self._listeners):
            try:
                callback(event)
            except Exception:
                continue

    def _process_frame(
        self, runtime: _CameraRuntime, frame: np.ndarray, now: float
    ) -> bool:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean, deviation = cv2.meanStdDev(gray)
        if float(mean[0][0]) < 18.0 and float(deviation[0][0]) < 2.0:
            with self._lock:
                runtime.black_frames += 1
                if runtime.black_frames >= 3:
                    runtime.state = "摄像头返回黑屏，正在重连"
                    runtime.error = f"{runtime.config.name} 持续返回黑帧"
            return False
        with self._lock:
            runtime.black_frames = 0
            if "黑帧" in runtime.error:
                runtime.error = ""
                runtime.state = (
                    "网页实时预览"
                    if runtime.preview_clients and not self._enabled
                    else "无人值守监控中"
                )
        encoded_ok, encoded = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 84]
        )
        if not encoded_ok:
            return False
        jpeg = encoded.tobytes()
        with self._frame_ready:
            runtime.last_frame_at = time.time()
            runtime.latest_jpeg = jpeg
            runtime.frame_sequence += 1
            self._frame_ready.notify_all()

        # Preview and explicit visual Q&A can still use the latest frame while
        # unattended monitoring is off; no background analysis may run then.
        if runtime.config.primary:
            self.store.update_frame(jpeg)
        if not self.enabled:
            return True

        if runtime.config.primary:
            presence_status = self.store.status()
            if (
                self.presence_monitor.enabled
                and not presence_status["presence_initialized"]
                and not presence_status["presence_checking"]
            ):
                self.presence_monitor.submit(jpeg, "baseline")

            if self.face_service.enabled and now - runtime.last_face_submit_at >= 1.2:
                if self.face_service.submit(jpeg):
                    runtime.last_face_submit_at = now

            gesture_interval = self.config.gesture_submit_interval_seconds
            if (
                self.gesture_service is not None
                and self.gesture_service.enabled
                and now - runtime.last_gesture_submit_at >= gesture_interval
            ):
                if self.gesture_service.submit(jpeg):
                    runtime.last_gesture_submit_at = now

        with self._lock:
            rule = runtime.motion_rule
            if not rule.enabled:
                runtime.motion_score = 0.0
                return True
            settings = MotionSettings(
                rule.sensitivity, rule.min_area_percent,
                rule.consecutive_frames, rule.cooldown_seconds,
                rule.confirmation_seconds,
            )
            score, triggered = runtime.detector.process(
                frame, settings, now, runtime.last_alert_at, rule.roi
            )
            runtime.motion_score = score
            if triggered:
                runtime.last_alert_at = now
        if not triggered:
            return True
        event = self.archive.record(
            jpeg,
            score,
            runtime.config.id,
            runtime.config.name,
        )
        if runtime.config.primary:
            if self.presence_monitor.enabled:
                self.presence_monitor.submit(jpeg, "motion")
            if self.store.scene_broadcast_enabled():
                self.store.submit_scene_broadcast(
                    jpeg, self.config.scene_broadcast_cooldown_seconds
                )
        self._dispatch_event(event)
        return True

    def _run_camera(self, runtime: _CameraRuntime) -> None:
        frame_count = 0
        fps_started_at = time.monotonic()
        failures = 0
        while not self._stop_event.is_set():
            with self._lock:
                preview_active = runtime.preview_clients > 0
            if not self.enabled and not preview_active:
                self._release_camera(runtime)
                self._stop_event.wait(0.5)
                continue

            if runtime.capture is None or not runtime.capture.isOpened():
                runtime.capture = self._open_camera(runtime.config)
                if not runtime.capture.isOpened():
                    self._release_camera(runtime)
                    with self._lock:
                        runtime.state = "等待摄像头释放"
                        runtime.error = (
                            f"{runtime.config.name}（索引 {runtime.config.index}）暂时无法打开"
                        )
                    self._stop_event.wait(2.0)
                    continue
                runtime.detector.reset()
                with self._lock:
                    runtime.active = True
                    runtime.state = (
                        "网页实时预览"
                        if preview_active and not self.enabled
                        else "无人值守监控中"
                    )
                    runtime.error = ""

            iteration_started_at = time.monotonic()
            ok, frame = runtime.capture.read()
            if not ok or frame is None:
                failures += 1
                if failures >= 8:
                    self._release_camera(runtime)
                    with self._lock:
                        runtime.state = "摄像头读取失败，正在重连"
                        runtime.error = f"{runtime.config.name} 连续读取失败"
                    failures = 0
                self._stop_event.wait(0.05)
                continue

            failures = 0
            frame_count += 1
            now = time.monotonic()
            elapsed = now - fps_started_at
            if elapsed >= 1.0:
                with self._lock:
                    runtime.fps = frame_count / elapsed
                frame_count = 0
                fps_started_at = now
            valid_frame = self._process_frame(runtime, frame, now)
            if (
                not valid_frame
                and runtime.black_frames
                >= max(10, self.config.native_camera_fps * 2)
            ):
                self._release_camera(runtime)
                self._stop_event.wait(0.8)
                continue
            frame_interval = 1.0 / self.config.native_camera_fps
            remaining = frame_interval - (time.monotonic() - iteration_started_at)
            if remaining > 0:
                self._stop_event.wait(remaining)
