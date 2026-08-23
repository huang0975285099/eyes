from __future__ import annotations

import logging
import signal

from .api_client import MediaAPIClient
from .config import Settings
from .health import start_health_server
from .modules import AnalyzerRegistry, FrameSamplerAnalyzer
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
            recording_root=settings.recording_root,
            ffmpeg_path=settings.ffmpeg_path,
            ffprobe_path=settings.ffprobe_path,
            command_timeout_seconds=settings.ffmpeg_timeout_seconds,
        )
    )
    state = ServiceState(settings.worker_id, registry.capabilities)
    health_server = start_health_server(state, settings.health_port)
    client = MediaAPIClient(
        settings.media_api, settings.request_timeout_seconds
    )
    worker = AnalysisWorker(settings, client, registry, state)

    def stop(_signum: int, _frame: object) -> None:
        worker.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        worker.run()
    finally:
        health_server.shutdown()
        health_server.server_close()


if __name__ == "__main__":
    main()
