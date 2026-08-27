from __future__ import annotations

import json
import io
import unittest
import urllib.error
import urllib.request

from ai_service.health import start_health_server
from ai_service.state import ServiceState


class FakeMediaClient:
    def __init__(self) -> None:
        self.saved: dict[str, object] | None = None
        self.saved_path = ""
        self.authorization = ""
        self.sources: list[dict[str, object]] = []
        self.logout_count = 0
        self.stream_path = ""
        self.stream_headers: dict[str, str] = {}

    def get_json(self, path: str, headers: dict[str, str] | None = None) -> dict[str, object] | list[object]:
        self.authorization = (headers or {}).get("Authorization", "")
        if path == "/api/portal/auth/status":
            return {"setup_required": False}
        if path == "/api/portal/auth/me":
            customer = self.authorization == "Bearer customer-session"
            return {"user": {"username": "customer" if customer else "admin", "role": "customer_admin" if customer else "platform_admin"}}
        if path == "/api/portal/sources":
            return {"sources": self.sources}
        if path.startswith("/api/portal/frames"):
            return []
        if path.startswith("/api/portal/segments"):
            return {"segments": []}
        if path.startswith("/api/portal/events"):
            return {"events": []}
        if path.startswith("/api/portal/analysis-rules"):
            return {"rules": []}
        if path == "/api/portal/jobs":
            return {"jobs": []}
        if path == "/api/portal/customers":
            return {"customers": []}
        raise AssertionError(path)

    def post_json(self, path: str, payload: dict[str, object], headers: dict[str, str] | None = None) -> dict[str, object]:
        self.authorization = (headers or {}).get("Authorization", "")
        if path == "/api/portal/auth/login":
            username = str(payload.get("username", ""))
            role = "customer_admin" if username == "customer" else "platform_admin"
            token = "customer-session" if role == "customer_admin" else "session-123"
            return {"session_token": token, "user": {"username": username, "role": role}}
        if path == "/api/portal/auth/logout":
            self.logout_count += 1
            return {"ok": True}
        self.saved = payload
        return {"ok": True}

    def put_json(self, path: str, payload: dict[str, object], headers: dict[str, str] | None = None) -> dict[str, object]:
        self.authorization = (headers or {}).get("Authorization", "")
        self.saved_path = path
        self.saved = payload
        return {"ok": True}

    def get_bytes(self, path: str, headers: dict[str, str] | None = None) -> tuple[bytes, str]:
        self.authorization = (headers or {}).get("Authorization", "")
        return b"jpeg-data", "image/jpeg"

    def open_stream(self, path: str, headers: dict[str, str] | None = None) -> object:
        self.authorization = (headers or {}).get("Authorization", "")
        self.stream_path = path
        self.stream_headers = dict(headers or {})

        class Response(io.BytesIO):
            status = 206
            headers = {
                "Content-Type": "video/mp4",
                "Content-Length": "9",
                "Content-Range": "bytes 0-8/9",
                "Accept-Ranges": "bytes",
            }

        return Response(b"mp4-bytes")


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
        self.assertIn("超级管理平台".encode(), body)
        _, _, body = self.get("/api/dashboard/auth/status")
        self.assertFalse(json.loads(body)["setup_required"])

    def test_dashboard_csp_allows_mse_blob_playback(self) -> None:
        request = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(request, timeout=3) as response:
            policy = response.headers["Content-Security-Policy"]
        self.assertIn("media-src 'self' blob:", policy)

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

    def test_platform_admin_can_update_source_operator(self) -> None:
        cookie = self.login()
        payload = {"video_source_id": 7, "operator_name": "张三"}
        request = urllib.request.Request(
            self.base + "/api/dashboard/source-operator",
            data=json.dumps(payload).encode(),
            method="PUT",
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.client.saved_path, "/api/portal/source-operator")
        self.assertEqual(self.client.saved, payload)

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

    def test_customer_native_login_returns_bearer_and_forwards_it(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/customer/auth/login",
            data=json.dumps({"username": "customer", "password": "password123"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Eyes-Native-App": "1"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read())
        self.assertEqual(result["session_token"], "customer-session")
        self.assertEqual(result["user"]["role"], "customer_admin")

        request = urllib.request.Request(
            self.base + "/api/customer/sources",
            headers={"Authorization": "Bearer customer-session", "Origin": "capacitor://localhost"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "capacitor://localhost")
            self.assertEqual(json.loads(response.read()), {"sources": []})
        self.assertEqual(self.client.authorization, "Bearer customer-session")

    def test_customer_recording_list_and_range_video_are_proxied(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/customer/segments?stream_name=camera-01",
            headers={"Authorization": "Bearer customer-session"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(json.loads(response.read()), {"segments": []})
        request = urllib.request.Request(
            self.base + "/api/customer/segments/12/video",
            headers={
                "Authorization": "Bearer customer-session",
                "Range": "bytes=0-8",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 0-8/9")
            self.assertEqual(response.read(), b"mp4-bytes")
        self.assertEqual(self.client.stream_path, "/api/portal/segments/12/video")
        self.assertEqual(self.client.stream_headers["Range"], "bytes=0-8")
        self.assertEqual(self.client.authorization, "Bearer customer-session")

    def test_customer_event_evidence_and_review_are_proxied(self) -> None:
        headers = {"Authorization": "Bearer customer-session"}
        request = urllib.request.Request(
            self.base + "/api/customer/events?status=pending", headers=headers
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(json.loads(response.read()), {"events": []})

        request = urllib.request.Request(
            self.base + "/api/customer/events/8/snapshot", headers=headers
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.read(), b"jpeg-data")

        request = urllib.request.Request(
            self.base + "/api/customer/events/8/clip",
            headers={**headers, "Range": "bytes=0-8"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"mp4-bytes")
        self.assertEqual(self.client.stream_path, "/api/portal/events/8/clip")

        payload = {"status": "confirmed"}
        request = urllib.request.Request(
            self.base + "/api/customer/events/8/review",
            data=json.dumps(payload).encode(),
            method="PUT",
            headers={**headers, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.client.saved_path, "/api/portal/events/8/review")
        self.assertEqual(self.client.saved, payload)

    def test_customer_cannot_enter_platform_dashboard(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/dashboard/auth/login",
            data=json.dumps({"username": "customer", "password": "password123"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(context.exception.code, 403)
        context.exception.close()
        self.assertEqual(self.client.logout_count, 1)

    def test_platform_token_cannot_call_customer_data_api(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/customer/sources",
            headers={"Authorization": "Bearer session-123", "Origin": "tauri://localhost"},
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(context.exception.code, 403)
        context.exception.close()

    def test_customer_portal_assets_are_served(self) -> None:
        status, content_type, body = self.get("/customer/")
        self.assertEqual((status, content_type), (200, "text/html"))
        self.assertIn(b"/customer/assets/", body)


if __name__ == "__main__":
    unittest.main()
