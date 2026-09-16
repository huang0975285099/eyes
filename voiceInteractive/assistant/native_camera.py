"""浏览器关闭后接管 USB 摄像头的本机后台监控。"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import cv2
import numpy as np

from .config import Config
from .paths import APP_DIR

if TYPE_CHECKING:
    from .camera import CameraFrameStore, PersonPresenceMonitor
    from .face import FaceRecognitionService


@dataclass(frozen=True)
class MotionSettings:
    sensitivity: int
    min_area_percent: float
    consecutive_frames: int
    cooldown_seconds: float


class MotionDetector:
    """背景差分检测器；连续多帧变化后才触发，降低光照噪声误报。"""

    def __init__(self) -> None:
        self.background: np.ndarray | None = None
        self.background_started_at = 0.0
        self.changed_frames = 0

    def reset(self) -> None:
        self.background = None
        self.background_started_at = 0.0
        self.changed_frames = 0

    def process(
        self,
        frame: np.ndarray,
        settings: MotionSettings,
        now: float,
        last_alert_at: float,
    ) -> tuple[float, bool]:
        height, width = frame.shape[:2]
        if height <= 0 or width <= 0:
            return 0.0, False
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
        minimum_area = max(60.0, total_area * settings.min_area_percent / 100.0)
        changed_area = sum(
            cv2.contourArea(contour)
            for contour in contours
            if cv2.contourArea(contour) >= minimum_area
        )
        significant = changed_area > 0
        self.changed_frames = self.changed_frames + 1 if significant else 0
        triggered = (
            now - self.background_started_at >= 1.0
            and self.changed_frames >= settings.consecutive_frames
            and now - last_alert_at >= settings.cooldown_seconds
        )
        if triggered:
            self.changed_frames = 0
            # 变化后的画面成为新基线，便于之后检测人员离开或再次进入。
            self.background = gray.astype("float")
            self.background_started_at = now
        else:
            cv2.accumulateWeighted(
                gray, self.background, 0.002 if significant else 0.025
            )
        return min(100.0, changed_area / total_area * 100.0), triggered


class MotionEventArchive:
    """保存后台变化快照，并按保留天数自动清理。"""

    def __init__(self, retention_days: int, enabled: bool) -> None:
        self.directory = APP_DIR / "data" / "events"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.enabled = enabled
        self._lock = threading.Lock()
        self._events: deque[dict] = deque(maxlen=30)
        self.cleanup()
        for path in sorted(
            self.directory.glob("motion_*.jpg"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:30]:
            timestamp = datetime.fromtimestamp(path.stat().st_mtime)
            self._events.append(
                {
                    "id": path.stem.removeprefix("motion_"),
                    "display_time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "snapshot_url": f"/api/motion-events/{path.name}",
                }
            )

    def cleanup(self) -> int:
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        removed = 0
        for path in self.directory.glob("motion_*.jpg"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def record(self, jpeg: bytes, score: float) -> dict:
        now = datetime.now()
        event_id = now.strftime("%Y%m%d_%H%M%S_%f")
        event = {
            "id": event_id,
            "display_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "motion_score": round(score, 2),
            "snapshot_url": None,
        }
        if self.enabled:
            filename = f"motion_{event_id}.jpg"
            try:
                (self.directory / filename).write_bytes(jpeg)
                event["snapshot_url"] = f"/api/motion-events/{filename}"
            except OSError:
                # 磁盘只读或空间不足时，监控和语音提醒仍应继续工作。
                pass
        with self._lock:
            self._events.appendleft(event)
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


class NativeCameraMonitor:
    """网页画面中断后自动接管摄像头，网页恢复时主动释放设备。"""

    def __init__(
        self,
        config: Config,
        store: CameraFrameStore,
        presence_monitor: PersonPresenceMonitor,
        face_service: FaceRecognitionService,
    ) -> None:
        self.config = config
        self.store = store
        self.presence_monitor = presence_monitor
        self.face_service = face_service
        self.archive = MotionEventArchive(
            config.native_camera_event_retention_days,
            config.native_camera_save_snapshots,
        )
        self.settings = MotionSettings(
            config.person_motion_sensitivity,
            config.person_motion_min_area_percent,
            config.person_motion_consecutive_frames,
            config.person_motion_cooldown_seconds,
        )
        self._enabled = config.native_camera_enabled
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: cv2.VideoCapture | None = None
        self._detector = MotionDetector()
        self._state = "等待网页摄像头"
        self._error = ""
        self._camera_active = False
        self._fps = 0.0
        self._motion_score = 0.0
        self._last_alert_at = 0.0
        self._last_frame_at = 0.0
        self._last_face_submit_at = 0.0
        self._browser_claim_until = 0.0
        self._listeners: list[Callable[[dict], None]] = []

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def add_event_listener(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            self._state = "等待网页摄像头" if enabled else "后台摄像头接管已关闭"
            self._error = ""

    def claim_for_browser(self, seconds: float = 12.0) -> None:
        """Temporarily release the device so getUserMedia can acquire it."""
        with self._lock:
            self._browser_claim_until = time.monotonic() + max(2.0, seconds)
            self._state = "正在把摄像头交给网页"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="native-camera-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=4)
        self._release_camera()

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "active": self._camera_active,
                "state": self._state,
                "error": self._error,
                "camera_index": self.config.native_camera_index,
                "fps": round(self._fps, 1),
                "motion_score": round(self._motion_score, 2),
                "last_frame_at": self._last_frame_at,
                "browser_claimed": time.monotonic() < self._browser_claim_until,
                "events": self.archive.events(),
            }

    def _open_camera(self) -> cv2.VideoCapture:
        index = self.config.native_camera_index
        capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not capture.isOpened():
            capture.release()
            capture = cv2.VideoCapture(index, cv2.CAP_MSMF)
        if capture.isOpened():
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.native_camera_width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.native_camera_height)
            capture.set(cv2.CAP_PROP_FPS, self.config.native_camera_fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture

    def _release_camera(self) -> None:
        capture = self._capture
        self._capture = None
        if capture is not None:
            capture.release()
        self._detector.reset()
        with self._lock:
            self._camera_active = False
            self._fps = 0.0
            self._motion_score = 0.0

    def _dispatch_event(self, event: dict) -> None:
        for callback in tuple(self._listeners):
            try:
                callback(event)
            except Exception:
                continue

    def _process_frame(self, frame: np.ndarray, now: float) -> None:
        encoded_ok, encoded = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 84]
        )
        if not encoded_ok:
            return
        jpeg = encoded.tobytes()
        self.store.update_frame(jpeg, source="native")
        self._last_frame_at = time.time()

        presence_status = self.store.status()
        if (
            self.presence_monitor.enabled
            and not presence_status["presence_initialized"]
            and not presence_status["presence_checking"]
        ):
            self.presence_monitor.submit(jpeg, "baseline")

        if self.face_service.enabled and now - self._last_face_submit_at >= 1.2:
            if self.face_service.submit(jpeg):
                self._last_face_submit_at = now

        score, triggered = self._detector.process(
            frame, self.settings, time.time(), self._last_alert_at
        )
        with self._lock:
            self._motion_score = score
        if not triggered:
            return
        self._last_alert_at = time.time()
        event = self.archive.record(jpeg, score)
        if self.presence_monitor.enabled:
            self.presence_monitor.submit(jpeg, "motion")
        if self.store.scene_broadcast_enabled():
            self.store.submit_scene_broadcast(
                jpeg, self.config.scene_broadcast_cooldown_seconds
            )
        self._dispatch_event(event)

    def _run(self) -> None:
        frame_count = 0
        fps_started_at = time.monotonic()
        failures = 0
        while not self._stop_event.is_set():
            if not self.enabled:
                self._release_camera()
                self._stop_event.wait(0.5)
                continue

            if time.monotonic() < self._browser_claim_until:
                if self._capture is not None:
                    self._release_camera()
                with self._lock:
                    self._state = "等待网页连接摄像头"
                    self._error = ""
                self._stop_event.wait(0.25)
                continue

            browser_age = self.store.browser_frame_age_seconds()
            if (
                browser_age is not None
                and browser_age < self.config.native_camera_fallback_seconds
            ):
                if self._capture is not None:
                    self._release_camera()
                with self._lock:
                    self._state = "网页摄像头正在工作，后台待机"
                    self._error = ""
                self._stop_event.wait(0.5)
                continue

            if self._capture is None or not self._capture.isOpened():
                self._capture = self._open_camera()
                if not self._capture.isOpened():
                    self._release_camera()
                    with self._lock:
                        self._state = "等待摄像头释放"
                        self._error = "后台暂时无法打开摄像头，将自动重试"
                    self._stop_event.wait(2.0)
                    continue
                self._detector.reset()
                with self._lock:
                    self._camera_active = True
                    self._state = "后台摄像头已接管"
                    self._error = ""

            ok, frame = self._capture.read()
            if not ok or frame is None:
                failures += 1
                if failures >= 8:
                    self._release_camera()
                    with self._lock:
                        self._state = "摄像头读取失败，正在重连"
                        self._error = "连续读取失败"
                    failures = 0
                self._stop_event.wait(0.05)
                continue

            failures = 0
            frame_count += 1
            now = time.monotonic()
            elapsed = now - fps_started_at
            if elapsed >= 1.0:
                with self._lock:
                    self._fps = frame_count / elapsed
                frame_count = 0
                fps_started_at = now
            self._process_frame(frame, now)

            target_delay = 1.0 / self.config.native_camera_fps
            self._stop_event.wait(max(0.0, target_delay))
