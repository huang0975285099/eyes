# -*- coding: utf-8 -*-
"""8771 服务层流程探针：不加载模型（打桩 worker），秒级回归 HTTP/SSE 编排。

覆盖三条容易回归出错的路径：
1. worker 已在运行时建立 SSE —— 首帧必须补发 modules，否则前端就绪判定永远
   等不到（worker 只在启动那一刻发一次 status），会白等 90s 后放弃标注流
2. 停止同一路后立刻重启 —— 必须等旧 worker 回收完成，否则两个 worker 同时
   拉同一路原始流、推同一路标注流（SRS 串流 / 画面闪烁）
3. 模块集合变更（mask_engine 开关、补 regions.json）—— 必须重建 worker，
   复用旧进程会沿用旧模块

用法：.venv-paddle\\Scripts\\python.exe assistant\\paddle_service\\probe_service_flow.py
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from assistant.paddle_service import paddle_server as S  # noqa: E402
from assistant.paddle_service.paddle_runner import PaddleRunner  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_STOP_DELAY = 3.0
_failed = 0


def _check(label: str, ok: bool, detail: str = "") -> None:
    global _failed
    print(f"{'  PASS' if ok else '  FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        _failed += 1


# ---- 打桩：避免真起 worker（模型加载约 25s）----
def _fake_start(self):
    self.requested_modules = list(self.modules)
    self.ready_modules = list(self.modules)
    self._running = True
    self._stopping = False
    self._stopped.clear()
    self._emit({"type": "status", "running": True,
                "modules": list(self.modules), "stream": self.stream_name})


_orig_stop = PaddleRunner.stop


def _slow_stop(self):
    """模拟优雅回收耗时，便于观察 start 是否真的等它。"""
    time.sleep(_STOP_DELAY)
    _orig_stop(self)


PaddleRunner.start = _fake_start
PaddleRunner.stop = _slow_stop


def _post(port: int, path: str, payload: dict, headers: dict | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    with _OPENER.open(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _get(port: int, path: str):
    with _OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode())


def _sse_first_event(port: int, stream: str, timeout: float = 5.0) -> dict:
    """建连并读取第一条 SSE data（不依赖 requests/httpx）。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(
            f"GET /events?stream={stream} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Accept: text/event-stream\r\n\r\n".encode())
        s.settimeout(timeout)
        buf = b""
        while b"\n\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        for line in buf.decode("utf-8", "replace").splitlines():
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])
        return {}
    finally:
        s.close()


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), S.PaddleHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"8771 测试实例 :{port}（worker 已打桩，不加载模型）")

    print("\n1) worker 已运行时建连 SSE —— 首帧应补发 modules")
    _post(port, "/analyze/start", {"stream": "usb1",
                                   "modules": ["fall", "fight", "intrusion", "mask"]})
    ev = _sse_first_event(port, "usb1")
    _check("首帧带 modules", isinstance(ev.get("modules"), list),
           f"收到 modules={ev.get('modules')}")
    _check("首帧 running=true", ev.get("running") is True)
    dup = _post(port, "/analyze/start", {"stream": "usb1",
                                         "modules": ["fall", "fight", "intrusion", "mask"]})
    _check("同模块重复 start 幂等", dup.get("already_running") is True, str(dup))

    print("\n2) 模块变更应重建 worker")
    changed = _post(port, "/analyze/start",
                    {"stream": "usb1", "modules": ["fall", "fight"]})
    _check("不同模块不复用旧 worker", not changed.get("already_running"), str(changed))

    print("\n3) 停止后立刻重启同一路 —— 必须等旧进程回收完")
    _post(port, "/analyze/stop", {"stream": "usb1"})
    t0 = time.time()
    _post(port, "/analyze/start", {"stream": "usb1", "modules": ["fall", "fight"]})
    elapsed = time.time() - t0
    _check("start 等到了旧 worker 回收", elapsed >= _STOP_DELAY * 0.8,
           f"耗时 {elapsed:.1f}s（旧 worker 回收需 {_STOP_DELAY}s）")

    print("\n4) /status 反映真实状态")
    time.sleep(_STOP_DELAY + 1.0)
    st = _get(port, "/status")
    streams = {s["stream"]: s for s in st["streams"]}
    _check("usb1 在运行", streams.get("usb1", {}).get("running") is True, str(streams))

    print("\n5) 关停：所有 runner 被清空")
    S._stop_all_runners()
    with S._runners_lock:
        left = len(S._runners) + len(S._stopping_runners)
    _check("无残留 runner", left == 0, f"残留 {left}")
    server.shutdown()
    server.server_close()

    print("\n" + ("全部通过" if _failed == 0 else f"{_failed} 项失败"))
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
