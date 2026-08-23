from __future__ import annotations

import logging
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .api_client import RecordingAPIClient, RecordingAPIError
from .config import Settings
from .modules import AnalysisError, AnalysisResult, AnalyzerRegistry
from .state import ServiceState

logger = logging.getLogger(__name__)


class AnalysisWorker:
    def __init__(
        self,
        settings: Settings,
        client: RecordingAPIClient,
        registry: AnalyzerRegistry,
        state: ServiceState,
    ) -> None:
        self._settings = settings
        self._client = client
        self._registry = registry
        self._state = state
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=settings.concurrency, thread_name_prefix="analysis"
        )
        self._futures: dict[Future[AnalysisResult], dict[str, Any]] = {}
        self._last_heartbeat = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        logger.info(
            "AIService worker=%s modules=%s concurrency=%d",
            self._settings.worker_id,
            ",".join(self._registry.capabilities),
            self._settings.concurrency,
        )
        self._state.update(status="running")
        try:
            while not self._stop.is_set():
                self._collect_finished()
                self._send_heartbeat_if_due()
                self._claim_available_jobs()
                self._stop.wait(self._settings.poll_interval_seconds)
        finally:
            self._state.update(status="stopping")
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._collect_finished()
            self._send_heartbeat("stopped")

    def _claim_available_jobs(self) -> None:
        slots = self._settings.concurrency - len(self._futures)
        self._state.update(active_jobs=len(self._futures))
        if slots <= 0:
            return
        try:
            jobs = self._client.claim_jobs(
                self._settings.worker_id,
                self._registry.capabilities,
                slots,
                self._settings.job_lease_seconds,
            )
            self._state.update(last_error="", status="running" if jobs else "idle")
        except RecordingAPIError as exc:
            self._record_error(str(exc))
            return

        for job in jobs:
            analyzer = self._registry.get(str(job.get("algorithm_code", "")))
            future = self._executor.submit(analyzer.analyze, job)
            self._futures[future] = job
            logger.info(
                "claimed job=%s algorithm=%s segment=%s attempt=%s",
                job.get("id"),
                job.get("algorithm_code"),
                job.get("segment_id"),
                job.get("attempt"),
            )
        self._state.update(active_jobs=len(self._futures))

    def _collect_finished(self) -> None:
        completed = [future for future in self._futures if future.done()]
        for future in completed:
            job = self._futures.pop(future)
            job_id = int(job["id"])
            try:
                result = future.result()
                self._client.report_success(
                    self._settings.worker_id, job_id, result.frames
                )
            except AnalysisError as exc:
                self._report_failure(job_id, str(exc), exc.retryable)
            except Exception as exc:  # keep a bad module from killing the worker
                logger.exception("unexpected analyzer failure for job=%s", job_id)
                self._report_failure(job_id, str(exc), True)
            else:
                self._state.increment("completed_jobs")
                logger.info("completed job=%s frames=%d", job_id, len(result.frames))
        self._state.update(active_jobs=len(self._futures))

    def _report_failure(self, job_id: int, message: str, retryable: bool) -> None:
        self._state.increment("failed_jobs")
        self._record_error(message)
        logger.error("failed job=%s retryable=%s: %s", job_id, retryable, message)
        try:
            self._client.report_failure(
                self._settings.worker_id, job_id, message, retryable
            )
        except RecordingAPIError as exc:
            # The lease will expire and RecordingService will make the job
            # available again if the report cannot be delivered.
            self._record_error(str(exc))

    def _send_heartbeat_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat >= self._settings.heartbeat_interval_seconds:
            self._send_heartbeat("online")
            self._last_heartbeat = now

    def _send_heartbeat(self, status: str) -> None:
        snapshot = self._state.snapshot()
        try:
            self._client.heartbeat(
                worker_id=self._settings.worker_id,
                hostname=socket.gethostname(),
                version=__version__,
                capabilities=self._registry.capabilities,
                status=status,
                active_jobs=len(self._futures),
                last_error=str(snapshot["last_error"]),
            )
            self._state.update(
                last_heartbeat_at=datetime.now(timezone.utc).isoformat()
            )
        except RecordingAPIError as exc:
            self._record_error(str(exc))

    def _record_error(self, message: str) -> None:
        logger.warning(message)
        self._state.update(last_error=message, status="degraded")
