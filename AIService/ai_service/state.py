from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass
class ServiceState:
    worker_id: str
    capabilities: list[str]
    realtime_capabilities: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _lock: Lock = field(default_factory=Lock, repr=False)
    _status: str = "starting"
    _active_jobs: int = 0
    _completed_jobs: int = 0
    _failed_jobs: int = 0
    _last_error: str = ""
    _last_heartbeat_at: str = ""
    _active_streams: int = 0
    _processed_frames: int = 0
    _emitted_events: int = 0
    _realtime_last_sync_at: str = ""
    _realtime_last_error: str = ""
    _dropped_frames: int = 0
    _analyzer_failures: int = 0
    _open_circuits: int = 0
    _unassigned_streams: int = 0

    def update(self, **values: Any) -> None:
        with self._lock:
            for name, value in values.items():
                setattr(self, f"_{name}", value)

    def increment(self, name: str) -> None:
        with self._lock:
            attribute = f"_{name}"
            setattr(self, attribute, getattr(self, attribute) + 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": self._status in {"running", "idle"},
                "worker_id": self.worker_id,
                "status": self._status,
                "capabilities": self.capabilities,
                "realtime_capabilities": self.realtime_capabilities,
                "active_jobs": self._active_jobs,
                "completed_jobs": self._completed_jobs,
                "failed_jobs": self._failed_jobs,
                "last_error": self._last_error,
                "last_heartbeat_at": self._last_heartbeat_at,
                "active_streams": self._active_streams,
                "processed_frames": self._processed_frames,
                "emitted_events": self._emitted_events,
                "realtime_last_sync_at": self._realtime_last_sync_at,
                "realtime_last_error": self._realtime_last_error,
                "dropped_frames": self._dropped_frames,
                "analyzer_failures": self._analyzer_failures,
                "open_circuits": self._open_circuits,
                "unassigned_streams": self._unassigned_streams,
                "started_at": self.started_at.isoformat(),
            }
