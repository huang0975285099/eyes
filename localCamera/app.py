from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import urlopen

import cv2
import numpy as np


APP_NAME = "USB Camera Motion Alert"


def resource_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def writable_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        candidates = [
            Path(sys.executable).resolve().parent / "data",
            Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LocalCamera",
        ]
    else:
        candidates = [Path(__file__).resolve().parent / "data"]
    errors: list[str] = []
    for root in candidates:
        try:
            root.mkdir(parents=True, exist_ok=True)
            return root
        except OSError as exc:
            errors.append(f"{root}: {exc}")
    raise RuntimeError("Unable to create a data directory: " + "; ".join(errors))


@dataclass
class MonitorConfig:
    camera_index: int = 0
    sensitivity: int = 60
    min_area_percent: float = 0.8
    consecutive_frames: int = 3
    cooldown_seconds: float = 5.0
    save_snapshots: bool = True

    def update(self, values: dict[str, Any]) -> None:
        if "camera_index" in values:
            self.camera_index = max(0, min(9, int(values["camera_index"])))
        if "sensitivity" in values:
            self.sensitivity = max(1, min(100, int(values["sensitivity"])))
        if "min_area_percent" in values:
            self.min_area_percent = max(0.05, min(50.0, float(values["min_area_percent"])))
        if "consecutive_frames" in values:
            self.consecutive_frames = max(1, min(30, int(values["consecutive_frames"])))
        if "cooldown_seconds" in values:
            self.cooldown_seconds = max(0.5, min(3600.0, float(values["cooldown_seconds"])))
        if "save_snapshots" in values:
            self.save_snapshots = bool(values["save_snapshots"])


