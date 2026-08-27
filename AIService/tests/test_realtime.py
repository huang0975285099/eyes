from __future__ import annotations

import io
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from ai_service.modules import (
    Detection,
    RealtimeAnalyzer,
    TemporalRealtimeAnalyzer,
    VideoFrame,
)
from ai_service.config import Settings
from ai_service.events import AggregatedEvent
from ai_service.modules import AnalyzerRegistry
from ai_service.realtime import (
    FrameRingBuffer,
    RealtimeCoordinator,
    RealtimeRule,
    RealtimeRuntime,
    StreamBuffers,
    StreamConfig,
    StreamManager,
    StreamSession,
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

    def test_rule_only_update_does_not_restart_stream(self) -> None:
        removed: list[StreamConfig] = []
        manager = StreamManager(
            ffmpeg_path="ffmpeg",
            max_fps=8,
            ring_seconds=30,
            temporal_seconds=8,
            reconnect_seconds=3,
            frame_width=960,
            ring_fps=4,
            callback=lambda *_args: None,
            remove_callback=lambda config, _buffers: removed.append(config),
        )
        first = self._stream_config({"sample_fps": 2, "threshold": 0.5})
        threshold_only = self._stream_config(
            {"sample_fps": 2, "threshold": 0.8}
        )
        higher_fps = self._stream_config({"sample_fps": 4, "threshold": 0.8})
        with patch.object(StreamSession, "start") as start, patch.object(
            StreamSession, "stop"
        ) as stop:
            manager.sync([first])
            session = manager._sessions[9]
            manager.sync([threshold_only])
            self.assertIs(manager._sessions[9], session)
            self.assertEqual(manager._sessions[9].config.rules[0].config["threshold"], 0.8)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(stop.call_count, 0)

            manager.sync([higher_fps])
            self.assertIsNot(manager._sessions[9], session)
            self.assertEqual(start.call_count, 2)
            self.assertEqual(stop.call_count, 1)
            manager.stop()
        self.assertTrue(removed)

    def test_disabling_one_rule_keeps_shared_stream_running(self) -> None:
        removed: list[StreamConfig] = []
        manager = StreamManager(
            ffmpeg_path="ffmpeg",
            max_fps=8,
            ring_seconds=30,
            temporal_seconds=8,
            reconnect_seconds=3,
            frame_width=960,
            ring_fps=4,
            callback=lambda *_args: None,
            remove_callback=lambda config, _buffers: removed.append(config),
        )
        base = self._stream_config({"sample_fps": 2})
        both = replace(
            base,
            rules=(
                RealtimeRule("quality", {"sample_fps": 2}),
                RealtimeRule("intrusion", {"sample_fps": 2}),
            ),
        )
        quality_only = replace(
            base, rules=(RealtimeRule("quality", {"sample_fps": 2}),)
        )
        with patch.object(StreamSession, "start") as start, patch.object(
            StreamSession, "stop"
        ) as stop:
            manager.sync([both])
            session = manager._sessions[9]
            manager.sync([quality_only])
            self.assertIs(manager._sessions[9], session)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(stop.call_count, 0)
            self.assertEqual(
                [rule.algorithm_code for rule in removed[-1].rules], ["intrusion"]
            )
            manager.stop()

    def test_temporal_analyzer_receives_shared_window(self) -> None:
        completed = threading.Event()

        class WindowAnalyzer(TemporalRealtimeAnalyzer):
            code = "fight"
            model_version = "test-v1"

            def analyze_window(
                self, frames: list[VideoFrame], config: dict[str, object]
            ) -> list[Detection]:
                self.sequences = [frame.sequence for frame in frames]
                completed.set()
                return []

        analyzer = WindowAnalyzer()
        registry = AnalyzerRegistry()
        registry.register_realtime(analyzer)
        state = ServiceState("test-worker", [], ["fight"])
        runtime = RealtimeRuntime(
            Settings.from_env(), _NoopClient(), registry, state  # type: ignore[arg-type]
        )
        now = datetime.now(timezone.utc)
        frames = [
            VideoFrame(1, now - timedelta(seconds=3), b"one"),
            VideoFrame(2, now - timedelta(seconds=1), b"two"),
            VideoFrame(3, now, b"three"),
        ]
        buffers = StreamBuffers(FrameRingBuffer(30), FrameRingBuffer(8))
        for frame in frames:
            buffers.evidence.add(frame)
            buffers.temporal.add(frame)
        stream = self._stream_config({"sample_fps": 4, "window_seconds": 2})
        runtime.handle_frame(stream, stream.rules[0], frames[-1], buffers)
        self.assertTrue(completed.wait(2))
        runtime.stop()
        self.assertEqual(analyzer.sequences, [2, 3])

    def test_slow_algorithm_drops_extra_work_instead_of_queueing(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class SlowAnalyzer(RealtimeAnalyzer):
            code = "quality"
            max_concurrency = 1

            def analyze_frame(
                self, frame: VideoFrame, config: dict[str, object]
            ) -> list[Detection]:
                started.set()
                release.wait(2)
                return []

        registry = AnalyzerRegistry()
        registry.register_realtime(SlowAnalyzer())
        settings = replace(Settings.from_env(), realtime_concurrency=2)
        state = ServiceState("test-worker", [], ["quality"])
        runtime = RealtimeRuntime(
            settings, _NoopClient(), registry, state  # type: ignore[arg-type]
        )
        now = datetime.now(timezone.utc)
        frame = VideoFrame(1, now, b"jpeg")
        buffers = StreamBuffers(FrameRingBuffer(30), FrameRingBuffer(8))
        buffers.evidence.add(frame)
        buffers.temporal.add(frame)
        first = self._stream_config({"sample_fps": 2}, source_id=9)
        second = self._stream_config({"sample_fps": 2}, source_id=10)
        runtime.handle_frame(first, first.rules[0], frame, buffers)
        self.assertTrue(started.wait(2))
        runtime.handle_frame(second, second.rules[0], frame, buffers)
        release.set()
        runtime.stop()
        self.assertGreaterEqual(state.snapshot()["dropped_frames"], 1)

    def test_repeated_algorithm_failures_open_circuit(self) -> None:
        class FailingAnalyzer(RealtimeAnalyzer):
            code = "quality"

            def __init__(self) -> None:
                self.calls = 0

            def analyze_frame(
                self, frame: VideoFrame, config: dict[str, object]
            ) -> list[Detection]:
                self.calls += 1
                raise RuntimeError("model failed")

        analyzer = FailingAnalyzer()
        registry = AnalyzerRegistry()
        registry.register_realtime(analyzer)
        settings = replace(
            Settings.from_env(),
            algorithm_failure_threshold=2,
            algorithm_retry_seconds=60,
        )
        state = ServiceState("test-worker", [], ["quality"])
        runtime = RealtimeRuntime(
            settings, _NoopClient(), registry, state  # type: ignore[arg-type]
        )
        now = datetime.now(timezone.utc)
        frame = VideoFrame(1, now, b"jpeg")
        buffers = StreamBuffers(FrameRingBuffer(30), FrameRingBuffer(8))
        buffers.evidence.add(frame)
        buffers.temporal.add(frame)
        stream = self._stream_config({"sample_fps": 2})
        with self.assertLogs("ai_service.realtime", level="ERROR"):
            for expected_failures in (1, 2):
                runtime.handle_frame(stream, stream.rules[0], frame, buffers)
                deadline = time.monotonic() + 2
                while (
                    state.snapshot()["analyzer_failures"] < expected_failures
                    or (stream.video_source_id, "quality") in runtime._busy
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
        runtime.handle_frame(stream, stream.rules[0], frame, buffers)
        runtime.stop()
        snapshot = state.snapshot()
        self.assertEqual(analyzer.calls, 2)
        self.assertEqual(snapshot["open_circuits"], 1)
        self.assertGreaterEqual(snapshot["dropped_frames"], 1)

    def test_same_event_is_published_in_order_while_executor_is_parallel(self) -> None:
        settings = replace(Settings.from_env(), event_concurrency=2)
        runtime = RealtimeRuntime(
            settings,
            _NoopClient(),  # type: ignore[arg-type]
            AnalyzerRegistry(),
            ServiceState("test-worker", [], []),
        )
        now = datetime.now(timezone.utc)
        order: list[str] = []

        def fake_publish(
            event: AggregatedEvent, _frames: list[VideoFrame]
        ) -> None:
            if event.phase == "opened":
                time.sleep(0.1)
            order.append(event.phase)

        runtime._publish = fake_publish  # type: ignore[method-assign]
        common = dict(
            event_id="same-event",
            video_source_id=1,
            stream_name="camera-1",
            algorithm_code="quality",
            event_type="frozen",
            confidence=0.9,
            started_at=now,
            snapshot_at=now,
            model_version="test-v1",
        )
        runtime._submit_event(
            AggregatedEvent(phase="opened", ended_at=None, **common), []
        )
        runtime._submit_event(
            AggregatedEvent(phase="closed", ended_at=now, **common), []
        )
        runtime.stop()
        self.assertEqual(order, ["opened", "closed"])

    @staticmethod
    def _stream_config(
        config: dict[str, object], source_id: int = 9
    ) -> StreamConfig:
        return StreamConfig(
            video_source_id=source_id,
            customer_id=3,
            stream_name=f"camera-{source_id}",
            input_url=f"rtmp://srs:1935/live/camera-{source_id}",
            fallback_url="",
            rules=(RealtimeRule("fight" if "window_seconds" in config else "quality", config),),
        )


class _NoopClient:
    def report_events(
        self, worker_id: str, events: list[dict[str, object]]
    ) -> None:
        return


if __name__ == "__main__":
    unittest.main()
