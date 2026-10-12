# -*- coding: utf-8 -*-
"""PP-Human 流式探针：拉 SRS 原始流 → 全模块分析 → 标注流推回 SRS。

链路: http://10.0.20.219:8080/live/usb1.flv
      -> MOT+KPT骨骼+ATTR属性+STGCN摔倒+ppTSM打架 (CPU)
      -> rtmp://10.0.20.219:1935/live/usb1_labeled

官方 PushStream 写死 `-f rtsp`，这里 monkey-patch 为 RTMP/FLV + 本地 ffmpeg。
用法（在 voiceInteractive 目录，且 usb1 正在推流）:
  .venv-paddle\\Scripts\\python.exe assistant\\paddle_service\\probe_stream.py
"""
import os
import sys
import types
from pathlib import Path

import yaml
import paddle

VOICE_DIR = Path(__file__).resolve().parents[2]
ROOT = VOICE_DIR / "third_party" / "PaddleDetection"
# ffmpeg 候选位置：third_party（现行）→ 项目根（旧布局）→ PATH
FFMPEG = str(next((p for p in (VOICE_DIR / "third_party" / "ffmpeg.exe",
                               VOICE_DIR / "ffmpeg.exe") if p.exists()), "ffmpeg"))
sys.path.insert(0, str(ROOT / "deploy" / "pipeline"))
sys.path.insert(0, str(ROOT / "deploy"))
os.chdir(ROOT)

SRS_HTTP = "http://10.0.20.219:8080/live/usb1.flv"
LABEL_RTMP = "rtmp://10.0.20.219:1935/live/usb1_labeled"

paddle.enable_static()

import pipeline.pipeline as P  # noqa: E402


def patched_initcmd(self, fps, width, height):
    """rawvideo(bgr24) 管道 -> libx264 -> RTMP/FLV，零延迟参数。"""
    self.command = [
        FFMPEG, "-y",
        # 不能给输入声明 -r：否则即使开 wallclock，帧间隔也会被压回声明帧率，
        # SRS 收到的时间戳不前进
        "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        # 分析帧率(CPU~1.5fps)远低于源帧率，用墙钟打 PTS
        "-use_wallclock_as_timestamps", "1", "-i", "-",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        # GOP 按帧数算：实际分析帧率~1-2fps，4 帧约 2-4 秒一个关键帧，起播快
        "-pix_fmt", "yuv420p", "-g", "4",
        "-fps_mode", "passthrough",  # 可变帧率透传墙钟时间戳，不补帧
        "-f", "flv", LABEL_RTMP,
    ]
    import subprocess as sp
    print("[push] ffmpeg ->", LABEL_RTMP, flush=True)
    self.pipe = sp.Popen(self.command, stdin=sp.PIPE)


# PipePredictor 中通过 `from pipe_utils import PushStream` 拿到的是绑定时刻的类
P.PushStream.initcmd = patched_initcmd


def make_args():
    return types.SimpleNamespace(
        output_dir="output",
        draw_center_traj=False,
        secs_interval=2,
        do_entrance_counting=False,
        do_break_in_counting=False,
        region_type="horizontal",
        region_polygon=[],
        illegal_parking_time=-1,
        pushurl="rtmp://10.0.20.219:1935/live",  # file_name=None -> 实际名 output，patch 中忽略
        device="GPU",
        run_mode="paddle",
        trt_min_shape=1,
        trt_max_shape=1280,
        trt_opt_shape=640,
        trt_calib_mode=False,
        cpu_threads=8,
        enable_mkldnn=False,
    )


def main():
    # file_name=None 时官方推流名是 output，patch 内固定改推 usb1_labeled
    with open(VOICE_DIR / "assistant" / "paddle_service" / "pphuman_eyes.yml",
              encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    p = P.PipePredictor(make_args(), cfg, is_video=True)
    p.set_file_name(None)
    print("[probe] 开始拉流分析，Ctrl+C 退出:", SRS_HTTP, flush=True)
    p.run(SRS_HTTP)


if __name__ == "__main__":
    main()
