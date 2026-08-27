from __future__ import annotations

import io
import threading
import unittest
from datetime import datetime, timedelta, timezone

from ai_service.modules import VideoFrame
from ai_service.config import Settings
from ai_service.modules import AnalyzerRegistry
from ai_service.realtime import (
    FrameRingBuffer,
    RealtimeCoordinator,
    StreamConfig,
    iter_mjpeg,
)
from ai_service.state import ServiceState


class RealtimeCoreTests(unittest.TestCase):
    def test_extracts_complete_jpegs_from_pipe(self) -> None:
        stream = io.BytesIO(
            b"noise\xff\xd8first\xff\xd9between\xff\xd8second\xff\xd9"
        )
        frames = list(iter_mjpeg(stream, threading.Event()))
        self.assertEqual(
            frames,
            [b"\xff\xd8first\xff\xd9", b"\xff\xd8second\xff\xd9"],
        )

    def test_ring_buffer_prunes_old_frames(self) -> None:
        ring = FrameRingBuffer(5)
        now = datetime.now(timezone.utc)
        ring.add(VideoFrame(1, now - timedelta(seconds=10), b"old"))
        ring.add(VideoFrame(2, now, b"new"))
        self.assertEqual([frame.jpeg for frame in ring.snapshot()], [b"new"])

    def test_parses_realtime_config(self) -> None:
        config = StreamConfig.from_payload(
            {
                "video_source_id": 9,
                "customer_id": 3,
                "stream_name": "camera-9",
                "input_url": "rtmp://srs:1935/live/camera-9",
                "fallback_url": "http://srs:8080/live/camera-9.m3u8",
                "rules": [
                    {
                        "algorithm_code": "INTRUSION",
                        "config": {"sample_fps": 2},
                    }
                ],
            }
        )
        self.assertEqual(config.rules[0].algorithm_code, "intrusion")
        self.assertEqual(config.rules[0].sample_fps(8), 2)

    def test_rejects_local_file_as_realtime_input(self) -> None:
        with self.assertRaises(ValueError):
            StreamConfig.from_payload(
                {
                    "video_source_id": 1,
                    "stream_name": "camera-1",
                    "input_url": "/var/recordings/video.mp4",
                    "rules": [],
                }
            )

    def test_coordinator_with_no_registered_algorithm_starts_no_stream(self) -> None:
        called = threading.Event()

        class FakeClient:
            def get_realtime_config(
                self, worker_id: str, capabilities: list[str]
            ) -> dict[str, object]:
                self.worker_id = worker_id
                self.capabilities = capabilities
                called.set()
                return {"streams": []}

            def report_events(
                self, worker_id: str, events: list[dict[str, object]]
            ) -> None:
                raise AssertionError("no event should be reported")

        settings = Settings.from_env()
        registry = AnalyzerRegistry()
        state = ServiceState("test-worker", [], [])
        coordinator = RealtimeCoordinator(
            settings, FakeClient(), registry, state  # type: ignore[arg-type]
        )
        coordinator.start()
        self.assertTrue(called.wait(2))
        coordinator.stop()
        snapshot = state.snapshot()
        self.assertEqual(snapshot["active_streams"], 0)
        self.assertTrue(snapshot["realtime_last_sync_at"])


if __name__ == "__main__":
    unittest.main()
