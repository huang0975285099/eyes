# -*- coding: utf-8 -*-
"""PP-Human 单路分析 worker（独立子进程，pipeline 必须在主线程运行）。

为什么是独立进程：OpenCV(FFmpeg 后端) 的 cv2.VideoCapture 打开 HTTP-FLV 网络流
在子线程中会永久挂起（avformat_open_input 线程亲和问题），而 PP-Human 官方管线
就是主线程设计。8771 服务（paddle_runner.PaddleRunner）为本脚本拉起子进程：
  - stdin 收到 "stop\\n" 即优雅停止（stop_flag 注入 capturevideo 轮询）
  - stdout 输出 "EVENT {json}\\n" 事件行（摔倒/打架/口罩/区域入侵/状态），其余日志行由父进程透传

标注流叠加：管线把画好人体框/骨骼的帧（BGR）写进 ffmpeg stdin，本模块把 stdin 包一层
_FrameSink，在写入前叠加禁区多边形、口罩人脸框（绿=口罩/红=未戴口罩）与入侵目标高亮。

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

from .overlay import draw_overlays  # noqa: E402
from .regions import load_regions  # noqa: E402

# 主线程内的当前分析器（Result.update 拦截回调）+ 该路 labeled 推流地址
# _current 为 thread-local（initcmd/update 发生在主线程）；capturevideo 运行在管线
# 捕获子线程，读不到 thread-local，故另用进程级全局（worker 单流单进程，无并发歧义）
_current = threading.local()
_active_analyzer = None
_push_url: str | None = None
_patches_applied = False
_patches_lock = threading.Lock()


class _FrameSink:
    """包装 ffmpeg 的 stdin：写入前把叠加层画到帧上（帧为 BGR，长度 h*w*3）。

    只转发 write/flush/close/closed，其余属性（name、fileno 等）走真实流。
    叠加失败一律退回原始字节，绝不能因为画框中断推流。
    """

    def __init__(self, real, analyzer, width: int, height: int):
        self._real = real
        self._analyzer = analyzer
        self._size = width * height * 3

    def write(self, buf):
        try:
            if len(buf) == self._size:
                buf = self._analyzer.render_frame(buf)
        except Exception:  # noqa: BLE001
            pass
        return self._real.write(buf)

    def flush(self):
        try:
            return self._real.flush()
        except Exception:  # noqa: BLE001
            return None

    def close(self):
        return self._real.close()

    @property
    def closed(self):
        return getattr(self._real, "closed", True)

    def __getattr__(self, name):
        return getattr(self._real, name)


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
            if self.pipe.stdin is not None:
                analyzer.set_frame_size(width, height)
                self.pipe.stdin = _FrameSink(self.pipe.stdin, analyzer, width, height)

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
            analyzer = _active_analyzer
            if analyzer is not None:
                analyzer.on_frame(frame)  # 节流执行口罩检测（口罩 predictor 在本线程调用）
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

    FALL_COOLDOWN = 30.0   # 同一 track_id 摔倒事件告警冷却（秒，与实施方案 4.6 一致）
    FIGHT_COOLDOWN = 10.0  # 打架事件告警冷却（秒）
    MASK_INTERVAL = 3.0    # 口罩检测节流：捕获线程每 3s 跑一次（人脸模型 GPU 约 10~60ms）
    MASK_DEBOUNCE = 3      # 连续 N 个采样周期都检出未戴口罩才告警（防转脸误报）
    MASK_COOLDOWN = 30.0   # 口罩告警冷却（秒）
    INTRUSION_DRAW_TTL = 2.0  # 入侵目标高亮框保留时长（秒）

    def __init__(self, stream_name: str, stop_flag: threading.Event,
                 modules: set[str] | None = None):
        self.stream_name = stream_name
        self.pull_url = f"{SRS_HTTP}/{stream_name}.flv"
        self.stop_flag = stop_flag
        # 实际生效模块（口罩模型加载失败 / 未配禁区会从集合中移除，并通过 status 事件告知前端）
        self.modules: set[str] = set(modules or ())
        self._last_fall: dict[str, float] = {}
        self._last_fight = 0.0
        self._pushstream = None
        self._emit_lock = threading.Lock()
        self._mask_det = None
        self._last_mask_check = 0.0
        # 口罩去抖/冷却状态（on_frame 在捕获线程，draw 在主线程，共用一把锁）
        self._mask_streak = 0
        self._last_mask_alert = 0.0
        self._mask_alerting = False
        # 叠加层状态与帧尺寸（render_frame 在主线程调用，on_frame/_check_events 在其它线程）
        self._draw_lock = threading.Lock()
        self._mask_draw: dict | None = None
        self._intrusion_draw: dict[tuple, dict] = {}
        self._frame_w = 0
        self._frame_h = 0
        # 禁区（conf/regions.json）；空列表即关闭该路区域入侵
        self._regions = load_regions(stream_name)
        if "intrusion" in self.modules and not self._regions:
            self.modules.discard("intrusion")
            print(f"[worker] {stream_name} 未在 conf/regions.json 配置禁区，"
                  f"区域入侵未启用", flush=True)
        self._last_intrusion: dict[tuple, float] = {}

    def emit(self, event: dict) -> None:
        """事件以 EVENT 行写 stdout（父进程逐行解析）；加锁防与管线 print 交织。"""
        line = "EVENT " + json.dumps(event, ensure_ascii=False)
        with self._emit_lock:
            print(line, flush=True)

    def set_frame_size(self, width: int, height: int) -> None:
        self._frame_w, self._frame_h = int(width), int(height)

    def render_frame(self, buf: bytes) -> bytes:
        """在写入 ffmpeg 前叠加禁区/口罩/入侵高亮；无叠加内容时原样返回。"""
        if not self._frame_w or not self._frame_h:
            return buf
        now = time.time()
        with self._draw_lock:
            mask = self._mask_draw if self._mask_draw and self._mask_draw["until"] > now else None
            intrusions = [v for v in self._intrusion_draw.values() if v["until"] > now]
        if not self._regions and not mask and not intrusions:
            return buf
        frame = np.frombuffer(buf, dtype=np.uint8).reshape(
            self._frame_h, self._frame_w, 3).copy()
        draw_overlays(frame, self._regions, mask["faces"] if mask else (), intrusions)
        return frame.tobytes()

    def _mark_intrusion(self, key: tuple, region_name: str, box, now: float) -> None:
        with self._draw_lock:
            self._intrusion_draw[key] = {
                "box": box, "region": region_name, "until": now + self.INTRUSION_DRAW_TTL}

    def _check_intrusion(self, res, now: float) -> None:
        """MOT 检测框中心点落入禁区多边形即报（同一区域+track_id 按冷却去重）。

        mot_res['boxes'] 每行 = [track_id, 0, score, xmin, ymin, xmax, ymax]（原始帧像素）
        """
        if "intrusion" not in self.modules or not self._regions:
            return
        boxes = res.get("boxes") if hasattr(res, "get") else None
        if boxes is None or len(boxes) == 0:
            return
        for row in boxes:
            try:
                track_id = int(float(row[0]))
                x1, y1, x2, y2 = (float(row[3]), float(row[4]),
                                  float(row[5]), float(row[6]))
            except Exception:  # noqa: BLE001
                continue
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            for region in self._regions:
                if not region.contains(cx, cy):
                    continue
                key = (region.name, track_id)
                self._mark_intrusion(key, region.name, (x1, y1, x2, y2), now)
                if now - self._last_intrusion.get(key, 0.0) < region.cooldown:
                    continue
                self._last_intrusion[key] = now
                self.emit({
                    "type": "intrusion",
                    "stream": self.stream_name,
                    "region": region.name,
                    "track_id": track_id,
                    "point": [round(cx), round(cy)],
                    "timestamp": now,
                })
        if len(self._last_intrusion) > 200:  # 长期运行防字典无限增长
            for key, ts in list(self._last_intrusion.items()):
                if now - ts > 600:
                    self._last_intrusion.pop(key, None)

    def _check_events(self, name: str, res) -> None:
        now = time.time()
        try:
            if name == "mot" and res:
                self._check_intrusion(res, now)
            elif name == "skeleton_action" and res:
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

    def _build_mask_detector(self) -> None:
        """加载 PaddleHub 口罩双模型（失败仅降级，不影响摔倒/打架主管线）。"""
        if "mask" not in self.modules:
            return
        try:
            from .mask_detector import MaskDetector
            t0 = time.time()
            self._mask_det = MaskDetector()
            print(f"[worker] {self.stream_name} 口罩检测模型就绪 "
                  f"({time.time() - t0:.1f}s)", flush=True)
        except Exception as error:  # noqa: BLE001
            self.modules.discard("mask")
            print(f"[worker] {self.stream_name} 口罩模型加载失败，降级为不检测口罩: "
                  f"{error}", flush=True)

    def on_frame(self, frame_bgr) -> None:
        """管线捕获线程每帧回调（capturevideo 内）；口罩检测按 MASK_INTERVAL 节流。"""
        if self._mask_det is None or self.stop_flag.is_set():
            return
        now = time.time()
        if now - self._last_mask_check < self.MASK_INTERVAL:
            return
        self._last_mask_check = now
        try:
            faces = self._mask_det.detect(frame_bgr)
        except Exception as error:  # noqa: BLE001
            # 单次推理异常不杀主管线；持续异常时恢复后自动继续
            print(f"[worker] {self.stream_name} 口罩检测异常: {error}", flush=True)
            return
        if not faces:
            # 无人脸 = 无证据（侧脸/远景小脸）：既不复位去抖计数，也不发"全员佩戴"，避免漏报
            return
        # 叠加层：保留到下一次采样之后（+1.5s 容错），避免框在标注流上闪烁
        with self._draw_lock:
            self._mask_draw = {"faces": faces, "until": now + self.MASK_INTERVAL + 1.5}
        no_mask = [f for f in faces if f["label"] == "no_mask"]
        if no_mask:
            self._mask_streak += 1
            if self._mask_streak < self.MASK_DEBOUNCE:
                return  # 连续 MASK_DEBOUNCE 个采样周期（约 9s）都检出未戴才告警，防转脸误报
            if now - self._last_mask_alert < self.MASK_COOLDOWN:
                return
            self._last_mask_alert = now
            self._mask_alerting = True
            self.emit({
                "type": "mask",
                "stream": self.stream_name,
                "faces": len(faces),
                "no_mask": len(no_mask),
                "boxes": no_mask,   # 只回传违规人脸，事件体积更小
                "timestamp": now,
            })
            return
        # 本周期检出人脸但无人未戴：解除告警（一次性）并重置去抖计数
        self._mask_streak = 0
        if self._mask_alerting:
            self._mask_alerting = False
            self.emit({
                "type": "mask",
                "stream": self.stream_name,
                "faces": len(faces),
                "no_mask": 0,
                "boxes": [],
                "timestamp": now,
            })

    def run(self) -> None:
        global _push_url, _active_analyzer
        _push_url = f"{SRS_RTMP}/{self.stream_name}_labeled"
        _current.analyzer = self
        _active_analyzer = self
        with open(VOICE_DIR / "assistant" / "paddle_service" / "pphuman_eyes.yml",
                  encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        # predictor 只构建一次（模型常驻 GPU）；FLV 断线后复用 predictor 重入 run()，
        # 重连成本仅 VideoCapture 打开（1~3s），避免重新加载模型（~25s）
        predictor = P.PipePredictor(_make_args(), cfg, is_video=True)
        predictor.set_file_name(None)
        predictor._eyes_stop = self.stop_flag
        # 口罩模型在 PP-Human 主管线之后加载（约 1.6s）；失败自动从 modules 降级
        self._build_mask_detector()
        self.emit({"type": "status", "running": True,
                   "modules": sorted(self.modules),
                   "stream": self.stream_name, "timestamp": time.time()})
        while not self.stop_flag.is_set():
            predictor.run(self.pull_url)  # 阻塞；流结束/断开时返回
            self.close_pushstream()        # 本轮 ffmpeg 随 run 结束而废弃
            if self.stop_flag.is_set():
                break
            print("[worker] 拉流中断，2s 后重连 " + self.pull_url, flush=True)
            self.stop_flag.wait(2.0)
            # 重连前重置 PushStream 引用槽位，下轮 initcmd 会挂上新 ffmpeg
            self._pushstream = None

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
    parser.add_argument("--modules", default="fall,fight,intrusion",
                        help="逗号分隔：fall/fight/mask/intrusion")
    args = parser.parse_args()
    modules = {m.strip() for m in args.modules.split(",") if m.strip()}
    modules &= {"fall", "fight", "mask", "intrusion"}

    _apply_patches()
    stop_flag = threading.Event()
    threading.Thread(target=_watch_stdin, args=(stop_flag,),
                     name="stdin-watch", daemon=True).start()

    analyzer = PaddleAnalyzer(args.stream, stop_flag, modules)
    error = None
    try:
        print(f"[worker] {args.stream} 开始拉流分析", flush=True)
        analyzer.run()
    except Exception as e:  # noqa: BLE001
        error = str(e)
        print(f"[worker] {args.stream} 异常退出: {error}", flush=True)
    finally:
        global _active_analyzer
        _active_analyzer = None
        analyzer.close_pushstream()
        analyzer.emit({"type": "status", "running": False,
                       "stream": args.stream, "error": error,
                       "timestamp": time.time()})
        print(f"[worker] {args.stream} 已退出", flush=True)
        # paddle/CUDA 在 Windows 的 atexit 析构可能长时间挂起（进程已无实际工作），
        # stdout 事件已发完，直接以退出码强退，GPU 资源由驱动随进程回收
        sys.stdout.flush()
        sys.stderr.flush()
        exit_code = 0 if error is None else 1
        os._exit(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
