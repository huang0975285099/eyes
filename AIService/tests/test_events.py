from __future__ import annotations

import unittest
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_service.events import AggregatedEvent, EventAggregator, EventEvidenceWriter, EventPolicy
from ai_service.modules import Detection, VideoFrame


class EventAggregatorTests(unittest.TestCase):
    def test_opens_after_continuous_hits_and_closes_after_gap(self) -> None:
        aggregator = EventAggregator()
        policy = EventPolicy(
            min_hits=2,
            max_gap_seconds=2,
            clear_after_seconds=1,
            cooldown_seconds=30,
        )
        started = datetime(2026, 8, 26, tzinfo=timezone.utc)

        first = aggregator.process(
            video_source_id=7,
            stream_name="camera-7",
            algorithm_code="intrusion",
            model_version="test-v1",
            detections=[Detection("person_intrusion", 0.8)],
            captured_at=started,
            policy=policy,
        )
        self.assertEqual(first, [])

        opened = aggregator.process(
            video_source_id=7,
            stream_name="camera-7",
            algorithm_code="intrusion",
            model_version="test-v1",
            detections=[Detection("person_intrusion", 0.9)],
            captured_at=started + timedelta(milliseconds=500),
            policy=policy,
        )
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].phase, "opened")
        self.assertIsNone(opened[0].ended_at)

        closed = aggregator.process(
            video_source_id=7,
            stream_name="camera-7",
            algorithm_code="intrusion",
            model_version="test-v1",
            detections=[],
            captured_at=started + timedelta(seconds=2),
            policy=policy,
        )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].phase, "closed")
        self.assertEqual(closed[0].event_id, opened[0].event_id)
        self.assertEqual(closed[0].confidence, 0.9)

    def test_short_noise_does_not_create_event(self) -> None:
        aggregator = EventAggregator()
        policy = EventPolicy(min_hits=3, clear_after_seconds=1)
        now = datetime.now(timezone.utc)
        aggregator.process(
            video_source_id=1,
            stream_name="camera-1",
            algorithm_code="quality",
            model_version="rules-v1",
            detections=[Detection("blur", 0.6)],
            captured_at=now,
            policy=policy,
        )
        closed = aggregator.process(
            video_source_id=1,
            stream_name="camera-1",
            algorithm_code="quality",
            model_version="rules-v1",
            detections=[],
            captured_at=now + timedelta(seconds=2),
            policy=policy,
        )
        self.assertEqual(closed, [])

    def test_writes_snapshot_and_clip_inside_event_root(self) -> None:
        now = datetime.now(timezone.utc)
        event = AggregatedEvent(
            event_id="event-1",
            phase="closed",
            video_source_id=1,
            stream_name="camera-1",
            algorithm_code="quality",
            event_type="frozen",
            confidence=0.9,
            started_at=now,
            ended_at=now + timedelta(seconds=1),
            snapshot_at=now,
            model_version="rules-v1",
        )
        frames = [
            VideoFrame(1, now, b"\xff\xd8one\xff\xd9"),
            VideoFrame(2, now + timedelta(seconds=1), b"\xff\xd8two\xff\xd9"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            writer = EventEvidenceWriter(
                Path(temporary), "ffmpeg", 10, 5, 3, 4
            )

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                Path(command[-1]).write_bytes(b"mp4")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with patch("subprocess.run", side_effect=fake_run):
                snapshot, clip = writer.write(event, frames)
            self.assertTrue(Path(snapshot).is_file())
            self.assertTrue(Path(clip).is_file())
            self.assertIn("_events", Path(snapshot).parts)


if __name__ == "__main__":
    unittest.main()
