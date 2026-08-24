from __future__ import annotations

import json
import mimetypes
import re
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from .api_client import MediaAPIClient, MediaAPIError
from .state import ServiceState

_WEB_ROOT = Path(__file__).with_name("web")
_PACKAGED_CUSTOMER_WEB_ROOT = Path(__file__).with_name("customer_web")
_SOURCE_CUSTOMER_WEB_ROOT = Path(__file__).resolve().parents[2] / "h5" / "dist" / "spa"
_CUSTOMER_WEB_ROOT = (
    _PACKAGED_CUSTOMER_WEB_ROOT
    if _PACKAGED_CUSTOMER_WEB_ROOT.is_dir()
    else _SOURCE_CUSTOMER_WEB_ROOT
)
_FRAME_IMAGE = re.compile(r"^/api/dashboard/frames/(\d+)/image$")
_CUSTOMER_FRAME_IMAGE = re.compile(r"^/api/customer/frames/(\d+)/image$")
_SESSION_COOKIE = "eyes_session"
_MPEGTS_CDN = "https://cdn.jsdelivr.net/npm/mpegts.js@1.8.0/dist/mpegts.min.js"


def start_health_server(
    state: ServiceState,
    client: MediaAPIClient,
    port: int,
    srs_public_base: str = "",
) -> ThreadingHTTPServer:
    configured_srs_base = srs_public_base.strip().rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                payload = state.snapshot()
                self._json(200 if payload["ok"] else 503, payload)
                return
            if parsed.path == "/api/modules":
                self._json(200, {"worker_id": state.worker_id, "capabilities": state.capabilities})
                return
            get_routes = {
                "/api/dashboard/auth/status": ("/api/portal/auth/status", False, ""),
                "/api/dashboard/auth/me": ("/api/portal/auth/me", True, "platform_admin"),
                "/api/dashboard/sources": ("/api/portal/sources", True, "platform_admin"),
                "/api/dashboard/customers": ("/api/portal/customers", True, "platform_admin"),
                "/api/dashboard/jobs": ("/api/portal/jobs", True, "platform_admin"),
                "/api/dashboard/frames": ("/api/portal/frames", True, "platform_admin"),
                "/api/customer/auth/me": ("/api/portal/auth/me", True, "customer_admin"),
                "/api/customer/sources": ("/api/portal/sources", True, "customer_admin"),
                "/api/customer/jobs": ("/api/portal/jobs", True, "customer_admin"),
                "/api/customer/frames": ("/api/portal/frames", True, "customer_admin"),
            }
            if parsed.path in get_routes:
                target, authenticated, required_role = get_routes[parsed.path]
                suffix = f"?{parsed.query}" if parsed.query else ""
                self._proxy_json(
                    "GET", target + suffix,
                    authenticated=authenticated, required_role=required_role,
                )
                return
            if parsed.path == "/api/dashboard/streams":
                self._streams(required_role="platform_admin")
                return
            if parsed.path == "/api/customer/streams":
                self._streams(required_role="customer_admin")
                return
            frame_match = _FRAME_IMAGE.match(parsed.path)
            if frame_match:
                self._proxy_frame(frame_match.group(1), required_role="platform_admin")
                return
            customer_frame_match = _CUSTOMER_FRAME_IMAGE.match(parsed.path)
            if customer_frame_match:
                self._proxy_frame(customer_frame_match.group(1), required_role="customer_admin")
                return
            if parsed.path == "/customer" or parsed.path.startswith("/customer/"):
                self._customer_static(parsed.path)
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
            if parsed.path == "/mpegts.min.js":
                self._mpegts_script()
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            routes = {
                "/api/dashboard/auth/setup": ("/api/portal/auth/setup", False),
                "/api/dashboard/auth/login": ("/api/portal/auth/login", False),
                "/api/dashboard/auth/logout": ("/api/portal/auth/logout", True),
                "/api/dashboard/customers": ("/api/portal/customers", True),
                "/api/customer/auth/login": ("/api/portal/auth/login", False),
                "/api/customer/auth/logout": ("/api/portal/auth/logout", True),
            }
            if path not in routes:
                self._json(404, {"error": "not found"})
                return
            target, authenticated = routes[path]
            payload = self._read_json()
            if payload is None:
                return
            try:
                result = client.post_json(
                    target, payload, self._auth_headers() if authenticated else None
                )
            except MediaAPIError as exc:
                self._media_error(exc)
                return
            if path in {"/api/dashboard/auth/setup", "/api/dashboard/auth/login", "/api/customer/auth/login"}:
                token = str(result.get("session_token", ""))
                if not token:
                    self._json(502, {"error": "登录服务未返回会话"})
                    return
                role = str(result.get("user", {}).get("role", "")) if isinstance(result.get("user"), dict) else ""
                expected_role = "customer_admin" if path.startswith("/api/customer/") else "platform_admin"
                if role != expected_role:
                    try:
                        client.post_json("/api/portal/auth/logout", {}, {"Authorization": f"Bearer {token}"})
                    except MediaAPIError:
                        pass
                    self._json(403, {"error": "当前账号无权登录此管理平台"})
                    return
                native_client = self.headers.get("X-Eyes-Native-App", "") == "1"
                if not native_client:
                    result.pop("session_token", None)
                self._session_cookie(token)
            elif path in {"/api/dashboard/auth/logout", "/api/customer/auth/logout"}:
                self._session_cookie("", clear=True)
            self._json(200, result)

        def do_PUT(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            routes = {
                "/api/dashboard/sources": "/api/portal/sources",
                "/api/dashboard/source-owner": "/api/portal/source-owner",
                "/api/dashboard/customers": "/api/portal/customers",
                "/api/customer/sources": "/api/portal/sources",
                "/api/customer/auth/password": "/api/portal/auth/password",
            }
            if path not in routes:
                self._json(404, {"error": "not found"})
                return
            payload = self._read_json()
            if payload is None:
                return
            try:
                required_role = "customer_admin" if path.startswith("/api/customer/") else "platform_admin"
                headers = self._authorized_headers(required_role)
                result = client.put_json(routes[path], payload, headers)
            except MediaAPIError as exc:
                self._media_error(exc)
                return
            self._json(200, result)

        def do_OPTIONS(self) -> None:  # noqa: N802
            if urlsplit(self.path).path.startswith("/api/customer/") and self._cors_origin():
                self.send_response(204)
                self._cors_headers()
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Eyes-Native-App")
                self.send_header("Access-Control-Max-Age", "86400")
                self.end_headers()
                return
            self._json(404, {"error": "not found"})

        def _proxy_json(
            self, method: str, path: str, authenticated: bool, required_role: str = ""
        ) -> None:
            try:
                headers = self._authorized_headers(required_role) if authenticated else None
                if method == "GET":
                    value = client.get_json(path, headers)
                else:
                    raise ValueError("unsupported proxy method")
            except MediaAPIError as exc:
                self._media_error(exc)
                return
            self._json(200, value)

        def _proxy_frame(self, frame_id: str, required_role: str = "") -> None:
            try:
                headers = self._authorized_headers(required_role)
                body, content_type = client.get_bytes(
                    f"/api/portal/frames/{frame_id}/image", headers
                )
            except MediaAPIError as exc:
                self._media_error(exc)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type or "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            if urlsplit(self.path).path.startswith("/api/customer/"):
                self._cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def _streams(self, required_role: str = "") -> None:
            try:
                headers = self._authorized_headers(required_role)
                value = client.get_json("/api/portal/sources", headers)
            except MediaAPIError as exc:
                self._media_error(exc)
                return
            rows = value.get("sources", []) if isinstance(value, dict) else []
            if not isinstance(rows, list):
                self._json(502, {"error": "MediaService返回的视频源数据无效"})
                return
            playback_base = self._srs_base()
            streams: list[dict[str, object]] = []
            for item in rows:
                if not isinstance(item, dict) or not item.get("active"):
                    continue
                stream_name = str(item.get("stream_name", "")).strip()
                if not stream_name:
                    continue
                row = dict(item)
                row["playback_url"] = (
                    f"{playback_base}/live/{quote(stream_name, safe='')}.flv"
                    if playback_base
                    else ""
                )
                streams.append(row)
            self._json(200, {"streams": streams})

        def _srs_base(self) -> str:
            if configured_srs_base:
                return configured_srs_base
            host_header = self.headers.get("Host", "").strip()
            parsed_host = urlsplit("//" + host_header)
            hostname = parsed_host.hostname
            if not hostname:
                return ""
            public_host = f"[{hostname}]" if ":" in hostname else hostname
            forwarded_proto = self.headers.get("X-Forwarded-Proto", "http")
            scheme = forwarded_proto.split(",", 1)[0].strip().lower()
            if scheme not in {"http", "https"}:
                scheme = "http"
            return f"{scheme}://{public_host}:8080"

        def _auth_headers(self) -> dict[str, str]:
            authorization = self.headers.get("Authorization", "").strip()
            if authorization.lower().startswith("bearer "):
                return {"Authorization": authorization}
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(_SESSION_COOKIE)
            token = morsel.value if morsel is not None else ""
            return {"Authorization": f"Bearer {token}"}

        def _authorized_headers(self, required_role: str = "") -> dict[str, str]:
            headers = self._auth_headers()
            if not required_role:
                return headers
            identity = client.get_json("/api/portal/auth/me", headers)
            user = identity.get("user", {}) if isinstance(identity, dict) else {}
            role = str(user.get("role", "")) if isinstance(user, dict) else ""
            if role != required_role:
                platform = "客户平台" if required_role == "customer_admin" else "超级管理员平台"
                raise MediaAPIError(f"当前账号无权访问{platform}", 403)
            return headers

        def _read_json(self) -> dict[str, object] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 256 * 1024:
                    raise ValueError("请求内容大小无效")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是JSON对象")
                return payload
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return None

        def _session_cookie(self, token: str, clear: bool = False) -> None:
            max_age = 0 if clear else 7 * 24 * 60 * 60
            value = "" if clear else token
            self._pending_cookie = (
                f"{_SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
            )

        def _media_error(self, exc: MediaAPIError) -> None:
            status = exc.status_code if 400 <= exc.status_code < 500 else 502
            self._json(status, {"error": str(exc)})

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
                "style-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
                f"connect-src 'self' {self._srs_origin()}",
            )
            self.end_headers()
            self.wfile.write(body)

        def _customer_static(self, request_path: str) -> None:
            relative = request_path.removeprefix("/customer").lstrip("/") or "index.html"
            target = (_CUSTOMER_WEB_ROOT / relative).resolve()
            root = _CUSTOMER_WEB_ROOT.resolve()
            if root not in target.parents and target != root:
                self._json(404, {"error": "not found"})
                return
            if not target.is_file():
                target = _CUSTOMER_WEB_ROOT / "index.html"
            try:
                body = target.read_bytes()
            except OSError:
                self._json(503, {"error": "客户门户尚未构建"})
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if target.name == "index.html" else "public, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob: http: https:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                f"connect-src 'self' http: https: {self._srs_origin()}",
            )
            self.end_headers()
            self.wfile.write(body)

        def _mpegts_script(self) -> None:
            if (_WEB_ROOT / "mpegts.min.js").is_file():
                self._static("mpegts.min.js", "application/javascript; charset=utf-8")
                return
            self.send_response(302)
            self.send_header("Location", _MPEGTS_CDN)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

        def _srs_origin(self) -> str:
            parsed = urlsplit(self._srs_base())
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
            return ""

        def _cors_origin(self) -> str:
            origin = self.headers.get("Origin", "").strip()
            allowed = {
                "http://localhost", "https://localhost", "capacitor://localhost",
                "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
            }
            if origin in allowed or origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:"):
                return origin
            return ""

        def _cors_headers(self) -> None:
            origin = self._cors_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Credentials", "true")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if urlsplit(self.path).path.startswith("/api/customer/"):
                self._cors_headers()
            pending_cookie = getattr(self, "_pending_cookie", "")
            if pending_cookie:
                self.send_header("Set-Cookie", pending_cookie)
                self._pending_cookie = ""
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(
        target=server.serve_forever, name="dashboard-server", daemon=True
    )
    thread.start()
    return server
