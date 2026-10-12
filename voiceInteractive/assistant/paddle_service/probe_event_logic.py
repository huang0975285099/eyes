# -*- coding: utf-8 -*-
"""事件逻辑探针（不加载任何模型，秒级跑完）：验证 worker 的口罩去抖/冷却、
区域入侵判定与冷却、标注流叠加、ffmpeg stdin 包装。

用法：.venv-paddle\\Scripts\\python.exe assistant\\paddle_service\\probe_event_logic.py
（需在项目根目录执行；会 import paddle_worker，因此仍需 .venv-paddle 环境）
"""
import io
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

# 直接以脚本方式运行时，把项目根加入 sys.path 才能 import assistant.*
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from assistant.paddle_service import paddle_worker as W  # noqa: E402


class FakeDet:
    def __init__(self, seq):
        self.seq = seq
        self.i = 0

    def detect(self, frame):
        r = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return r


NO_MASK = [{"label": "no_mask", "confidence": 0.9, "box": [10, 10, 50, 60]}]
MASK = [{"label": "mask", "confidence": 0.9, "box": [10, 10, 50, 60]}]

events = []
a = W.PaddleAnalyzer("usb1", threading.Event(), {"fall", "fight", "mask", "intrusion"})
a.emit = lambda ev: events.append(ev)
print("生效模块 =", sorted(a.modules), " 禁区 =", [r.name for r in a._regions])

# ---- 1) 口罩去抖：前两次不发，第三次发；随后恢复发一次；无人脸不发 ----
a._mask_det = FakeDet([NO_MASK, NO_MASK, NO_MASK, MASK, []])
for i in range(5):
    a._last_mask_check = 0.0  # 绕过 3s 节流
    a.on_frame(np.zeros((720, 1280, 3), np.uint8))
mask_ev = [e for e in events if e["type"] == "mask"]
print("口罩事件序列 =", [e["no_mask"] for e in mask_ev], "(期望 [1, 0]：第3周期才告警，恢复发0)")

# ---- 2) 口罩冷却 30s：再连续 3 次不应重复告警 ----
a._mask_det = FakeDet([NO_MASK])
for _ in range(4):
    a._last_mask_check = 0.0
    a.on_frame(np.zeros((720, 1280, 3), np.uint8))
print("冷却期内新增口罩事件 =", len([e for e in events if e["type"] == "mask"]) - 2, "(期望 0)")

# ---- 3) 区域入侵：中心点 (800,400) 落在「柜台禁区」内，首次报、冷却内不重报 ----
boxes = np.array([[3, 0, 0.92, 700.0, 300.0, 900.0, 500.0]])
a._check_intrusion({"boxes": boxes}, time.time())
a._check_intrusion({"boxes": boxes}, time.time())
intr = [e for e in events if e["type"] == "intrusion"]
print("入侵事件数 =", len(intr), "(期望 1)  内容 =",
      json.dumps(intr[0], ensure_ascii=False) if intr else None)
# 区域外目标不应产生事件
a._check_intrusion({"boxes": np.array([[9, 0, 0.9, 10.0, 10.0, 100.0, 100.0]])}, time.time())
print("区域外误报 =", len([e for e in events if e["type"] == "intrusion"]) - 1, "(期望 0)")

# ---- 4) render_frame：长度不变、内容被改动 ----
a.set_frame_size(1280, 720)  # 真实链路由 patched_initcmd 在推流建立时设置
raw = np.full((720, 1280, 3), 30, np.uint8).tobytes()
out = a.render_frame(raw)
print("render_frame 长度一致 =", len(out) == len(raw), " 内容有叠加 =", out != raw)

# ---- 5) _FrameSink 透传 + 尺寸不符时原样写 ----
sink = W._FrameSink(io.BytesIO(), a, 1280, 720)
sink.write(raw)
short = b"\x00" * 12
sink.write(short)
print("sink 收字节 =", len(sink._real.getvalue()), "=", len(raw) + len(short))
print("note_push_frame 计数 =", a._push_frames, "(期望 2：每写一次 +1，短帧也计)")

# ---- 6) 帧率统计 _stats_loop：注入 30 帧 / 3s，应产出 fps≈10 的 stats 事件 ----
flag = threading.Event()
sa = W.PaddleAnalyzer("usb9", flag, set())
sa.STATS_INTERVAL = 0.05          # 探针里不等真实 3s
sa.emit = lambda ev: events.append(ev)
sa._push_frames = 30
sa._push_t0 = time.time() - 3.0
t = threading.Thread(target=sa._stats_loop, daemon=True)
t.start()
time.sleep(0.3)
flag.set()                         # 让循环体收尾退出
t.join(timeout=2)
stats = [e for e in events if e["type"] == "stats"]
print("stats 事件 =", len(stats) > 0, " 首条 fps =", stats[0]["fps"] if stats else None,
      "(期望 ≈10，供 8771 /stats 与前端显示)")
