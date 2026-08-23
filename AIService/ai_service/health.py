from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .state import ServiceState


def start_health_server(state: ServiceState, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                payload = state.snapshot()
                self._json(200 if payload["ok"] else 503, payload)
                return
            if self.path == "/api/modules":
                self._json(
                    200,
                    {
                        "worker_id": state.worker_id,
                        "capabilities": state.capabilities,
                    },
                )
                return
            self._json(404, {"error": "not found"})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(
        target=server.serve_forever, name="health-server", daemon=True
    )
    thread.start()
    return server
