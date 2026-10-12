# -*- coding: utf-8 -*-
"""PP-Human 8771 HTTP 服务：/analyze/start|stop + /events(SSE) + /status + /ready。

独立进程加载飞桨 GPU 环境（.venv-paddle），避免与 torch 主环境 DLL 冲突。
前端通过 EventSource 订阅 /events，收到 fall/fight 事件后复用现有告警链路。

启动：.venv-paddle\\Scripts\\python.exe -u -m assistant.paddle_service.paddle_server
"""
from __future__ import annotations

import json
import queue
import signal
import sys
import time
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .paddle_runner import PaddleRunner, VOICE_DIR

_PORT = 8771
_DEFAULT_SRS_HOST = "10.0.20.219"


def _load_srs_host() -> str:
    """8771 是独立进程，不加载 Config 对象，直接读 config.json 的 srs_host。

    与 8765 反向代理、前端推拉流共用同一配置源，避免三处硬编码各自漂移。
    """
    try:
        with open(VOICE_DIR / "config.json", encoding="utf-8") as f:
            host = str(json.load(f).get("srs_host", "")).strip()
        return host or _DEFAULT_SRS_HOST
    except Exception:  # noqa: BLE001  配置缺失/损坏时退回默认，不阻断服务启动
        return _DEFAULT_SRS_HOST


SRS_HOST = _load_srs_host()
_runners: dict[str, PaddleRunner] = {}
# 正在后台回收的 runner：停止同一路后立刻重启时，"旧 worker 还没死、新 worker 已起"
# 会让两个进程同时拉同一路原始流、推同一路标注流（SRS 串流、画面闪烁），
# 所以 start 前必须先等它回收完成
_stopping_runners: dict[str, PaddleRunner] = {}
_runners_lock = threading.Lock()
_ALLOWED_MODULES = {"fall", "fight", "mask", "intrusion"}
# 口罩需 mask_engine=paddlehub 才加入；区域入侵需 conf/regions.json 配了该路禁区才真正生效
_DEFAULT_MODULES = ["fall", "fight", "intrusion"]


def _get_runner(stream: str, modules: list[str] | None = None) -> PaddleRunner:
    with _runners_lock:
        runner = _runners.get(stream)
        if runner is None:
            runner = PaddleRunner(stream, modules, SRS_HOST)
            _runners[stream] = runner
        return runner


def _start_runner(stream: str, modules: list[str]) -> dict:
    with _runners_lock:
        pending = _stopping_runners.get(stream)
    if pending is not None:
        print(f"[start] {stream} 上一次回收未完成，等待其退出", flush=True)
        pending.wait_stopped(timeout=_SHUTDOWN_WAIT_SECONDS)
    with _runners_lock:
        runner = _runners.get(stream)
        if runner is not None and runner.running:
            # 已在运行且模块一致 → 幂等复用。模块不一致（改了 mask_engine、补了
            # regions.json、页面刷新）必须重建：复用旧 worker 会沿用旧模块
            if set(getattr(runner, "requested_modules", runner.modules)) == set(modules):
                return {"ok": True, "stream": stream, "already_running": True}
            stale = _runners.pop(stream)
        else:
            stale = None
    if stale is not None:
        print(f"[start] {stream} 模块变更，重建分析进程", flush=True)
        stale.request_stop()
    # 已停止的旧 runner 直接替换（stop_flag 已置位，后台 join 回收中）
    with _runners_lock:
        runner = _runners.get(stream)
        if runner is None or not runner.running:
            runner = PaddleRunner(stream, modules, SRS_HOST)
            _runners[stream] = runner
    runner.start()
    return {"ok": True, "stream": stream}


def _stop_runner(stream: str) -> dict:
    with _runners_lock:
        runner = _runners.pop(stream, None)
        if runner is None:
            return {"ok": True, "stream": stream, "not_running": True}
        _stopping_runners[stream] = runner

    def _reap() -> None:
        try:
            runner.stop()
        except Exception as error:  # noqa: BLE001
            print(f"[stop] {stream} 回收失败: {error}", flush=True)
        finally:
            with _runners_lock:
                _stopping_runners.pop(stream, None)

    threading.Thread(target=_reap, name=f"stop-{stream}", daemon=True).start()
    return {"ok": True, "stream": stream}


_shutdown_started = threading.Event()
_shutdown_done = threading.Event()
# 等 /shutdown 线程回收完 worker 的上限：优雅宽限 25s + terminate 5s + kill 5s + 余量
_SHUTDOWN_WAIT_SECONDS = 40


