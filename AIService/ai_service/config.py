from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class Settings:
    media_api: str
    recording_root: Path
    worker_id: str
    concurrency: int
    poll_interval_seconds: float
    heartbeat_interval_seconds: float
    job_lease_seconds: int
    request_timeout_seconds: float
    ffmpeg_path: str
    ffprobe_path: str
    ffmpeg_timeout_seconds: int
    health_port: int

    @classmethod
    def from_env(cls) -> "Settings":
        default_worker = f"{socket.gethostname()}-{os.getpid()}"
        return cls(
            media_api=os.getenv("AI_MEDIA_API", "http://media-service:22222").rstrip("/"),
            recording_root=Path(
                os.getenv("AI_RECORDING_ROOT", "/var/recordings")
            ),
            worker_id=os.getenv("AI_WORKER_ID", default_worker).strip(),
            concurrency=_positive_int("AI_CONCURRENCY", 4),
            poll_interval_seconds=_positive_float("AI_POLL_INTERVAL", 3),
            heartbeat_interval_seconds=_positive_float(
                "AI_HEARTBEAT_INTERVAL", 10
            ),
            job_lease_seconds=_positive_int("AI_JOB_LEASE_SECONDS", 300),
            request_timeout_seconds=_positive_float("AI_REQUEST_TIMEOUT", 15),
            ffmpeg_path=os.getenv("AI_FFMPEG_PATH", "ffmpeg"),
            ffprobe_path=os.getenv("AI_FFPROBE_PATH", "ffprobe"),
            ffmpeg_timeout_seconds=_positive_int("AI_FFMPEG_TIMEOUT", 120),
            health_port=_positive_int("AI_HEALTH_PORT", 11111),
        )
