from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .api_client import MediaAPIClient, MediaAPIError
from .state import ServiceState

_WEB_ROOT = Path(__file__).with_name("web")
_FRAME_IMAGE = re.compile(r"^/api/dashboard/frames/(\d+)/image$")


def start_health_server(
    state: ServiceState, client: MediaAPIClient, port: int
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                payload = state.snapshot()
                self._json(200 if payload["ok"] else 503, payload)
                return
            if parsed.path == "/api/modules":
                self._json(
                    200,
                    {
                        "worker_id": state.worker_id,
                        "capabilities": state.capabilities,
                    },
                )
                return
            if parsed.path == "/api/dashboard/rules":
                self._proxy_json("/api/ai/rules")
                return
            if parsed.path == "/api/dashboard/frames":
                suffix = f"?{parsed.query}" if parsed.query else ""
                self._proxy_json("/api/frames" + suffix)
                return
            if parsed.path == "/api/dashboard/jobs":
                self._proxy_json("/api/ai/jobs/stats")
                return
            frame_match = _FRAME_IMAGE.match(parsed.path)
            if frame_match:
                self._proxy_frame(frame_match.group(1))
                return
            if parsed.path in {"/", "/index.html"}:
                self._static("index.html", "text/html; charset=utf-8")
                return
            if parsed.path == "/app.css":
                self._static("app.css", "text/css; charset=utf-8")
                return
            if parsed.path == "/app.js":
                self._static("app.js", "application/javascript; charset=utf-8")
                return
            self._json(404, {"error": "not found"})

        def do_PUT(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != "/api/dashboard/rules":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 256 * 1024:
                    raise ValueError("请求内容大小无效")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是JSON对象")
                result = client.put_json("/api/ai/rules", payload)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            except MediaAPIError as exc:
                self._json(502, {"error": str(exc)})
                return
            self._json(200, result)

        def _proxy_json(self, path: str) -> None:
            try:
                value = client.get_json(path)
            except MediaAPIError as exc:
                self._json(502, {"error": str(exc)})
                return
            self._json(200, value)

        def _proxy_frame(self, frame_id: str) -> None:
            try:
                body, content_type = client.get_bytes(f"/frames/{frame_id}/image")
            except MediaAPIError as exc:
                self._json(502, {"error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type or "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, name: str, content_type: str) -> None:
            try:
                body = (_WEB_ROOT / name).read_bytes()
            except OSError:
                self._json(404, {"error": "web resource not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self'; script-src 'self'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(
        target=server.serve_forever, name="dashboard-server", daemon=True
    )
    thread.start()
    return server
