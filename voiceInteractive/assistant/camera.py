"""摄像头帧存储、YOLO 人物检测与在场监测。"""

from __future__ import annotations

import base64
import json
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime

from .config import Config
from .paths import APP_DIR


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
