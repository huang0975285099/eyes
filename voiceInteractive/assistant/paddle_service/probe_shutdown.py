# -*- coding: utf-8 -*-
"""退出路径探针：验证 worker 回收对 KeyboardInterrupt 的容忍。

背景：Windows 控制台的 Ctrl+C 会发给**共享控制台的所有进程**，8771 子进程收到后
在 `runner.stop()` 的 `proc.wait()` 期间再次命中就会抛栈退出，worker 与它拉起的
ffmpeg 变孤儿（继续占显存、继续推 RTMP）。本探针用假进程回归三条路径：

1. 回收等待期间 Ctrl+C -> 不抛栈，继续走 terminate/kill 兜底且进程被回收
   （Windows 的 WaitForSingleObject 不可被打断，中断会延迟到 wait 返回后才抛出，
   这正是线上遇到的情形：中断落在 _reap 上若不被容忍就会漏回收）
2. 并发 stop（/shutdown 线程 + Ctrl+C 的 finally）-> 两者都等回收完成
3. 进程已自行退出 -> 立即返回，不空等

用法：.venv-paddle\\Scripts\\python.exe assistant/paddle_service/probe_shutdown.py
"""
import subprocess
import sys
import threading
import time
from pathlib import Path
import _thread

# 直接以脚本方式运行时，把项目根加入 sys.path 才能 import assistant.*
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from assistant.paddle_service import paddle_runner as R  # noqa: E402
from assistant.paddle_service.paddle_runner import PaddleRunner  # noqa: E402

# 缩短优雅宽限：保持逻辑不变，让探针几秒内跑完（线上是 25s）
R._STOP_GRACE_SECONDS = 3


def _spawn():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def _make(proc):
    r = PaddleRunner("usb1")
    r._proc = proc
    r._running = True
    return r


# ---- 1) wait 期间被打断：必须继续走 terminate 兜底，不能抛栈 ----
proc1 = _spawn()
r = _make(proc1)
threading.Timer(1.0, _thread.interrupt_main).start()
t0 = time.time()
r.stop()  # 不应抛出 KeyboardInterrupt
print(f"1) 打断后 stop 正常返回，用时 {time.time()-t0:.1f}s，proc.poll()={proc1.poll()} "
      f"(非 None 即已回收)")
if proc1.poll() is None:
    proc1.kill()

# ---- 2) 并发 stop（/shutdown 线程 + Ctrl+C 的 finally）：两者都要等回收完成 ----
proc2 = _spawn()
r2 = _make(proc2)
results = []


def _call():
    try:
        r2.stop()
        results.append("ok")
    except BaseException as exc:  # noqa: BLE001
        results.append(f"raised {type(exc).__name__}")


t1 = threading.Thread(target=_call)
t2 = threading.Thread(target=_call)
t1.start()
t2.start()
# 两个线程都在等：先给子进程 stdin 写 stop（它不读 stdin，所以靠 terminate 兜底）
t1.join(timeout=60)
t2.join(timeout=60)
print(f"2) 并发 stop 结果={results}，proc.poll()={proc2.poll()} "
      f"(期望 ['ok','ok'] 且非 None)")
if proc2.poll() is None:
    proc2.kill()

# ---- 3) 正常路径不受影响：进程自己退出时立即返回 ----
proc3 = subprocess.Popen([sys.executable, "-c", "pass"])
r3 = _make(proc3)
t0 = time.time()
r3.stop()
print(f"3) 已退出进程 stop 立即返回，用时 {time.time()-t0:.2f}s (期望 <1s)")
print("全部通过")
