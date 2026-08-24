from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .base import AnalysisError, AnalysisResult, Analyzer

logger = logging.getLogger(__name__)


class FrameSamplerAnalyzer(Analyzer):
    """Capture one current frame directly from an SRS live stream."""

    code = "frame_sampler"

    def __init__(
        self,
        evidence_root: Path,
        ffmpeg_path: str,
        command_timeout_seconds: int,
    ) -> None:
        # Images share the durable evidence volume, but no MP4 recording is read.
        self._evidence_root = (evidence_root / "_frames").resolve()
        self._ffmpeg_path = ffmpeg_path
        self._timeout = command_timeout_seconds

    def analyze(self, job: dict[str, Any]) -> AnalysisResult:
        if str(job.get("input_type", "")) != "live_stream":
            raise AnalysisError(
                "frame_sampler only accepts live_stream jobs", retryable=False
            )
        stream_name = _safe_component(str(job.get("stream_name", "")))
        if not stream_name:
            raise AnalysisError("job stream_name is empty", retryable=False)
        input_url = _validated_stream_url(str(job.get("input_url", "")))
        fallback_value = str(job.get("fallback_url", "")).strip()
        fallback_url = (
            _validated_stream_url(fallback_value) if fallback_value else ""
        )
        job_id = int(job.get("id", 0) or 0)
        if job_id <= 0:
            raise AnalysisError("job id is invalid", retryable=False)

        frames_dir = (
            self._evidence_root
            / stream_name
            / f"{job_id // 10000:08d}"
        ).resolve()
        _require_within(self._evidence_root, frames_dir)
        frames_dir.mkdir(parents=True, exist_ok=True)
        output_path = frames_dir / f"job_{job_id}.jpg"
        if not output_path.exists() or output_path.stat().st_size == 0:
            errors: list[str] = []
            for stream_url in dict.fromkeys([input_url, fallback_url]):
                if not stream_url:
                    continue
                try:
                    self._capture(stream_url, output_path)
                    break
                except AnalysisError as exc:
                    errors.append(str(exc))
            else:
                raise AnalysisError("; ".join(errors) or "no live stream URL available")
        captured_at = datetime.fromtimestamp(output_path.stat().st_mtime, timezone.utc)
        return AnalysisResult(
            frames=[
                {
                    "frame_index": 1,
                    "file_path": str(output_path),
                    "captured_at": captured_at.isoformat(),
                }
            ]
        )

    def _capture(self, input_url: str, output_path: Path) -> None:
        temporary_path = output_path.with_name(output_path.stem + ".part.jpg")
        command = [
            self._ffmpeg_path,
            "-nostdin",
            "-fflags",
            "nobuffer",
            "-analyzeduration",
            "1000000",
            "-probesize",
            "1000000",
            "-i",
            input_url,
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(temporary_path),
        ]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            temporary_path.unlink(missing_ok=True)
            raise AnalysisError(f"ffmpeg timed out reading {input_url}") from exc
        except FileNotFoundError as exc:
            raise AnalysisError(
                f"ffmpeg executable was not found: {self._ffmpeg_path}",
                retryable=False,
            ) from exc

        if completed.returncode != 0:
            temporary_path.unlink(missing_ok=True)
            detail = completed.stderr[-1000:].strip()
            raise AnalysisError(
                f"ffmpeg failed to capture live stream {input_url}: {detail}"
            )
        if not temporary_path.exists() or temporary_path.stat().st_size == 0:
            temporary_path.unlink(missing_ok=True)
            raise AnalysisError(
                f"ffmpeg produced an empty frame for {input_url}"
            )
        os.replace(temporary_path, output_path)


def _validated_stream_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"rtmp", "rtmps", "http", "https"} or not parsed.netloc:
        raise AnalysisError("job input_url is invalid", retryable=False)
    return value.strip()


def _safe_component(value: str) -> str:
    value = value.strip()
    if value in {"", ".", ".."}:
        return ""
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", value)


def _require_within(root: Path, target: Path) -> None:
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise AnalysisError(
            f"path is outside the evidence root: {target}", retryable=False
        ) from exc
