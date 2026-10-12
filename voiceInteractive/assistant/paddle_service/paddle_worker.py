# -*- coding: utf-8 -*-
"""PP-Human 单路分析 worker（独立子进程，pipeline 必须在主线程运行）。

为什么是独立进程：OpenCV(FFmpeg 后端) 的 cv2.VideoCapture 打开 HTTP-FLV 网络流
在子线程中会永久挂起（avformat_open_input 线程亲和问题），而 PP-Human 官方管线
就是主线程设计。8771 服务（paddle_runner.PaddleRunner）为本脚本拉起子进程：
  - stdin 收到 "stop\\n" 即优雅停止（stop_flag 注入 capturevideo 轮询）
  - stdout 输出 "EVENT {json}\\n" 事件行（摔倒/打架/状态），其余日志行由父进程透传

用法：.venv-paddle\\Scripts\\python.exe -u -m assistant.paddle_service.paddle_worker --stream usb1
"""
import os
import sys
import time
import json
import types
import queue
import threading
import subprocess as sp
from pathlib import Path

import cv2
import yaml
import numpy as np
import paddle

VOICE_DIR = Path(__file__).resolve().parents[2]
ROOT = VOICE_DIR / "third_party" / "PaddleDetection"
# ffmpeg 候选位置：third_party（现行）→ 项目根（旧布局）→ PATH
def _resolve_ffmpeg() -> str:
    for candidate in (VOICE_DIR / "third_party" / "ffmpeg.exe",
                      VOICE_DIR / "ffmpeg.exe"):
        if candidate.exists():
            return str(candidate)
    return "ffmpeg"

FFMPEG = _resolve_ffmpeg()
SRS_HTTP = "http://10.0.20.219:8080/live"
SRS_RTMP = "rtmp://10.0.20.219:1935/live"

sys.path.insert(0, str(ROOT / "deploy" / "pipeline"))
sys.path.insert(0, str(ROOT / "deploy"))
os.chdir(ROOT)

paddle.enable_static()

import pipeline.pipeline as P  # noqa: E402
from datacollector import Result  # noqa: E402

# 主线程内的当前分析器（Result.update 拦截回调）+ 该路 labeled 推流地址
_current = threading.local()
_push_url: str | None = None
_patches_applied = False
_patches_lock = threading.Lock()


def _apply_patches():
    """全局一次性 monkey-patch（PushStream/Result.update/capturevideo）。幂等。"""
    global _patches_applied
    with _patches_lock:
        if _patches_applied:
            return
        _patches_applied = True

    def patched_initcmd(self, fps, width, height):
        """rawvideo(bgr24) 管道 -> libx264 -> RTMP/FLV，墙钟 PTS + 可变帧率透传。"""
        self.command = [
            FFMPEG, "-y",
            # 不能给输入声明 -r：否则即使开 wallclock，帧间隔也会被压回声明帧率，
            # SRS 收到的时间戳不前进
            "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-use_wallclock_as_timestamps", "1", "-i", "-",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", "4",
            "-fps_mode", "passthrough",  # 可变帧率透传墙钟时间戳，不补帧
            "-f", "flv", _push_url,
        ]
        print(f"[push] ffmpeg -> {_push_url}", flush=True)
        self.pipe = sp.Popen(self.command, stdin=sp.PIPE)
        analyzer = getattr(_current, "analyzer", None)
        if analyzer is not None:
            analyzer._pushstream = self

    P.PushStream.initcmd = patched_initcmd

    _orig_update = Result.update

    def patched_update(self, res, name):
        _orig_update(self, res, name)
        analyzer = getattr(_current, "analyzer", None)
        if analyzer is not None:
            analyzer._check_events(name, res)

    Result.update = patched_update

    def patched_capturevideo(self, capture, framequeue):
        """与官方实现一致，额外轮询 _eyes_stop 标志以支持运行中停止。"""
        stop_flag = getattr(self, "_eyes_stop", None)
        while True:
            if stop_flag is not None and stop_flag.is_set():
                try:
                    capture.release()
                except Exception:
                    pass
                return
            if framequeue.full():
                time.sleep(0.1)
                continue
            ret, frame = capture.read()
            if not ret:
                return
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            framequeue.put(frame_rgb)

    P.PipePredictor.capturevideo = patched_capturevideo


def _scalar(value, dtype=float):
    """兼容 np.ndarray / np 标量 / Python 原生数，取首个元素。"""
    try:
        arr = np.asarray(value).reshape(-1)
        return dtype(arr[0]) if arr.size else dtype(0)
    except Exception:
        try:
            return dtype(value)
        except Exception:
            return dtype(0)


