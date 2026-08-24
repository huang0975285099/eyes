from __future__ import annotations

import json
import unittest
import urllib.request

from ai_service.health import start_health_server
from ai_service.state import ServiceState


class FakeMediaClient:
    def __init__(self) -> None:
        self.saved: dict[str, object] | None = None
        self.authorization = ""
        self.sources: list[dict[str, object]] = []

    def get_json(self, path: str, headers: dict[str, str] | None = None) -> dict[str, object] | list[object]:
        self.authorization = (headers or {}).get("Authorization", "")
        if path == "/api/portal/auth/status":
            return {"setup_required": False}
        if path == "/api/portal/auth/me":
            return {"user": {"username": "admin", "role": "platform_admin"}}
        if path == "/api/portal/sources":
            return {"sources": self.sources}
        if path.startswith("/api/portal/frames"):
            return []
        if path == "/api/portal/jobs":
            return {"jobs": []}
        if path == "/api/portal/customers":
            return {"customers": []}
        raise AssertionError(path)

    def post_json(self, path: str, payload: dict[str, object], headers: dict[str, str] | None = None) -> dict[str, object]:
        self.authorization = (headers or {}).get("Authorization", "")
        if path == "/api/portal/auth/login":
            return {"session_token": "session-123", "user": {"username": "admin", "role": "platform_admin"}}
        if path == "/api/portal/auth/logout":
            return {"ok": True}
        self.saved = payload
        return {"ok": True}

    def put_json(self, path: str, payload: dict[str, object], headers: dict[str, str] | None = None) -> dict[str, object]:
        self.authorization = (headers or {}).get("Authorization", "")
        self.saved = payload
        return {"ok": True}

    def get_bytes(self, path: str, headers: dict[str, str] | None = None) -> tuple[bytes, str]:
        self.authorization = (headers or {}).get("Authorization", "")
        return b"jpeg-data", "image/jpeg"


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeMediaClient()
        state = ServiceState("test-worker", ["frame_sampler"])
        state.update(status="idle")
        self.server = start_health_server(
            state, self.client, 0, "http://srs-public.example:8080"
        )  # type: ignore[arg-type]
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def get(self, path: str, cookie: str = "") -> tuple[int, str, bytes]:
        request = urllib.request.Request(self.base + path, headers={"Cookie": cookie})
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.headers.get_content_type(), response.read()

    def login(self) -> str:
        request = urllib.request.Request(
            self.base + "/api/dashboard/auth/login",
            data=json.dumps({"username": "admin", "password": "password123"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            return response.headers["Set-Cookie"].split(";", 1)[0]

    def test_serves_dashboard_and_auth_status(self) -> None:
        status, content_type, body = self.get("/")
        self.assertEqual((status, content_type), (200, "text/html"))
        self.assertIn("视频与AI服务平台".encode(), body)
        _, _, body = self.get("/api/dashboard/auth/status")
        self.assertFalse(json.loads(body)["setup_required"])

    def test_login_forwards_bearer_session(self) -> None:
        cookie = self.login()
        _, _, body = self.get("/api/dashboard/sources", cookie)
        self.assertEqual(json.loads(body), {"sources": []})
        self.assertEqual(self.client.authorization, "Bearer session-123")

        payload = {"sources": [{"video_source_id": 1, "recording_enabled": True}]}
        request = urllib.request.Request(
            self.base + "/api/dashboard/sources",
            data=json.dumps(payload).encode(),
            method="PUT",
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.client.saved, payload)

    def test_proxies_authorized_frame_image(self) -> None:
        cookie = self.login()
        status, content_type, body = self.get("/api/dashboard/frames/9/image", cookie)
        self.assertEqual((status, content_type, body), (200, "image/jpeg", b"jpeg-data"))
        self.assertEqual(self.client.authorization, "Bearer session-123")

    def test_live_streams_are_built_from_authorized_portal_sources(self) -> None:
        self.client.sources = [
            {
                "stream_name": "customer-camera-1",
                "display_name": "客户一号摄像头",
                "active": True,
                "codec": "HEVC",
                "width": 1920,
                "height": 1080,
            },
            {"stream_name": "offline-camera", "active": False},
        ]
        cookie = self.login()
        _, _, body = self.get("/api/dashboard/streams", cookie)
        streams = json.loads(body)["streams"]
        self.assertEqual(len(streams), 1)
        self.assertEqual(
            streams[0]["playback_url"],
            "http://srs-public.example:8080/live/customer-camera-1.flv",
        )
        self.assertEqual(self.client.authorization, "Bearer session-123")


if __name__ == "__main__":
    unittest.main()
