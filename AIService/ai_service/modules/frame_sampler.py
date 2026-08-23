from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .base import AnalysisError, AnalysisResult, Analyzer

logger = logging.getLogger(__name__)


class FrameSamplerAnalyzer(Analyzer):
    code = "frame_sampler"

    def __init__(
        self,
        recording_root: Path,
        ffmpeg_path: str,
        ffprobe_path: str,
        command_timeout_seconds: int,
    ) -> None:
        self._recording_root = recording_root.resolve()
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._timeout = command_timeout_seconds

    def analyze(self, job: dict[str, Any]) -> AnalysisResult:
        input_path = self._validated_input_path(str(job.get("input_path", "")))
        stream_name = _safe_component(str(job.get("stream_name", "")))
        if not stream_name:
            raise AnalysisError("job stream_name is empty", retryable=False)

        started_at = _parse_datetime(str(job.get("started_at", "")))
        fallback_duration = float(job.get("duration", 0) or 0)
        duration = self._probe_duration(input_path, fallback_duration)
        offsets = plan_frame_offsets(duration)
        if not offsets:
            logger.info(
                "segment %s is shorter than 30 seconds; no frames required",
                job.get("segment_id"),
            )
            return AnalysisResult()

        frames_dir = (self._recording_root / "_frames" / stream_name).resolve()
        _require_within(self._recording_root / "_frames", frames_dir)
        frames_dir.mkdir(parents=True, exist_ok=True)

        artifacts: list[dict[str, Any]] = []
        for frame_index, offset in enumerate(offsets, start=1):
            output_path = frames_dir / f"{input_path.stem}_f{frame_index}.jpg"
            if not output_path.exists() or output_path.stat().st_size == 0:
                self._extract(input_path, output_path, offset)
            artifacts.append(
                {
                    "frame_index": frame_index,
                    "file_path": str(output_path),
                    "captured_at": (started_at + timedelta(seconds=offset)).isoformat(),
                }
            )
        return AnalysisResult(frames=artifacts)

    def _validated_input_path(self, value: str) -> Path:
        if not value:
            raise AnalysisError("job input_path is empty", retryable=False)
        path = Path(value).resolve()
        _require_within(self._recording_root, path)
        if not path.is_file():
            raise AnalysisError(f"recording segment does not exist: {path}")
        return path

    def _probe_duration(self, path: Path, fallback: float) -> float:
        command = [
            self._ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=True,
            )
            payload = json.loads(completed.stdout)
            value = float(payload["format"]["duration"])
            if value > 0:
                return value
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
            KeyError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            if fallback <= 0:
                raise AnalysisError(f"ffprobe failed for {path}: {exc}") from exc
            logger.warning("ffprobe failed for %s; using %.2fs: %s", path, fallback, exc)
        return fallback

    def _extract(self, input_path: Path, output_path: Path, offset: int) -> None:
        temporary_path = output_path.with_name(output_path.stem + ".part.jpg")
        command = [
            self._ffmpeg_path,
            "-nostdin",
            "-ss",
            str(offset),
            "-i",
            str(input_path),
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
            raise AnalysisError(
                f"ffmpeg timed out at offset {offset}s for {input_path}"
            ) from exc
        except FileNotFoundError as exc:
            raise AnalysisError(
                f"ffmpeg executable was not found: {self._ffmpeg_path}",
                retryable=False,
            ) from exc

        if completed.returncode != 0:
            temporary_path.unlink(missing_ok=True)
            detail = completed.stderr[-1000:].strip()
            raise AnalysisError(
                f"ffmpeg failed at offset {offset}s for {input_path}: {detail}"
            )
        if not temporary_path.exists() or temporary_path.stat().st_size == 0:
            temporary_path.unlink(missing_ok=True)
            raise AnalysisError(
                f"ffmpeg produced an empty frame at offset {offset}s for {input_path}"
            )
        os.replace(temporary_path, output_path)


def plan_frame_offsets(duration: float) -> list[int]:
    effective_duration = int(duration)
    if effective_duration < 30:
        return []
    frame_count = 2
    if effective_duration >= 600:
        frame_count = effective_duration // 300
    interval = effective_duration // frame_count
    return [interval * index + interval // 2 for index in range(frame_count)]


def _parse_datetime(value: str) -> datetime:
    if not value:
        raise AnalysisError("job started_at is empty", retryable=False)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnalysisError(
            f"job started_at is invalid: {value}", retryable=False
        ) from exc


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
            f"path is outside the recording root: {target}", retryable=False
        ) from exc
