# -*- coding: utf-8 -*-
"""PP-Human 8771 HTTP 服务：/analyze/start|stop + /events(SSE) + /status + /ready。

独立进程加载飞桨 GPU 环境（.venv-paddle），避免与 torch 主环境 DLL 冲突。
前端通过 EventSource 订阅 /events，收到 fall/fight 事件后复用现有告警链路。

启动：.venv-paddle\\Scripts\\python.exe -u -m assistant.paddle_service.paddle_server
"""
from __future__ import annotations

import json
import queue
import sys
import time
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .paddle_runner import PaddleRunner

_PORT = 8771
_runners: dict[str, PaddleRunner] = {}
_runners_lock = threading.Lock()
_ALLOWED_MODULES = {"fall", "fight", "mask"}


def _get_runner(stream: str, modules: list[str] | None = None) -> PaddleRunner:
    with _runners_lock:
        runner = _runners.get(stream)
        if runner is None:
            runner = PaddleRunner(stream, modules)
            _runners[stream] = runner
        return runner


def _start_runner(stream: str, modules: list[str]) -> dict:
    with _runners_lock:
        runner = _runners.get(stream)
        already = bool(runner and runner.running)
    if already:
        return {"ok": True, "stream": stream, "already_running": True}
    # 已停止的旧 runner 直接替换（stop_flag 已置位，后台 join 回收中）
    with _runners_lock:
        runner = _runners.get(stream)
        if runner is None or not runner.running:
            runner = PaddleRunner(stream, modules)
            _runners[stream] = runner
    runner.start()
    return {"ok": True, "stream": stream}


def _stop_runner(stream: str) -> dict:
    with _runners_lock:
        runner = _runners.pop(stream, None)
    if runner is None:
        return {"ok": True, "stream": stream, "not_running": True}
    runner.request_stop()  # 立即返回，后台线程 join
    return {"ok": True, "stream": stream}


class PaddleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def handle_error(self, request, client_address) -> None:
        # 浏览器/curl 主动断开 SSE 长连接属正常行为，不打 traceback
        if isinstance(sys.exc_info()[1], (ConnectionError, ConnectionResetError,
                                          ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def do_OPTIONS(self) -> None:
        body = b""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/ready":
            self._respond({"ready": True})
            return
        if path == "/status":
            with _runners_lock:
                streams = [
                    {"stream": name, "running": runner.running,
                     "error": runner.error, "modules": list(runner.modules)}
                    for name, runner in _runners.items()
                ]
            self._respond({"streams": streams})
            return
        if path == "/events":
            stream = parse_qs(parsed.query).get("stream", ["usb1"])[0]
            self._handle_sse(stream)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/analyze/start":
            stream, modules = self._read_body()
            self._respond(_start_runner(stream, modules))
            return
        if path == "/analyze/stop":
            stream, _modules = self._read_body()
            self._respond(_stop_runner(stream))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _read_body(self) -> tuple[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                return "usb1", ["fall", "fight"]
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            stream = str(payload.get("stream", "usb1")).strip() or "usb1"
            raw_modules = payload.get("modules", ["fall", "fight"])
            if not isinstance(raw_modules, list):
                raw_modules = ["fall", "fight"]
            modules = [m for m in raw_modules if isinstance(m, str) and m in _ALLOWED_MODULES]
            return stream, modules or ["fall", "fight"]
        except Exception:
            return "usb1", ["fall", "fight"]

    def _respond(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass

    def _handle_sse(self, stream: str) -> None:
        runner = _get_runner(stream)
        q = runner.subscribe()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self._sse_write({
                "type": "status", "running": runner.running,
                "stream": stream, "timestamp": time.time(),
            })
            while True:
                try:
                    event = q.get(timeout=15)
                    self._sse_write(event)
                except queue.Empty:
                    self._sse_raw(b": heartbeat\n\n")  # 保活，防中间代理断连
        except (ConnectionError, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            runner.unsubscribe(q)

    def _sse_write(self, event: dict) -> None:
        data = json.dumps(event, ensure_ascii=False)
        self._sse_raw(f"data: {data}\n\n".encode("utf-8"))

    def _sse_raw(self, raw) -> None:
        self.wfile.write(raw)
        self.wfile.flush()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", _PORT), PaddleHandler)
    print(f"PP-Human 分析服务已启动：http://127.0.0.1:{_PORT}/  （飞桨 GPU 环境）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPP-Human 分析服务正在停止……", flush=True)
    finally:
        with _runners_lock:
            runners = list(_runners.values())
            _runners.clear()
        for runner in runners:
            runner.stop()
        server.server_close()


if __name__ == "__main__":
    main()
