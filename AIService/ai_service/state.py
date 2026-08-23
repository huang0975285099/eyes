from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass
class ServiceState:
    worker_id: str
    capabilities: list[str]
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _lock: Lock = field(default_factory=Lock, repr=False)
    _status: str = "starting"
    _active_jobs: int = 0
    _completed_jobs: int = 0
    _failed_jobs: int = 0
    _last_error: str = ""
    _last_heartbeat_at: str = ""

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
                "active_jobs": self._active_jobs,
                "completed_jobs": self._completed_jobs,
                "failed_jobs": self._failed_jobs,
                "last_error": self._last_error,
                "last_heartbeat_at": self._last_heartbeat_at,
                "started_at": self.started_at.isoformat(),
            }
