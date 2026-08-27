from __future__ import annotations

import logging
import signal

from .api_client import MediaAPIClient
from .config import Settings
from .health import start_health_server
from .modules import AnalyzerRegistry, FrameSamplerAnalyzer
from .realtime import RealtimeCoordinator
from .state import ServiceState
from .worker import AnalysisWorker


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env()
    registry = AnalyzerRegistry()
    registry.register(
        FrameSamplerAnalyzer(
            evidence_root=settings.evidence_root,
            ffmpeg_path=settings.ffmpeg_path,
            command_timeout_seconds=settings.ffmpeg_timeout_seconds,
        )
    )
    state = ServiceState(
        settings.worker_id,
        registry.capabilities,
        registry.realtime_capabilities,
    )
    client = MediaAPIClient(
        settings.media_api, settings.request_timeout_seconds
    )
    health_server = start_health_server(
        state,
        client,
        settings.health_port,
        settings.srs_public_base,
    )
    worker = AnalysisWorker(settings, client, registry, state)
    realtime = RealtimeCoordinator(settings, client, registry, state)

    def stop(_signum: int, _frame: object) -> None:
        worker.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    realtime.start()
    try:
        worker.run()
    finally:
        realtime.stop()
        health_server.shutdown()
        health_server.server_close()


if __name__ == "__main__":
    main()