def _stop_all_runners() -> None:
    """停掉并回收全部 runner（幂等：已清空则为空操作）。"""
    with _runners_lock:
        runners = list(_runners.values())
        runners += list(_stopping_runners.values())  # 正在回收的也等它结束
        _runners.clear()
    for runner in runners:
        try:
            runner.stop()
        except Exception as error:  # noqa: BLE001
            print(f"[shutdown] 停止 {runner.stream_name} 失败: {error}", flush=True)


def _stop_all_and_shutdown(server) -> None:
    """顺序停掉所有 runner（各自含 25s 优雅宽限），再让 serve_forever 返回。

    必须在独立线程调用：server.shutdown() 不能在请求处理线程内执行，否则自锁。
    """
    if _shutdown_started.is_set():
        return
    _shutdown_started.set()
    try:
        _stop_all_runners()
        print("[shutdown] 所有分析流已停止，正在关闭 8771", flush=True)
        server.shutdown()
    finally:
        _shutdown_done.set()


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
        if path == "/shutdown":
            self._shutdown()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _shutdown(self) -> None:
        """停掉所有 worker 后关闭服务（main.py 退出前调用）。

        必须走 HTTP 正常退出路径：父进程 terminate() 在 Windows 是硬杀，
        paddle_worker 与其 ffmpeg 会变孤儿，继续占显存并往 SRS 推 RTMP。
        自定义头 X-Local-Shutdown 是防页面 CSRF：跨域请求需先预检，
        而 OPTIONS 未放行该头，浏览器会拦下（本地脚本/curl 不受影响）。
        """
        if self.headers.get("X-Local-Shutdown") != "1":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self._respond({"ok": True, "shutting_down": True})
        threading.Thread(
            target=_stop_all_and_shutdown, args=(self.server,),
            name="paddle-shutdown", daemon=True,
        ).start()

    def _read_body(self) -> tuple[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                return "usb1", list(_DEFAULT_MODULES)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            stream = str(payload.get("stream", "usb1")).strip() or "usb1"
            raw_modules = payload.get("modules", _DEFAULT_MODULES)
            if not isinstance(raw_modules, list):
                raw_modules = list(_DEFAULT_MODULES)
            modules = [m for m in raw_modules if isinstance(m, str) and m in _ALLOWED_MODULES]
            return stream, modules or list(_DEFAULT_MODULES)
        except Exception:
            return "usb1", list(_DEFAULT_MODULES)

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
            status = {
                "type": "status", "running": runner.running,
                "stream": stream, "timestamp": time.time(),
            }
            # worker 已在运行（页面刷新 / 同一路重复开始巡检）：必须补发实际模块，
            # 否则前端的就绪判定（要求带 modules）永远等不到 —— worker 只在启动那
            # 一刻发一次 status，前端会白等 PADDLE_READY_TIMEOUT 后放弃标注流
            if runner.running and runner.ready_modules:
                status["modules"] = list(runner.ready_modules)
            self._sse_write(status)
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


def _cleanup(server) -> None:
    """serve_forever 返回后的清理：忽略后续 Ctrl+C，保证 worker 一定被回收。

    Windows 控制台的 Ctrl+C 会发给共享控制台的**所有**进程。8771 收到后
    serve_forever 抛 KeyboardInterrupt，若在 finally 的 runner.stop() 等待期间
    再次命中，就会抛栈退出并漏掉 worker 回收（曾出现 traceback + worker/ffmpeg
    残留）。这里先屏蔽信号，再走与 /shutdown 相同的回收流程。
    """
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:  # noqa: BLE001  非主线程等场景下设置失败不影响清理
        pass
    try:
        _stop_all_runners()
        server.server_close()
    except Exception as error:  # noqa: BLE001
        print(f"[shutdown] 清理异常: {error}", flush=True)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", _PORT), PaddleHandler)
    print(f"PP-Human 分析服务已启动：http://127.0.0.1:{_PORT}/  （飞桨 GPU 环境）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPP-Human 分析服务正在停止……", flush=True)
    finally:
        # /shutdown 已在跑（主进程优雅停止路径）：它持有 runner 引用并在回收 worker，
        # 必须等它做完，否则进程一退 daemon 线程被杀 -> worker 变孤儿
        if _shutdown_started.is_set():
            _shutdown_done.wait(timeout=_SHUTDOWN_WAIT_SECONDS)
        _cleanup(server)


if __name__ == "__main__":
    main()
