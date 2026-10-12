# -*- coding: utf-8 -*-
"""PP-Human 单路分析的子进程编排器（8771 服务侧，轻量，不导入 paddle）。

为什么分析逻辑在独立子进程（paddle_worker.py）：
OpenCV(FFmpeg) 在非主线程调用 cv2.VideoCapture 打开 HTTP-FLV 会永久挂起，
PP-Human 管线必须跑在进程主线程；子进程同时带来崩溃隔离与 DLL 隔离。

进程协议：
  - 父 → 子：stdin 写入 "stop\\n" 请求优雅停止（worker 内 stop_flag 让捕获线程退出）
  - 子 → 父：stdout 每行 "EVENT {json}\\n" 为事件（fall/fight/status），
    其余行透传到 8771 控制台；stderr 合并进 stdout
  - 优雅停止超时 → terminate → kill；worker 死亡后其 ffmpeg 因 stdin 管道 EOF 自行退出
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import subprocess as sp
from pathlib import Path

VOICE_DIR = Path(__file__).resolve().parents[2]

_STOP_GRACE_SECONDS = 25
_TERMINATE_GRACE_SECONDS = 5


class PaddleRunner:
    def __init__(self, stream_name: str, modules: list[str] | None = None):
        self.stream_name = stream_name
        self.modules = list(modules or ["fall", "fight"])
        self._proc: sp.Popen | None = None
        self._reader: threading.Thread | None = None
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._running = False
        self._error: str | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def error(self) -> str | None:
        return self._error

    # ---- 事件订阅（SSE 广播）----
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _emit(self, event: dict) -> None:
        with self._sub_lock:
            for q in self._subscribers:
                q.put(event)

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._running:
            return
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self._proc = sp.Popen(
            [
                sys.executable, "-u", "-m",
                "assistant.paddle_service.paddle_worker",
                "--stream", self.stream_name,
                "--modules", ",".join(self.modules),
            ],
            cwd=str(VOICE_DIR),
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            stderr=sp.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self._running = True
        self._stopping = False
        self._error = None
        self._reader = threading.Thread(
            target=self._read_output, name=f"worker-reader-{self.stream_name}",
            daemon=True)
        self._reader.start()
        self._emit({"type": "status", "running": True,
                    "stream": self.stream_name})

    def request_stop(self) -> None:
        """请求停止（立即返回，回收在后台线程做，避免 HTTP 请求长时间挂起）。"""
        threading.Thread(target=self.stop, daemon=True).start()

    def stop(self) -> None:
        """同步停止：stdin 通知优雅退出 → 等待 → terminate → kill 兜底。"""
        proc = self._proc
        if proc is None or self._stopping:
            self._running = False
            return
        self._stopping = True
        # 1) 优雅停止：worker 的 capturevideo 轮询 stop_flag，排空帧队列后自行退出
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write("stop\n")
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(timeout=_STOP_GRACE_SECONDS)
        except sp.TimeoutExpired:
            # 2) terminate（worker 内 ffmpeg 随其 stdin 管道关闭而 EOF 退出）
            proc.terminate()
            try:
                proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
            except sp.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        if self._reader is not None:
            self._reader.join(timeout=3)
        self._proc = None
        self._running = False

    def _read_output(self) -> None:
        """逐行读 worker stdout：EVENT 行解析广播，其余透传控制台；EOF 表示进程退出。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line.startswith("EVENT "):
                    payload = line[len("EVENT "):]
                    try:
                        event = json.loads(payload)
                        if event.get("type") == "status" and event.get("running") is False:
                            self._running = False
                            if event.get("error"):
                                self._error = event["error"]
                        elif event.get("type") == "status" and event.get("modules"):
                            # worker 实际生效模块（口罩模型加载失败时会被降级）
                            self.modules = list(event["modules"])
                        self._emit(event)
                        continue
                    except json.JSONDecodeError:
                        pass
                print(f"[worker:{self.stream_name}] {line}", flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"[runner] 读取 {self.stream_name} worker 输出异常: {error}", flush=True)
        finally:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=10)  # 正常情况 worker 以 os._exit 秒退
                except sp.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            code = proc.returncode
            self._running = False
            if code not in (0, None) and not self._error:
                self._error = f"worker exited with code {code}"
            self._emit({"type": "status", "running": False,
                        "stream": self.stream_name, "error": self._error})
            print(f"[runner] {self.stream_name} worker 已退出 code={code}", flush=True)
