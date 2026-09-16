"""Web Dashboard HTTP 服务。"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .camera import CameraFrameStore, PersonPresenceMonitor, YoloPersonDetector
from .config import Config
from .face import FaceRecognitionService
from .llm import ModelRouter
from .native_camera import NativeCameraMonitor
from .paths import APP_DIR
from .platform_utils import audio_device_options, find_audio_device


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        handler,
        store: CameraFrameStore,
        config: Config,
        model_router: ModelRouter,
        presence_monitor: PersonPresenceMonitor,
        face_service: FaceRecognitionService,
        native_camera: NativeCameraMonitor,
    ):
        super().__init__(address, handler)
        self.store = store
        self.config = config
        self.model_router = model_router
        self.presence_monitor = presence_monitor
        self.face_service = face_service
        self.native_camera = native_camera


class DashboardHTTPServerV6(DashboardHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer
    MAX_FRAME_BYTES = 5 * 1024 * 1024
    MAX_VIDEO_BYTES = 80 * 1024 * 1024

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
            input_options = audio_device_options("input")
            output_options = audio_device_options("output")
            selected_input = find_audio_device(
                self.server.config.input_device, "input"
            )
            selected_output = find_audio_device(
                self.server.config.output_device, "output"
            )
            self._send_json(
                {
                    "camera_name_keywords": self.server.config.camera_name_keywords,
                    "model": self.server.model_router.info(),
                    "model_options": [
                        {
                            "provider": "online",
                            "label": self.server.config.online_model,
                        },
                        {
                            "provider": "ollama",
                            "label": self.server.config.ollama_model,
                        },
                    ],
                    "person_monitor": {
                        "enabled": self.server.presence_monitor.enabled,
                        "detector": self.server.config.person_detector,
                        "sensitivity": self.server.config.person_motion_sensitivity,
                        "min_area_percent": self.server.config.person_motion_min_area_percent,
                        "consecutive_frames": self.server.config.person_motion_consecutive_frames,
                        "cooldown_seconds": self.server.config.person_motion_cooldown_seconds,
                    },
                    "scene_broadcast": {
                        "enabled": self.server.store.scene_broadcast_enabled(),
                        "cooldown_seconds": self.server.config.scene_broadcast_cooldown_seconds,
                    },
                    "face_recognition": {
                        "enabled": self.server.face_service.enabled,
                        "recommended_samples": 15,
                    },
                    "native_camera": {
                        "enabled": self.server.native_camera.enabled,
                        "fallback_seconds": self.server.config.native_camera_fallback_seconds,
                        "retention_days": self.server.config.native_camera_event_retention_days,
                        "max_megabytes": self.server.config.native_camera_event_max_megabytes,
                        "cameras": [
                            {
                                "id": camera.id,
                                "name": camera.name,
                                "index": camera.index,
                                "enabled": camera.enabled,
                                "primary": camera.primary,
                            }
                            for camera in self.server.config.native_cameras
                        ],
                    },
                    "audio": {
                        "input_options": input_options,
                        "output_options": output_options,
                        "selected_input_index": selected_input.index,
                        "selected_output_index": selected_output.index,
                        "restart_required": True,
                    },
                }
            )
            return
        if request_path == "/api/status":
            status = self.server.store.status()
            status["face_recognition"] = self.server.face_service.status()
            status["native_camera"] = self.server.native_camera.status()
            self._send_json(status)
            return
        if request_path.startswith("/api/motion-events/"):
            filename = request_path.removeprefix("/api/motion-events/")
            path = self.server.native_camera.archive.resolve(filename)
            if path is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return
        if request_path == "/api/people":
            self._send_json({"people": self.server.face_service.database.list_people()})
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
        if request_path == "/api/presence-snapshot":
            event_id, frame = self.server.store.presence_snapshot()
            if frame is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Presence-Event-Id", str(event_id))
            self.end_headers()
            self.wfile.write(frame)
            return
        if request_path == "/api/scene-snapshot":
            event_id, frame = self.server.store.scene_broadcast_snapshot()
            if frame is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Scene-Event-Id", str(event_id))
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
        if request_path == "/api/model-provider":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 1024:
                    raise ValueError("无效的请求内容")
                payload = json.loads(
                    self.rfile.read(content_length).decode("utf-8")
                )
                model_info = self.server.model_router.switch(
                    str(payload.get("provider", ""))
                )
                self.server.store.set_assistant_status(
                    f"已切换为{model_info['label']}"
                )
                self._send_json({"ok": True, "model": model_info})
            except (ValueError, RuntimeError, json.JSONDecodeError) as error:
                self._send_json(
                    {"ok": False, "error": str(error)},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if request_path == "/api/audio-devices":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 4096:
                    raise ValueError("无效的请求内容")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                input_selector = payload.get("input_device")
                output_selector = payload.get("output_device")
                if isinstance(input_selector, bool) or not isinstance(
                    input_selector, (str, int)
                ):
                    raise ValueError("请选择有效的麦克风")
                if isinstance(output_selector, bool) or not isinstance(
                    output_selector, (str, int)
                ):
                    raise ValueError("请选择有效的扬声器")
                input_device = find_audio_device(input_selector, "input")
                output_device = find_audio_device(output_selector, "output")
                config_path = self.server.model_router.config_path
                raw = json.loads(config_path.read_text(encoding="utf-8"))
                raw["input_device"] = input_selector
                raw["output_device"] = output_selector
                temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
                temporary_path.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary_path, config_path)
                self._send_json(
                    {
                        "ok": True,
                        "restart_required": True,
                        "input": {
                            "index": input_device.index,
                            "name": input_device.name,
                        },
                        "output": {
                            "index": output_device.index,
                            "name": output_device.name,
                        },
                    }
                )
            except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
                self._send_json(
                    {"ok": False, "error": str(error)},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if request_path == "/api/person-monitor":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 1024:
                    raise ValueError("无效的请求内容")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必须是布尔值")
                self.server.presence_monitor.set_enabled(enabled)
                self._send_json({"ok": True, "enabled": enabled})
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json(
                    {"ok": False, "error": str(error)},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if request_path == "/api/native-camera":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 1024:
                    raise ValueError("无效的请求内容")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必须是布尔值")
                self.server.native_camera.set_enabled(enabled)
                self._send_json({"ok": True, "enabled": enabled})
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if request_path == "/api/camera-client":
            connected_count = None
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if 0 < content_length <= 1024:
                    payload = json.loads(
                        self.rfile.read(content_length).decode("utf-8")
                    )
                    if isinstance(payload.get("connected_count"), int):
                        connected_count = max(
                            0, min(20, payload["connected_count"])
                        )
            except (ValueError, json.JSONDecodeError):
                connected_count = None
            self.server.native_camera.claim_for_browser(
                connected_count=connected_count
            )
            self._send_json({"ok": True})
            return
        if request_path == "/api/face-recognition":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 1024:
                    raise ValueError("无效的请求内容")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必须是布尔值")
                self.server.face_service.set_enabled(enabled)
                self._send_json({"ok": True, "enabled": enabled})
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if request_path == "/api/people":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 4096:
                    raise ValueError("无效的请求内容")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                if payload.get("consent") is not True:
                    raise ValueError("录入前必须确认已获得本人同意")
                person = self.server.face_service.database.add_person(
                    str(payload.get("name", "")), payload.get("aliases", [])
                )
                self._send_json({"ok": True, "person": person}, HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if request_path == "/api/scene-broadcast":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 1024:
                    raise ValueError("无效的请求内容")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必须是布尔值")
                self.server.store.set_scene_broadcast_enabled(enabled)
                queued = False
                if enabled:
                    current_frame = self.server.store.latest_frame(
                        self.server.config.camera_frame_max_age_seconds
                    )
                    if current_frame is not None:
                        queued = self.server.store.submit_scene_broadcast(
                            current_frame,
                            self.server.config.scene_broadcast_cooldown_seconds,
                        )
                self._send_json(
                    {"ok": True, "enabled": enabled, "queued": queued}
                )
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json(
                    {"ok": False, "error": str(error)},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if request_path not in {
            "/api/frame",
            "/api/snapshot",
            "/api/presence-check",
            "/api/scene-description",
            "/api/face-recognize",
        }:
            sample_match = re.fullmatch(r"/api/people/([0-9a-f]{32})/samples", request_path)
            if sample_match:
                self._handle_face_sample(sample_match.group(1))
                return
            video_match = re.fullmatch(r"/api/people/([0-9a-f]{32})/video", request_path)
            if video_match:
                self._handle_face_video(video_match.group(1))
                return
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
        if request_path == "/api/presence-check":
            accepted = self.server.presence_monitor.submit(
                frame, self.headers.get("X-Presence-Reason", "motion")
            )
            self._send_json(
                {"accepted": accepted},
                HTTPStatus.ACCEPTED if accepted else HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        if request_path == "/api/scene-description":
            accepted = self.server.store.submit_scene_broadcast(
                frame, self.server.config.scene_broadcast_cooldown_seconds
            )
            self._send_json(
                {"accepted": accepted},
                HTTPStatus.ACCEPTED if accepted else HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        if request_path == "/api/face-recognize":
            accepted = self.server.face_service.submit(frame)
            self._send_json(
                {"accepted": accepted},
                HTTPStatus.ACCEPTED if accepted else HTTPStatus.TOO_MANY_REQUESTS,
            )
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

    def _handle_face_sample(self, person_id: str) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > self.MAX_FRAME_BYTES:
            self._send_json({"ok": False, "error": "无效的照片大小"}, HTTPStatus.BAD_REQUEST)
            return
        frame = self.rfile.read(content_length)
        if not (frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")):
            self._send_json({"ok": False, "error": "只支持JPEG照片"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            result = self.server.face_service.enroll(person_id, frame)
            self._send_json({"ok": True, **result}, HTTPStatus.CREATED)
        except (ValueError, RuntimeError) as error:
            self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _handle_face_video(self, person_id: str) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > self.MAX_VIDEO_BYTES:
            self._send_json(
                {"ok": False, "error": "视频大小无效或超过80MB"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        content_type = self.headers.get("Content-Type", "video/webm").split(";", 1)[0]
        if content_type not in {"video/webm", "video/mp4", "application/octet-stream"}:
            self._send_json(
                {"ok": False, "error": "只支持WebM或MP4视频"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            result = self.server.face_service.enroll_video(
                person_id, self.rfile.read(content_length), content_type
            )
            self._send_json({"ok": True, **result}, HTTPStatus.CREATED)
        except (ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        request_path = self.path.partition("?")[0]
        person_match = re.fullmatch(r"/api/people/([0-9a-f]{32})", request_path)
        if not person_match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        deleted = self.server.face_service.database.delete_person(person_match.group(1))
        if not deleted:
            self._send_json({"ok": False, "error": "人员不存在"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"ok": True})


class CameraDashboard:
    def __init__(self, config: Config, model_router: ModelRouter) -> None:
        self.config = config
        self.model_router = model_router
        self.store = CameraFrameStore()
        self.store.set_scene_broadcast_enabled(config.scene_broadcast_enabled)
        if config.person_detector == "yolo":
            person_detector = YoloPersonDetector(config)
        elif config.person_detector == "qwen":
            person_detector = model_router
        else:
            raise ValueError(
                f"不支持的人物检测器：{config.person_detector}，请使用yolo或qwen"
            )
        self.presence_monitor = PersonPresenceMonitor(
            self.store, person_detector, config.person_monitor_enabled
        )
        self.face_service = FaceRecognitionService(config)
        self.native_camera = NativeCameraMonitor(
            config, self.store, self.presence_monitor, self.face_service
        )
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
            self.model_router,
            self.presence_monitor,
            self.face_service,
            self.native_camera,
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
                    self.model_router,
                    self.presence_monitor,
                    self.face_service,
                    self.native_camera,
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
        self.native_camera.start()
        browser_disabled = (
            os.environ.get("LAOYE_NO_BROWSER") == "1"
            or os.environ.get("XIAOBU_NO_BROWSER") == "1"
        )
        if self.config.open_browser and not browser_disabled:
            threading.Timer(0.8, lambda: webbrowser.open(self.url)).start()

    def stop(self) -> None:
        self.native_camera.stop()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.presence_monitor.close()
        self.face_service.close()
