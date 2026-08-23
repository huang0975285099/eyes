from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class MediaAPIError(RuntimeError):
    pass


class MediaAPIClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def claim_jobs(
        self,
        worker_id: str,
        capabilities: list[str],
        max_jobs: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        response = self._post(
            "/api/internal/ai/jobs/claim",
            {
                "worker_id": worker_id,
                "capabilities": capabilities,
                "max_jobs": max_jobs,
                "lease_seconds": lease_seconds,
            },
        )
        jobs = response.get("jobs", [])
        if not isinstance(jobs, list):
            raise MediaAPIError("claim response does not contain a jobs list")
        return jobs

    def report_success(
        self, worker_id: str, job_id: int, frames: list[dict[str, Any]]
    ) -> None:
        self._post(
            "/api/internal/ai/jobs/report",
            {
                "worker_id": worker_id,
                "job_id": job_id,
                "success": True,
                "retryable": False,
                "frames": frames,
            },
        )

    def report_failure(
        self,
        worker_id: str,
        job_id: int,
        error: str,
        retryable: bool,
    ) -> None:
        self._post(
            "/api/internal/ai/jobs/report",
            {
                "worker_id": worker_id,
                "job_id": job_id,
                "success": False,
                "retryable": retryable,
                "error": error[:4000],
            },
        )

    def heartbeat(
        self,
        worker_id: str,
        hostname: str,
        version: str,
        capabilities: list[str],
        status: str,
        active_jobs: int,
        last_error: str,
    ) -> None:
        self._post(
            "/api/internal/ai/workers/heartbeat",
            {
                "worker_id": worker_id,
                "hostname": hostname,
                "version": version,
                "capabilities": capabilities,
                "status": status,
                "active_jobs": active_jobs,
                "last_error": last_error[:4000],
            },
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._base_url + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MediaAPIError(
                f"MediaService returned HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MediaAPIError(f"MediaService request failed: {exc}") from exc

        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaAPIError("MediaService returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise MediaAPIError("MediaService returned a non-object JSON value")
        return value
