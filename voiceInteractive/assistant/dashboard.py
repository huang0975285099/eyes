"""Web Dashboard HTTP 服务：托管 web/ 静态资源 + 生命周期事件。

职责：
- 静态文件托管（/ 与 /index.html + 兜底 web/ 目录的 css/js/mp3/wav）
- AppLifecycle（shutdown/restart 事件，供 main.py 与 tray 协调）
- HTTP server 启停骨架（IPv4 + IPv6）
"""

from __future__ import annotations

import mimetypes
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .lifecycle import AppLifecycle
from .paths import APP_DIR

# SRS 信令同源反向代理目标：
# 浏览器页面在 localhost（local 级）直连 10.0.20.x（private 级）会被 Chrome PNA 策略拦截，
# 由本机 8765 服务把 /srs/* 转发到 SRS 的 1985，浏览器侧即为同源请求。
# WebRTC 媒体面（UDP 8000）不经过此代理，浏览器仍直连 SRS。
SRS_PROXY_TARGET = "http://10.0.20.219:1985"
SRS_PROXY_PREFIX = "/srs/"
_SRS_PROXY_ALLOWED_METHODS = {"GET", "POST", "OPTIONS"}
_SRS_PROXY_TIMEOUT = 10
_SRS_PROXY_MAX_BODY = 512 * 1024


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, config=None):
        self.config = config
        super().__init__(server_address, RequestHandlerClass)


class DashboardHTTPServerV6(DashboardHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        request_path = self.path.partition("?")[0]
        if request_path.startswith(SRS_PROXY_PREFIX):
            self._proxy_to_srs()
            return
        if request_path in {"/", "/index.html"}:
            body = (APP_DIR / "web" / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if request_path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if request_path == "/app-config":
            # 前端只读的少量运行配置（口罩检测引擎等）
            cfg = getattr(self.server, "config", None)
            payload = json.dumps(
                {"mask_engine": getattr(cfg, "mask_engine", "qwen")},
                ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
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
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path.partition("?")[0].startswith(SRS_PROXY_PREFIX):
            self._proxy_to_srs()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        if self.path.partition("?")[0].startswith(SRS_PROXY_PREFIX):
            self._proxy_to_srs()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _proxy_to_srs(self) -> None:
        """把 /srs/<path> 原样转发到 SRS 1985（仅信令，小包短连接；同源后浏览器不再发 CORS 预检）。"""
        if self.command not in _SRS_PROXY_ALLOWED_METHODS:
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        rest = self.path[len(SRS_PROXY_PREFIX):]
        target = f"{SRS_PROXY_TARGET}/{rest}"
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > _SRS_PROXY_MAX_BODY:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        body = self.rfile.read(length) if length > 0 else None
        req = urllib.request.Request(target, data=body, method=self.command)
        content_type = self.headers.get("Content-Type")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=_SRS_PROXY_TIMEOUT) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                self.send_header(
                    "Content-Type", resp.headers.get("Content-Type", "application/json")
                )
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as error:
            payload = error.read()
            self.send_response(error.code)
            self.send_header(
                "Content-Type", error.headers.get("Content-Type", "application/json")
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (OSError, urllib.error.URLError) as error:
            msg = f'{{"code":-1,"error":"SRS proxy unreachable: {error}"}}'.encode("utf-8")
            self.send_response(HTTPStatus.BAD_GATEWAY)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)


class CameraDashboard:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = AppLifecycle()
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
            config=self.config,
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
                    config=self.config,
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
        browser_disabled = (
            os.environ.get("LAOYE_NO_BROWSER") == "1"
            or os.environ.get("XIAOBU_NO_BROWSER") == "1"
        )
        if self.config.open_browser and not browser_disabled:
            threading.Timer(0.8, lambda: webbrowser.open(self.url)).start()

    def stop(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
