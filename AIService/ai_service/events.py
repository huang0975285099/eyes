from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .modules import Detection, VideoFrame

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventPolicy:
    min_hits: int = 3
    max_gap_seconds: float = 2.0
    clear_after_seconds: float = 2.0
    cooldown_seconds: float = 60.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "EventPolicy":
        return cls(
            min_hits=_bounded_int(config.get("min_hits"), 3, 1, 1000),
            max_gap_seconds=_bounded_float(
                config.get("max_gap_seconds"), 2.0, 0.1, 3600.0
            ),
            clear_after_seconds=_bounded_float(
                config.get("clear_after_seconds"), 2.0, 0.1, 3600.0
            ),
            cooldown_seconds=_bounded_float(
                config.get("cooldown_seconds"), 60.0, 0.0, 86400.0
            ),
        )


@dataclass(frozen=True)
class AggregatedEvent:
    event_id: str
    phase: str
    video_source_id: int
    stream_name: str
    algorithm_code: str
    event_type: str
    confidence: float
    started_at: datetime
    ended_at: datetime | None
    snapshot_at: datetime
    model_version: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def api_payload(self, snapshot_path: str, clip_path: str) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata["event_phase"] = self.phase
        return {
            "event_id": self.event_id,
            "video_source_id": self.video_source_id,
            "stream_name": self.stream_name,
            "algorithm_code": self.algorithm_code,
            "event_type": self.event_type,
            "confidence": self.confidence,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "snapshot_path": snapshot_path,
            "clip_path": clip_path,
            "model_version": self.model_version,
            "metadata": metadata,
        }


@dataclass
class _Candidate:
    video_source_id: int
    stream_name: str
    algorithm_code: str
    event_type: str
    model_version: str
    first_at: datetime
    last_at: datetime
    snapshot_at: datetime
    hits: int
    max_confidence: float
    metadata: dict[str, Any]
    event_id: str = ""


