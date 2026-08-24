from __future__ import annotations

import json
import unittest
import urllib.request

from ai_service.health import start_health_server
from ai_service.state import ServiceState


class FakeMediaClient:
    def __init__(self) -> None:
        self.saved: dict[str, object] | None = None

    def get_json(self, path: str) -> dict[str, object] | list[object]:
        if path == "/api/ai/rules":
            return {"algorithm": {"code": "frame_sampler"}, "sources": []}
        if path.startswith("/api/frames"):
            return []
        if path == "/api/ai/jobs/stats":
            return {"jobs": [], "workers": []}
        if path == "/api/streams":
            return [
                {
                    "stream_name": "camera-main",
                    "display_name": "大门摄像头",
                    "codec": "HEVC",
                    "width": 1920,
                    "height": 1080,
                    "active": True,
                }
            ]
        raise AssertionError(path)

    def put_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        self.saved = payload
        return {"ok": True}

    def get_bytes(self, path: str) -> tuple[bytes, str]:
        self.assert_path = path
        return b"jpeg-data", "image/jpeg"


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeMediaClient()
        state = ServiceState("test-worker", ["frame_sampler"])
        state.update(status="idle")
        self.server = start_health_server(state, self.client, 0)  # type: ignore[arg-type]
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def get(self, path: str) -> tuple[int, str, bytes]:
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return response.status, response.headers.get_content_type(), response.read()

    def test_serves_dashboard_and_rules(self) -> None:
        request = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(request, timeout=3) as response:
            status = response.status
            content_type = response.headers.get_content_type()
            content_security_policy = response.headers["Content-Security-Policy"]
            body = response.read()
        self.assertEqual((status, content_type), (200, "text/html"))
        self.assertIn("AI分析平台".encode(), body)
        self.assertIn("实时监控".encode(), body)
        self.assertIn(b"/mpegts.min.js", body)
        self.assertIn("http://127.0.0.1:8080", content_security_policy)

        status, content_type, body = self.get("/api/dashboard/rules")
        self.assertEqual((status, content_type), (200, "application/json"))
        self.assertEqual(json.loads(body)["algorithm"]["code"], "frame_sampler")

    def test_updates_rules_and_proxies_images(self) -> None:
        payload = {
            "algorithm_code": "frame_sampler",
            "enabled_source_ids": [1, 2, 3],
            "config": {"frames_per_minute": 2},
        }
        request = urllib.request.Request(
            self.base + "/api/dashboard/rules",
            data=json.dumps(payload).encode(),
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.client.saved, payload)

        status, content_type, body = self.get("/api/dashboard/frames/9/image")
        self.assertEqual((status, content_type, body), (200, "image/jpeg", b"jpeg-data"))

    def test_returns_browser_playback_urls_for_online_streams(self) -> None:
        status, content_type, body = self.get("/api/dashboard/streams")
        self.assertEqual((status, content_type), (200, "application/json"))
        streams = json.loads(body)["streams"]
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["codec"], "HEVC")
        self.assertEqual(
            streams[0]["playback_url"],
            "http://127.0.0.1:8080/live/camera-main.flv",
        )


if __name__ == "__main__":
    unittest.main()
