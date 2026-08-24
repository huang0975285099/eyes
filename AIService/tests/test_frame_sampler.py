from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_service.modules import AnalysisError
from ai_service.modules.frame_sampler import (
    FrameSamplerAnalyzer,
    _validated_stream_url,
)


class LiveFrameSamplerTests(unittest.TestCase):
    def test_rejects_recording_segment_job(self) -> None:
        analyzer = FrameSamplerAnalyzer(Path(tempfile.mkdtemp()), "ffmpeg", 10)
        with self.assertRaises(AnalysisError):
            analyzer.analyze(
                {
                    "id": 1,
                    "input_type": "recording_segment",
                    "stream_name": "camera-1",
                    "input_url": "rtmp://srs:1935/live/camera-1",
                }
            )

    def test_validates_live_stream_url(self) -> None:
        self.assertEqual(
            _validated_stream_url("rtmp://srs:1935/live/camera-1"),
            "rtmp://srs:1935/live/camera-1",
        )
        with self.assertRaises(AnalysisError):
            _validated_stream_url("/var/recordings/segment.mp4")

    def test_captures_one_current_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            analyzer = FrameSamplerAnalyzer(Path(temporary), "ffmpeg", 10)

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                Path(command[-1]).write_bytes(b"jpeg")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("subprocess.run", side_effect=fake_run) as run:
                result = analyzer.analyze(
                    {
                        "id": 42,
                        "input_type": "live_stream",
                        "stream_name": "camera-1",
                        "input_url": "rtmp://srs:1935/live/camera-1",
                    }
                )

            self.assertEqual(len(result.frames), 1)
            frame_path = Path(result.frames[0]["file_path"])
            self.assertEqual(frame_path.read_bytes(), b"jpeg")
            self.assertIn("_frames", frame_path.parts)
            self.assertIn("rtmp://srs:1935/live/camera-1", run.call_args.args[0])

    def test_falls_back_to_hls_when_rtmp_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            analyzer = FrameSamplerAnalyzer(Path(temporary), "ffmpeg", 10)
            attempts: list[str] = []

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                input_url = command[command.index("-i") + 1]
                attempts.append(input_url)
                if input_url.startswith("rtmp://"):
                    return subprocess.CompletedProcess(command, 1, "", "unsupported codec")
                Path(command[-1]).write_bytes(b"jpeg")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("subprocess.run", side_effect=fake_run):
                result = analyzer.analyze(
                    {
                        "id": 43,
                        "input_type": "live_stream",
                        "stream_name": "camera-1",
                        "input_url": "rtmp://srs:1935/live/camera-1",
                        "fallback_url": "http://srs:8080/live/camera-1.m3u8",
                    }
                )

            self.assertEqual(len(result.frames), 1)
            self.assertEqual(
                attempts,
                [
                    "rtmp://srs:1935/live/camera-1",
                    "http://srs:8080/live/camera-1.m3u8",
                ],
            )


if __name__ == "__main__":
    unittest.main()