class EventAggregator:
    """Turns noisy per-frame hits into open/close event lifecycle signals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._candidates: dict[tuple[int, str, str], _Candidate] = {}
        self._cooldowns: dict[tuple[int, str, str], datetime] = {}

    def process(
        self,
        *,
        video_source_id: int,
        stream_name: str,
        algorithm_code: str,
        model_version: str,
        detections: list[Detection],
        captured_at: datetime,
        policy: EventPolicy,
    ) -> list[AggregatedEvent]:
        captured_at = _aware_utc(captured_at)
        best: dict[str, Detection] = {}
        for detection in detections:
            event_type = detection.event_type.strip().lower()
            if not event_type or not 0 <= detection.confidence <= 1:
                continue
            identity = detection.key.strip() or event_type
            current = best.get(identity)
            if current is None or detection.confidence > current.confidence:
                best[identity] = Detection(
                    event_type=event_type,
                    confidence=detection.confidence,
                    key=identity,
                    metadata=dict(detection.metadata),
                )

        emitted: list[AggregatedEvent] = []
        with self._lock:
            prefix = (video_source_id, algorithm_code)
            for key, candidate in list(self._candidates.items()):
                if key[:2] != prefix or key[2] in best:
                    continue
                if (captured_at - candidate.last_at).total_seconds() >= policy.clear_after_seconds:
                    completed = self._close(key, candidate, policy)
                    if completed is not None:
                        emitted.append(completed)

            for identity, detection in best.items():
                key = (video_source_id, algorithm_code, identity)
                cooldown_until = self._cooldowns.get(key)
                if cooldown_until is not None and captured_at < cooldown_until:
                    continue
                candidate = self._candidates.get(key)
                if candidate is not None and (
                    captured_at - candidate.last_at
                ).total_seconds() > policy.max_gap_seconds:
                    completed = self._close(key, candidate, policy)
                    if completed is not None:
                        emitted.append(completed)
                    candidate = None
                    if captured_at < self._cooldowns.get(key, captured_at):
                        continue
                if candidate is None:
                    candidate = _Candidate(
                        video_source_id=video_source_id,
                        stream_name=stream_name,
                        algorithm_code=algorithm_code,
                        event_type=detection.event_type,
                        model_version=model_version,
                        first_at=captured_at,
                        last_at=captured_at,
                        snapshot_at=captured_at,
                        hits=1,
                        max_confidence=detection.confidence,
                        metadata=dict(detection.metadata),
                    )
                    self._candidates[key] = candidate
                else:
                    candidate.hits += 1
                    candidate.last_at = captured_at
                    if detection.confidence >= candidate.max_confidence:
                        candidate.max_confidence = detection.confidence
                        candidate.snapshot_at = captured_at
                        candidate.metadata = dict(detection.metadata)
                if not candidate.event_id and candidate.hits >= policy.min_hits:
                    candidate.event_id = uuid.uuid4().hex
                    emitted.append(self._event(candidate, "opened", None))
            self._prune_cooldowns(captured_at)
        return emitted

    def flush_stream(
        self, video_source_id: int, algorithm_code: str = ""
    ) -> list[AggregatedEvent]:
        emitted: list[AggregatedEvent] = []
        with self._lock:
            for key, candidate in list(self._candidates.items()):
                if key[0] != video_source_id or (
                    algorithm_code and key[1] != algorithm_code
                ):
                    continue
                self._candidates.pop(key, None)
                if candidate.event_id:
                    emitted.append(self._event(candidate, "closed", candidate.last_at))
        return emitted

    def _close(
        self,
        key: tuple[int, str, str],
        candidate: _Candidate,
        policy: EventPolicy,
    ) -> AggregatedEvent | None:
        self._candidates.pop(key, None)
        if not candidate.event_id:
            return None
        self._cooldowns[key] = candidate.last_at + timedelta(
            seconds=policy.cooldown_seconds
        )
        return self._event(candidate, "closed", candidate.last_at)

    @staticmethod
    def _event(
        candidate: _Candidate, phase: str, ended_at: datetime | None
    ) -> AggregatedEvent:
        return AggregatedEvent(
            event_id=candidate.event_id,
            phase=phase,
            video_source_id=candidate.video_source_id,
            stream_name=candidate.stream_name,
            algorithm_code=candidate.algorithm_code,
            event_type=candidate.event_type,
            confidence=candidate.max_confidence,
            started_at=candidate.first_at,
            ended_at=ended_at,
            snapshot_at=candidate.snapshot_at,
            model_version=candidate.model_version,
            metadata=dict(candidate.metadata),
        )

    def _prune_cooldowns(self, now: datetime) -> None:
        for key, expires_at in list(self._cooldowns.items()):
            if expires_at <= now:
                self._cooldowns.pop(key, None)


class EventEvidenceWriter:
    def __init__(
        self,
        root: Path,
        ffmpeg_path: str,
        timeout_seconds: int,
        pre_seconds: float,
        post_seconds: float,
        clip_fps: float,
    ) -> None:
        self._root = (root / "_events").resolve()
        self._ffmpeg_path = ffmpeg_path
        self._timeout = timeout_seconds
        self._pre = pre_seconds
        self._post = post_seconds
        self._clip_fps = clip_fps

    def write(
        self, event: AggregatedEvent, frames: list[VideoFrame]
    ) -> tuple[str, str]:
        directory = (
            self._root
            / _safe_component(event.stream_name)
            / event.started_at.strftime("%Y/%m/%d")
        ).resolve()
        _require_within(self._root, directory)
        directory.mkdir(parents=True, exist_ok=True)
        snapshot_path = directory / f"{event.event_id}.jpg"
        if event.phase == "opened" or not snapshot_path.is_file():
            snapshot = _nearest_frame(frames, event.snapshot_at)
            if snapshot is None:
                raise RuntimeError("event ring buffer does not contain a snapshot")
            _atomic_write(snapshot_path, snapshot.jpeg)

        clip_path = directory / f"{event.event_id}.mp4"
        window_end = (event.ended_at or event.snapshot_at) + timedelta(
            seconds=self._post
        )
        window_start = event.started_at - timedelta(seconds=self._pre)
        clip_frames = [
            frame
            for frame in frames
            if window_start <= frame.captured_at <= window_end
        ]
        if len(clip_frames) >= 2:
            try:
                duration = (
                    clip_frames[-1].captured_at - clip_frames[0].captured_at
                ).total_seconds()
                actual_fps = (
                    min(self._clip_fps, max(0.2, (len(clip_frames) - 1) / duration))
                    if duration > 0
                    else self._clip_fps
                )
                self._write_clip(clip_path, clip_frames, actual_fps)
                return str(snapshot_path), str(clip_path)
            except RuntimeError as exc:
                logger.warning("event clip was skipped event=%s: %s", event.event_id, exc)
        if clip_path.is_file() and clip_path.stat().st_size > 0:
            return str(snapshot_path), str(clip_path)
        clip_path.unlink(missing_ok=True)
        return str(snapshot_path), ""

    def _write_clip(
        self, output_path: Path, frames: list[VideoFrame], clip_fps: float
    ) -> None:
        temporary = output_path.with_name(output_path.stem + ".part.mp4")
        command = [
            self._ffmpeg_path,
            "-nostdin",
            "-f",
            "image2pipe",
            "-framerate",
            f"{clip_fps:g}",
            "-vcodec",
            "mjpeg",
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(temporary),
        ]
        try:
            completed = subprocess.run(
                command,
                input=b"".join(frame.jpeg for frame in frames),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"failed to create event clip: {exc}") from exc
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            detail = completed.stderr[-1000:].decode("utf-8", errors="replace")
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg failed to create event clip: {detail}")
        os.replace(temporary, output_path)


def _nearest_frame(
    frames: list[VideoFrame], captured_at: datetime
) -> VideoFrame | None:
    if not frames:
        return None
    return min(
        frames,
        key=lambda frame: abs((frame.captured_at - captured_at).total_seconds()),
    )


def _atomic_write(path: Path, body: bytes) -> None:
    temporary = path.with_name(path.stem + ".part" + path.suffix)
    temporary.write_bytes(body)
    os.replace(temporary, path)


def _safe_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:-]", "_", value.strip())
    if safe in {"", ".", ".."}:
        raise ValueError("invalid stream name")
    return safe


def _require_within(root: Path, target: Path) -> None:
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("event evidence path is outside root") from exc


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _bounded_float(
    value: Any, default: float, minimum: float, maximum: float
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))