class MotionDetector:
    """Background-model motion detector with debouncing and a warm-up period."""

    def __init__(self) -> None:
        self.background: np.ndarray | None = None
        self.background_started_at = 0.0
        self.changed_frames = 0

    def reset(self) -> None:
        self.background = None
        self.background_started_at = 0.0
        self.changed_frames = 0

    def process(
        self, frame: np.ndarray, config: MonitorConfig, now: float, last_alert_at: float
    ) -> tuple[float, bool, list[tuple[int, int, int, int]]]:
        height, width = frame.shape[:2]
        target_width = 640
        scale = min(1.0, target_width / float(width))
        small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1.0 else frame.copy()
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.background is None:
            self.background = gray.astype("float")
            self.background_started_at = now
            return 0.0, False, []

        background_u8 = cv2.convertScaleAbs(self.background)
        delta = cv2.absdiff(gray, background_u8)

        # Higher sensitivity means a lower pixel-difference threshold.
        pixel_threshold = int(round(42 - (config.sensitivity - 1) * 30 / 99))
        mask = cv2.threshold(delta, pixel_threshold, 255, cv2.THRESH_BINARY)[1]
        mask = cv2.dilate(mask, None, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        total_area = float(small.shape[0] * small.shape[1])
        min_contour_area = max(60.0, total_area * config.min_area_percent / 100.0)
        boxes: list[tuple[int, int, int, int]] = []
        changed_area = 0.0
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_contour_area:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            boxes.append((x, y, w, h))
            changed_area += area

        motion_score = min(100.0, changed_area / total_area * 100.0)
        significant = bool(boxes)
        self.changed_frames = self.changed_frames + 1 if significant else 0

        cooling_down = now - last_alert_at < config.cooldown_seconds
        triggered = (
            now - self.background_started_at >= 1.0
            and self.changed_frames >= config.consecutive_frames
            and not cooling_down
        )
        if triggered:
            self.changed_frames = 0

        # Learn slowly during monitoring, quickly when the scene is stable.
        learning_rate = 0.002 if significant else 0.025
        cv2.accumulateWeighted(gray, self.background, learning_rate)

        if scale < 1.0:
            inv = 1.0 / scale
            boxes = [tuple(int(v * inv) for v in box) for box in boxes]
        return motion_score, triggered, boxes


class CameraEngine:
    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.data_dir = writable_data_dir()
        self.events_dir = self.data_dir / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)

        self.lock = threading.RLock()
        self.frame_ready = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.detector = MotionDetector()
        self.thread: threading.Thread | None = None
        self.capture: cv2.VideoCapture | None = None
        self.jpeg: bytes | None = None
        self.frame_sequence = 0
        self.monitoring = False
        self.camera_ok = False
        self.error = "Connecting to the camera..."
        self.fps = 0.0
        self.motion_score = 0.0
        self.last_alert_at = 0.0
        self.alert_count = 0
        self.latest_event: dict[str, Any] | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=30)
        self.started_at = time.time()
        self.requested_camera_index = config.camera_index
        self.active_camera_index: int | None = None
        self.native_notifications = False
        self.notification_callback: Callable[[dict[str, Any]], None] | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.frame_ready:
            self.frame_ready.notify_all()
        if self.thread:
            self.thread.join(timeout=3)
        if self.capture:
            self.capture.release()

    def set_monitoring(self, enabled: bool) -> None:
        with self.lock:
            self.monitoring = enabled
            self.motion_score = 0.0
            self.detector.reset()

    def update_config(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            old_index = self.config.camera_index
            self.config.update(values)
            if old_index != self.config.camera_index:
                self.requested_camera_index = self.config.camera_index
                self.camera_ok = False
                self.error = f"Switching to camera {self.config.camera_index}..."
            return asdict(self.config)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "app_name": APP_NAME,
                "camera_ok": self.camera_ok,
                "camera_index": self.config.camera_index,
                "monitoring": self.monitoring,
                "error": self.error,
                "fps": round(self.fps, 1),
                "motion_score": round(self.motion_score, 2),
                "last_alert_at": self.last_alert_at,
                "alert_count": self.alert_count,
                "native_notifications": self.native_notifications,
                "latest_event": self.latest_event,
                "events": list(self.events),
                "config": asdict(self.config),
                "uptime_seconds": int(time.time() - self.started_at),
            }

    def test_notification(self) -> bool:
        callback = self.notification_callback
        if callback is None:
            return False
        now = datetime.now()
        event = {
            "test": True,
            "display_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "motion_score": 0.0,
        }
        threading.Thread(target=callback, args=(event,), name="test-notification", daemon=True).start()
        return True

    def wait_for_frame(self, previous_sequence: int, timeout: float = 2.0) -> tuple[int, bytes | None]:
        with self.frame_ready:
            self.frame_ready.wait_for(
                lambda: self.frame_sequence != previous_sequence or self.stop_event.is_set(),
                timeout=timeout,
            )
            return self.frame_sequence, self.jpeg

    def _open_camera(self, index: int) -> cv2.VideoCapture:
        if os.name == "nt":
            capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not capture.isOpened():
                capture.release()
                capture = cv2.VideoCapture(index, cv2.CAP_MSMF)
        else:
            capture = cv2.VideoCapture(index)
        if capture.isOpened():
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            capture.set(cv2.CAP_PROP_FPS, 30)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture

    def _switch_camera_if_needed(self) -> None:
        with self.lock:
            requested = self.requested_camera_index
        if (
            self.capture is not None
            and requested == self.active_camera_index
            and self.capture.isOpened()
        ):
            return
        if self.capture is not None:
            self.capture.release()
        self.capture = self._open_camera(requested)
        with self.lock:
            self.config.camera_index = requested
            self.active_camera_index = requested
            self.detector.reset()
            self.camera_ok = self.capture.isOpened()
            self.error = (
                ""
                if self.camera_ok
                else f"Unable to open camera {requested}. Check the connection or try another index."
            )

    def _record_event(self, frame: np.ndarray, score: float) -> None:
        timestamp = datetime.now()
        event_id = timestamp.strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{event_id}.jpg"
        snapshot_url: str | None = None
        if self.config.save_snapshots:
            path = self.events_dir / filename
            cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            snapshot_url = f"/events/{filename}"
        event = {
            "id": event_id,
            "timestamp": timestamp.isoformat(timespec="seconds"),
            "display_time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "motion_score": round(score, 2),
            "snapshot_url": snapshot_url,
        }
        self.latest_event = event
        self.events.appendleft(event)
        self.alert_count += 1
        self.last_alert_at = time.time()
        callback = self.notification_callback
        if callback is not None:
            threading.Thread(target=callback, args=(event,), name="alert-notification", daemon=True).start()

    def _run(self) -> None:
        failures = 0
        frames = 0
        fps_started = time.monotonic()
        while not self.stop_event.is_set():
            self._switch_camera_if_needed()
            if self.capture is None or not self.capture.isOpened():
                time.sleep(1.0)
                # Retry the same camera periodically.
                if self.capture:
                    self.capture.release()
                self.capture = None
                continue

            ok, frame = self.capture.read()
            if not ok or frame is None:
                failures += 1
                if failures >= 10:
                    with self.lock:
                        self.camera_ok = False
                        self.error = "Camera read failed. Reconnecting..."
                    self.capture.release()
                    self.capture = None
                    failures = 0
                time.sleep(0.05)
                continue

            failures = 0
            frames += 1
            elapsed = time.monotonic() - fps_started
            if elapsed >= 1.0:
                with self.lock:
                    self.fps = frames / elapsed
                frames = 0
                fps_started = time.monotonic()

            with self.lock:
                self.camera_ok = True
                self.error = ""
                monitoring = self.monitoring
                config = MonitorConfig(**asdict(self.config))
                last_alert_at = self.last_alert_at

            boxes: list[tuple[int, int, int, int]] = []
            score = 0.0
            triggered = False
            if monitoring:
                score, triggered, boxes = self.detector.process(frame, config, time.time(), last_alert_at)
            else:
                self.detector.reset()

            display = frame.copy()
            for x, y, w, h in boxes:
                cv2.rectangle(display, (x, y), (x + w, y + h), (65, 62, 244), 3)

            with self.lock:
                self.motion_score = score
                if triggered:
                    self._record_event(frame, score)
                encode_ok, encoded = cv2.imencode(
                    ".jpg", display, [int(cv2.IMWRITE_JPEG_QUALITY), 82]
                )
                if encode_ok:
                    self.jpeg = encoded.tobytes()
                    self.frame_sequence += 1
                    self.frame_ready.notify_all()


class CameraHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], engine: CameraEngine):
        super().__init__(address, RequestHandler)
        self.engine = engine
        self.static_dir = resource_dir() / "static"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class RequestHandler(BaseHTTPRequestHandler):
    server: CameraHttpServer

    def log_message(self, format: str, *args: Any) -> None:
        if self.path != "/api/status" and sys.stderr is not None:
            super().log_message(format, *args)

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(self.server.engine.status())
            return
        if path == "/stream.mjpg":
            self._stream()
            return
        if path.startswith("/events/"):
            self._serve_event(path.removeprefix("/events/"))
            return
        if path == "/":
            path = "/index.html"
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/monitor":
                self.server.engine.set_monitoring(bool(body.get("enabled", False)))
                self._json(self.server.engine.status())
            elif path == "/api/test-notification":
                delivered = self.server.engine.test_notification()
                self._json({"ok": True, "native": delivered})
            elif path == "/api/config":
                config = self.server.engine.update_config(body)
                self._json({"ok": True, "config": config})
            else:
                self._json({"error": "Endpoint not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _serve_static(self, path: str) -> None:
        allowed = {
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        item = allowed.get(path)
        if not item:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        filename, content_type = item
        file_path = self.server.static_dir / filename
        try:
            payload = file_path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_event(self, filename: str) -> None:
        if Path(filename).name != filename or not filename.lower().endswith(".jpg"):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        path = self.server.engine.events_dir / filename
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=31536000")
        self.end_headers()
        self.wfile.write(payload)

    def _stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sequence = -1
        try:
            while not self.server.engine.stop_event.is_set():
                sequence, jpeg = self.server.engine.wait_for_frame(sequence)
                if jpeg is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii"))
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class PopupNotifier:
    """A reliable bottom-right alert window independent of browser permissions."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.available = False
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="popup-notifier", daemon=True)
        self.thread.start()
        self.ready.wait(timeout=2.0)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1.5)

    def show(self, event: dict[str, Any]) -> bool:
        if not self.available:
            return False
        self.events.put(event)
        return True

    def _run(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            self.available = True
            self.ready.set()
            popup: tk.Toplevel | None = None

            def show_next() -> None:
                nonlocal popup
                if self.stop_event.is_set():
                    root.destroy()
                    return
                latest: dict[str, Any] | None = None
                try:
                    while True:
                        latest = self.events.get_nowait()
                except queue.Empty:
                    pass
                if latest is not None:
                    if popup is not None and popup.winfo_exists():
                        popup.destroy()
                    popup = self._create_popup(tk, root, latest)
                root.after(100, show_next)

            root.after(0, show_next)
            root.mainloop()
        except Exception:
            self.available = False
            self.ready.set()

    def _create_popup(self, tk: Any, root: Any, event: dict[str, Any]) -> Any:
        is_test = bool(event.get("test"))
        title = "Sentinel Test" if is_test else "Visual Change Detected"
        if is_test:
            message = "Background alerts are working."
            accent = "#b9ef49"
        else:
            message = (
                f"Detected at {event['display_time']}\n"
                f"Changed area: {float(event['motion_score']):.2f}%"
            )
            accent = "#ff5b55"

        window = tk.Toplevel(root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg=accent)
        width, height = 390, 164
        x = max(12, window.winfo_screenwidth() - width - 18)
        y = max(12, window.winfo_screenheight() - height - 58)
        window.geometry(f"{width}x{height}+{x}+{y}")

        panel = tk.Frame(window, bg="#101512", padx=18, pady=14)
        panel.pack(fill="both", expand=True, padx=2, pady=2)
        tk.Label(
            panel,
            text=title,
            bg="#101512",
            fg=accent,
            font=("Segoe UI", 13, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            panel,
            text=message,
            bg="#101512",
            fg="#e8efea",
            font=("Segoe UI", 10),
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(8, 10))

        actions = tk.Frame(panel, bg="#101512")
        actions.pack(fill="x")

        def open_dashboard() -> None:
            webbrowser.open(self.url)
            window.destroy()

        tk.Button(
            actions,
            text="Open Dashboard",
            command=open_dashboard,
            bg="#b9ef49",
            fg="#101512",
            activebackground="#d0ff6b",
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        tk.Button(
            actions,
            text="Dismiss",
            command=window.destroy,
            bg="#27312c",
            fg="#e8efea",
            activebackground="#35423b",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
            font=("Segoe UI", 9),
        ).pack(side="right")
        window.after(12000, lambda: window.destroy() if window.winfo_exists() else None)
        return window


class TrayApplication:
    def __init__(self, engine: CameraEngine, server: CameraHttpServer, url: str) -> None:
        import pystray
        from PIL import Image, ImageDraw

        self.engine = engine
        self.server = server
        self.url = url
        self.pystray = pystray
        self.popup_notifier = PopupNotifier(url)

        image = Image.new("RGBA", (64, 64), (13, 20, 16, 255))
        draw = ImageDraw.Draw(image)
        green = (185, 239, 73, 255)
        draw.rounded_rectangle((2, 2, 61, 61), radius=16, outline=green, width=4)
        draw.ellipse((13, 21, 51, 43), outline=green, width=4)
        draw.ellipse((26, 25, 38, 39), fill=green)

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", self.open_dashboard, default=True),
            pystray.MenuItem("Start / Stop Detection", self.toggle_detection),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit Sentinel", self.exit_application),
        )
        self.icon = pystray.Icon("Sentinel", image, "Sentinel - USB Camera Motion Alert", menu)
        self.engine.notification_callback = self.show_notification
        self.engine.native_notifications = True

    def start(self) -> None:
        self.popup_notifier.start()
        self.icon.run_detached()

    def stop(self) -> None:
        self.engine.notification_callback = None
        self.engine.native_notifications = False
        self.popup_notifier.stop()
        self.icon.stop()

    def open_dashboard(self, *_: Any) -> None:
        webbrowser.open(self.url)

    def toggle_detection(self, *_: Any) -> None:
        enabled = not self.engine.status()["monitoring"]
        self.engine.set_monitoring(enabled)
        message = "Change detection started." if enabled else "Change detection stopped."
        self.icon.notify(message, "Sentinel")

    def show_notification(self, event: dict[str, Any]) -> None:
        try:
            if not self.popup_notifier.show(event):
                title = "Sentinel Test" if event.get("test") else "Visual Change Detected"
                self.icon.notify("Background alert received.", title)
        except Exception:
            # Detection and snapshot storage must continue if Windows rejects a notification.
            pass

    def exit_application(self, *_: Any) -> None:
        self.icon.stop()
        threading.Thread(target=self.server.shutdown, name="server-shutdown", daemon=True).start()


def show_error(message: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def existing_sentinel_running(url: str) -> bool:
    try:
        with urlopen(f"{url}/api/status", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("app_name") == APP_NAME
    except Exception:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--host", default="127.0.0.1", help="Listen address; local access only by default")
    parser.add_argument("--port", type=int, default=8765, help="Web interface port")
    parser.add_argument("--camera", type=int, default=0, help="Camera index")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    parser.add_argument("--no-tray", action="store_true", help="Disable the Windows tray icon")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    url = f"http://127.0.0.1:{args.port}"
    config = MonitorConfig(camera_index=max(0, args.camera))
    try:
        engine = CameraEngine(config)
        server = CameraHttpServer((args.host, args.port), engine)
    except OSError as exc:
        if existing_sentinel_running(url):
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        show_error(f"Unable to start the server:\n\n{exc}")
        return 1
    except RuntimeError as exc:
        show_error(str(exc))
        return 1

    engine.start()
    print(f"{APP_NAME} started: {url}")
    print("Press Ctrl+C to exit.")

    tray: TrayApplication | None = None
    if os.name == "nt" and not args.no_tray:
        try:
            tray = TrayApplication(engine, server, url)
            tray.start()
        except Exception as exc:
            engine.stop()
            server.server_close()
            show_error(f"Unable to start the system tray icon:\n\n{exc}")
            return 1

    shutting_down = threading.Event()

    def stop_handler(*_: Any) -> None:
        if not shutting_down.is_set():
            shutting_down.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        if tray is not None:
            tray.stop()
        server.server_close()
        engine.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
