"""Web Dashboard HTTP 服务。"""

from __future__ import annotations

import json
import mimetypes
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
        native_camera: NativeCameraMonitor,
    ):
        super().__init__(address, handler)
        self.store = store
        self.config = config
        self.model_router = model_router
        self.presence_monitor = presence_monitor
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
        if request_path in {"/", "/index.html", "/voice-preview"}:
            page_name = "voice-preview.html" if request_path == "/voice-preview" else "index.html"
            body = (APP_DIR / "web" / page_name).read_bytes()
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
                    },
                    "scene_broadcast": {
                        "enabled": self.server.store.scene_broadcast_enabled(),
                        "cooldown_seconds": self.server.config.scene_broadcast_cooldown_seconds,
                    },
                    "native_camera": {
                        "enabled": self.server.native_camera.enabled,
                        "retention_days": self.server.config.native_camera_event_retention_days,
                        "max_megabytes": self.server.config.native_camera_event_max_megabytes,
                        "cameras": [
                            {
                                "id": camera.id,
                                "name": camera.name,
                                "index": camera.index,
                                "enabled": camera.enabled,
                                "primary": camera.primary,
                                "motion": (
                                    self.server.native_camera.motion_rule(camera.id)
                                    if self.server.native_camera.has_camera(camera.id)
                                    else camera.motion.to_dict() if camera.motion else None
                                ),
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
            status["native_camera"] = self.server.native_camera.status()
            self._send_json(status)
            return
        if request_path == "/api/conversations":
            self._send_json(self.server.store.conversations())
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
        stream_match = re.fullmatch(r"/api/camera-stream/([A-Za-z0-9_-]+)", request_path)
        if stream_match:
            self._send_camera_stream(stream_match.group(1))
            return
        frame_match = re.fullmatch(r"/api/camera-frame/([A-Za-z0-9_-]+)", request_path)
        if frame_match:
            self._send_camera_frame(frame_match.group(1))
            return
        # 兜底：服务 web/ 目录下的静态文件（css/js/mp3/wav 等），支持 index.html 子资源
        if not request_path.startswith("/api/"):
            web_root = (APP_DIR / "web").resolve()
            candidate = (web_root / request_path.lstrip("/")).resolve()
            try:
                candidate.relative_to(web_root)
            except ValueError:
                candidate = None
            if candidate is not None and candidate.is_file():
                mimetypes.add_type("text/javascript", ".js")
                mime, _ = mimetypes.guess_type(candidate.name)
                body = candidate.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _send_camera_frame(self, camera_id: str) -> None:
        try:
            frame = self.server.native_camera.preview_frame(camera_id)
        except KeyError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if frame is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "camera frame unavailable")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(frame)

    def _send_camera_stream(self, camera_id: str) -> None:
        if not self.server.native_camera.has_camera(camera_id):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            frames = self.server.native_camera.preview_frames(camera_id)
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in frames:
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def do_POST(self) -> None:
        request_path = self.path.partition("?")[0]
        if request_path == "/api/asr":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0:
                    raise ValueError("音频数据为空")
                audio_bytes = self.rfile.read(content_length)
                content_type = self.headers.get("Content-Type", "") or "audio/webm"
                import urllib.error
                import urllib.request

                req = urllib.request.Request(
                    "http://127.0.0.1:8770/transcribe",
                    data=audio_bytes,
                    headers={"Content-Type": content_type},
                )
                try:
                    resp = urllib.request.urlopen(req, timeout=180)
                    self._send_json(json.loads(resp.read().decode("utf-8")))
                except urllib.error.HTTPError as upstream_error:
                    self._send_json(
                        json.loads(upstream_error.read().decode("utf-8")),
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
            except Exception as error:
                self._send_json(
                    {"text": "", "error": str(error)},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        motion_rule_match = re.fullmatch(
            r"/api/motion-rule/([A-Za-z0-9_-]+)", request_path
        )
        if motion_rule_match:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 4096:
                    raise ValueError("监控规则内容无效")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                rule = self.server.native_camera.update_motion_rule(
                    motion_rule_match.group(1), payload
                )
                self._send_json({"ok": True, "motion": rule})
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
                self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
            except OSError as error:
                self._send_json({"ok": False, "error": f"无法保存监控规则：{error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if request_path == "/api/shutdown":
            self.server.store.request_shutdown()
            self._send_json({"accepted": True}, HTTPStatus.ACCEPTED)
            return
        if request_path == "/api/restart":
            self._send_json({"accepted": True}, HTTPStatus.ACCEPTED)
            threading.Timer(0.15, self.server.store.request_restart).start()
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
                if enabled:
                    self.server.native_camera.ensure_enabled()
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
                    self.server.native_camera.ensure_enabled()
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
        self.send_error(HTTPStatus.NOT_FOUND)


class CameraDashboard:
    def __init__(self, config: Config, model_router: ModelRouter) -> None:
        self.config = config
        self.model_router = model_router
        self.store = CameraFrameStore(APP_DIR / "data" / "conversations.json")
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
        self.native_camera = NativeCameraMonitor(
            config,
            self.store,
            self.presence_monitor,
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
