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
import time
import subprocess as sp
from pathlib import Path

VOICE_DIR = Path(__file__).resolve().parents[2]

_STOP_GRACE_SECONDS = 25
_TERMINATE_GRACE_SECONDS = 5


class PaddleRunner:
    def __init__(self, stream_name: str, modules: list[str] | None = None,
                 srs_host: str = ""):
        self.stream_name = stream_name
        self.modules = list(modules or ["fall", "fight", "intrusion"])
        self.srs_host = (srs_host or "").strip()
        # 本次请求要跑的模块：与 modules 分开保存，因为 modules 会被 worker 上报的
        # "实际生效模块"覆盖（口罩加载失败/无禁区时会被剔除），拿它比较会反复重建 worker
        self.requested_modules = list(self.modules)
        # worker 上报的实际生效模块（None = 还没收到，模型仍在加载）
        self.ready_modules: list[str] | None = None
        # worker 最近一次帧率快照（EVENT stats），供 8771 /stats 查询；不进 SSE
        self.stats: dict | None = None
        self._proc: sp.Popen | None = None
        self._reader: threading.Thread | None = None
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._running = False
        self._error: str | None = None
        self._stopping = False
        self._stopped = threading.Event()
        self._lifecycle = threading.Lock()  # 保证 stop() 只有第一个调用者执行回收

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
                "--srs-host", self.srs_host,
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
        self.requested_modules = list(self.modules)
        self._running = True
        self._stopping = False
        self._stopped.clear()   # 允许同一 runner 实例停后重启
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

    def wait_stopped(self, timeout: float | None = None) -> bool:
        """等待回收完成（供"停止后立刻重启同一路"的场景串行化）。"""
        return self._stopped.wait(timeout=timeout)

    @staticmethod
    def _reap(proc: sp.Popen, timeout: float) -> bool:
        """等待子进程退出，返回是否已回收。

        刻意不用 `Popen.wait(timeout)`：Windows 下它内部的 WaitForSingleObject
        不可被 Ctrl+C 打断，挂起的中断会延迟到 wait 返回后才抛出，而实测中这会把
        一次 3s 的等待拖到 60s 以上（中断落在异常构造路径上导致计时失效）。
        改为轮询非阻塞的 `poll()`，超时由本函数自己掌握。

        同时容忍 KeyboardInterrupt：控制台 Ctrl+C 会发给共享控制台的所有进程，
        8771 在等待 worker 退出时被打断若直接抛栈，就会漏掉回收（曾出现
        traceback + worker/ffmpeg 残留）。中断只消耗当前轮询周期，不算失败。
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                if proc.poll() is not None:
                    return True
            except KeyboardInterrupt:
                pass
            except Exception:  # noqa: BLE001  句柄失效等情况一律视为已结束
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                time.sleep(0.05)
            except KeyboardInterrupt:
                pass

    def stop(self) -> None:
        """同步停止：stdin 通知优雅退出 → 等待 → terminate → kill 兜底（幂等）。"""
        # 判定"谁是回收者"必须与置位原子完成：分两步会让两个并发调用者同时进主体
        with self._lifecycle:
            first = not self._stopping
            self._stopping = True
            proc = self._proc
        if not first:
            # 并发调用（/shutdown 线程 与 Ctrl+C 的 finally）：等回收完成再返回，
            # 避免提前返回后进程退出、worker 变孤儿
            self._stopped.wait(timeout=_STOP_GRACE_SECONDS + _TERMINATE_GRACE_SECONDS * 2)
            return
        if proc is None:
            self._running = False
            self._stopped.set()
            return
        try:
            # 1) 优雅停止：worker 的 capturevideo 轮询 stop_flag，排空帧队列后自行退出
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.write("stop\n")
                    proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            if not self._reap(proc, _STOP_GRACE_SECONDS):
                # 2) terminate（worker 内 ffmpeg 随其 stdin 管道关闭而 EOF 退出）
                try:
                    proc.terminate()
                except OSError:
                    pass
                if not self._reap(proc, _TERMINATE_GRACE_SECONDS):
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    self._reap(proc, _TERMINATE_GRACE_SECONDS)
            if self._reader is not None:
                self._reader.join(timeout=3)
            self._proc = None
            self._running = False
        finally:
            # 兜底：Windows 下 WaitForSingleObject 不可被 Ctrl+C 打断，中断会延迟到
            # wait 返回后才抛出，个别情况下会让某一步超时比预期长。这里保证 stop()
            # 返回时子进程一定已经不存在，绝不留孤儿 worker / ffmpeg
            try:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._stopped.set()

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
                            self.ready_modules = list(event["modules"])
                        elif event.get("type") == "stats":
                            # 只更新服务端快照：前端是轮询 /stats 取的，没必要每 3s 广播一次
                            self.stats = event
                            continue
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
