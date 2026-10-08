"""Web Dashboard HTTP 服务：托管 web/ 静态资源 + 生命周期事件。

职责：
- 静态文件托管（/ 与 /index.html + 兜底 web/ 目录的 css/js/mp3/wav）
- AppLifecycle（shutdown/restart 事件，供 main.py 与 tray 协调）
- HTTP server 启停骨架（IPv4 + IPv6）
"""

from __future__ import annotations

import mimetypes
import os
import socket
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .lifecycle import AppLifecycle
from .paths import APP_DIR


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


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