def _make_args():
    # pushurl 只给基础路径：真实 labeled 地址在 patched_initcmd 中用 _push_url 注入
    return types.SimpleNamespace(
        output_dir="output",
        draw_center_traj=False,
        secs_interval=2,
        do_entrance_counting=False,
        do_break_in_counting=False,
        region_type="horizontal",
        region_polygon=[],
        illegal_parking_time=-1,
        pushurl=SRS_RTMP,
        device="GPU",
        run_mode="paddle",
        trt_min_shape=1,
        trt_max_shape=1280,
        trt_opt_shape=640,
        trt_calib_mode=False,
        cpu_threads=8,
        enable_mkldnn=False,
    )


class PaddleAnalyzer:
    """单路流分析（worker 主线程生命周期）。"""

    FALL_COOLDOWN = 10.0   # 同一 track_id 摔倒事件告警冷却（秒）
    FIGHT_COOLDOWN = 10.0  # 打架事件告警冷却（秒）

    def __init__(self, stream_name: str, stop_flag: threading.Event):
        self.stream_name = stream_name
        self.pull_url = f"{SRS_HTTP}/{stream_name}.flv"
        self.stop_flag = stop_flag
        self._last_fall: dict[str, float] = {}
        self._last_fight = 0.0
        self._pushstream = None
        self._emit_lock = threading.Lock()

    def emit(self, event: dict) -> None:
        """事件以 EVENT 行写 stdout（父进程逐行解析）；加锁防与管线 print 交织。"""
        line = "EVENT " + json.dumps(event, ensure_ascii=False)
        with self._emit_lock:
            print(line, flush=True)

    def _check_events(self, name: str, res) -> None:
        now = time.time()
        try:
            if name == "skeleton_action" and res:
                # res: [(mot_id, {'class': ndarray, 'score': ndarray}), ...]，class 0 = 摔倒
                for mot_id, action_res in res:
                    if _scalar(action_res.get("class"), int) != 0:
                        continue
                    key = str(int(mot_id))
                    if now - self._last_fall.get(key, 0.0) < self.FALL_COOLDOWN:
                        continue
                    self._last_fall[key] = now
                    self.emit({
                        "type": "fall",
                        "track_id": int(mot_id),
                        "score": round(_scalar(action_res.get("score")), 4),
                        "stream": self.stream_name,
                        "timestamp": now,
                    })
            elif name == "video_action" and res:
                # res: {'class': np.int64, 'score': np.float32}，class 1 = 打架
                if _scalar(res.get("class"), int) != 1:
                    return
                if now - self._last_fight < self.FIGHT_COOLDOWN:
                    return
                self._last_fight = now
                self.emit({
                    "type": "fight",
                    "score": round(_scalar(res.get("score")), 4),
                    "stream": self.stream_name,
                    "timestamp": now,
                })
        except Exception as error:  # noqa: BLE001
            print(f"[worker] {self.stream_name} 事件提取异常: {error}", flush=True)

    def run(self) -> None:
        global _push_url
        _push_url = f"{SRS_RTMP}/{self.stream_name}_labeled"
        _current.analyzer = self
        with open(VOICE_DIR / "assistant" / "paddle_service" / "pphuman_eyes.yml",
                  encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        predictor = P.PipePredictor(_make_args(), cfg, is_video=True)
        predictor.set_file_name(None)
        predictor._eyes_stop = self.stop_flag
        self.emit({"type": "status", "running": True,
                   "stream": self.stream_name, "timestamp": time.time()})
        predictor.run(self.pull_url)  # 阻塞至流结束或收到 stop

    def close_pushstream(self) -> None:
        """ffmpeg 推流子进程：关 stdin（触发其 EOF 退出）并兜底 terminate。"""
        pushstream = self._pushstream
        self._pushstream = None
        pipe = getattr(pushstream, "pipe", None) if pushstream else None
        if pipe is None:
            return
        try:
            if pipe.stdin and not pipe.stdin.closed:
                pipe.stdin.close()
        except Exception:
            pass
        try:
            pipe.terminate()
            pipe.wait(timeout=5)
        except Exception:
            try:
                pipe.kill()
            except Exception:
                pass


def _watch_stdin(stop_flag: threading.Event) -> None:
    """后台线程：父进程写入 "stop" 即请求停止。"""
    try:
        for line in sys.stdin:
            if line.strip() == "stop":
                stop_flag.set()
                return
    except Exception:
        pass


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", default="usb1")
    args = parser.parse_args()

    _apply_patches()
    stop_flag = threading.Event()
    threading.Thread(target=_watch_stdin, args=(stop_flag,),
                     name="stdin-watch", daemon=True).start()

    analyzer = PaddleAnalyzer(args.stream, stop_flag)
    error = None
    try:
        print(f"[worker] {args.stream} 开始拉流分析", flush=True)
        analyzer.run()
    except Exception as e:  # noqa: BLE001
        error = str(e)
        print(f"[worker] {args.stream} 异常退出: {error}", flush=True)
    finally:
        analyzer.close_pushstream()
        analyzer.emit({"type": "status", "running": False,
                       "stream": args.stream, "error": error,
                       "timestamp": time.time()})
        print(f"[worker] {args.stream} 已退出", flush=True)
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
