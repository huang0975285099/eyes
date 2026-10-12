# -*- coding: utf-8 -*-
"""诊断：确认飞桨标注流（usbN_labeled）到底有没有画出人体框/骨骼/属性。

排查"开始巡检后画面有人物但没有框/骨骼/属性"时用，一次跑完能切分问题层级：

1. 查 SRS API 的在线流列表 —— `usbN_labeled` 不在 = worker 没推标注流
   （飞桨 8771 没起来 / 拉不到原始流 / 模型还没加载完 / 推流失败）；
2. 抓 `usbN_labeled` 一帧存图 —— 有框 = 标注正常，问题在前端画面切换；
3. 抓 `usbN` 一帧对照，算两帧平均绝对差 —— 差异极小 = 标注确实没生效。

用法（需该路正在推流）：
    .venv-paddle\\Scripts\\python.exe assistant/paddle_service/probe_labeled_frame.py --stream usb1
结果图写入 assistant/paddle_service/probe_frames/。
"""
import argparse
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "probe_frames"
# 本机可能配了 HTTP_PROXY，SRS API 走内网必须绕过
os.environ.setdefault("no_proxy", "127.0.0.1,localhost,10.0.20.219")
# 不存在的流会让 VideoCapture 长时间阻塞：给 ffmpeg 后端设打开/读超时
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                      "rw_timeout;5000000|timeout;5000000")


def srs_streams(host: str, port: int):
    """返回 SRS 在线流名列表；连不上返回 None。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://{host}:{port}/api/v1/streams/"
    try:
        with opener.open(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001
        print(f"[SRS] 无法访问 {url} : {error}")
        return None
    return [s.get("name") for s in data.get("streams", []) if s.get("name")]


def _grab_blocking(url: str, frames: int, out: dict) -> None:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(url)  # 回退默认后端
    try:
        if not cap.isOpened():
            return
        for _ in range(frames):
            ok, frame = cap.read()
            if not ok:
                break
            out["frame"] = frame
            out["count"] += 1
    finally:
        cap.release()


def grab(url: str, frames: int, timeout: float = 20.0):
    """拉流并返回最后读到的一帧；带超时，避免不存在的流把脚本挂住。"""
    out = {"frame": None, "count": 0}
    worker = threading.Thread(target=_grab_blocking, args=(url, frames, out), daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        print(f"[!] 拉流超时（{timeout:.0f}s，流可能不存在或网络不可达）: {url}")
    return out["frame"], out["count"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", default="usb1", help="基础流名，如 usb1")
    parser.add_argument("--host", default="10.0.20.219", help="SRS 主机（与 worker 常量一致）")
    parser.add_argument("--http-port", type=int, default=8080, help="SRS HTTP-FLV 端口")
    parser.add_argument("--api-port", type=int, default=1985, help="SRS HTTP API 端口")
    parser.add_argument("--frames", type=int, default=30, help="每条流最多读多少帧")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    labeled_name = f"{args.stream}_labeled"

    names = srs_streams(args.host, args.api_port)
    if names is not None:
        print(f"[SRS] 在线流 {len(names)} 路：{', '.join(names) if names else '（空）'}")
        print(f"[SRS] {args.stream}: "
              f"{'在推' if args.stream in names else '**不在推**'}"
              f"   {labeled_name}: "
              f"{'在推' if labeled_name in names else '**不在推 -> worker 未推标注流**'}")

    raw_url = f"http://{args.host}:{args.http_port}/live/{args.stream}.flv"
    lab_url = f"http://{args.host}:{args.http_port}/live/{labeled_name}.flv"
    raw, n_raw = grab(raw_url, args.frames)
    lab, n_lab = grab(lab_url, args.frames)
    print(f"[抓帧] {args.stream}: {n_raw} 帧    {labeled_name}: {n_lab} 帧")

    if raw is not None:
        path = OUT_DIR / f"{args.stream}_raw_probe.jpg"
        cv2.imwrite(str(path), raw)
        print(f"[图] 原始流  {path}  {raw.shape}")
    elif n_raw == 0:
        print(f"[!] 拉不到原始流 {raw_url}（推流是否已开始？网络是否可达？）")

    if lab is not None:
        path = OUT_DIR / f"{labeled_name}_probe.jpg"
        cv2.imwrite(str(path), lab)
        print(f"[图] 标注流  {path}  {lab.shape}")
    elif n_lab == 0:
        print(f"[!] 拉不到标注流 {lab_url}（worker 未推流 / 标注流尚未注册）")

    if raw is not None and lab is not None and raw.shape == lab.shape:
        diff = float(np.abs(raw.astype(np.int16) - lab.astype(np.int16)).mean())
        verdict = "差异极小 -> 标注很可能没生效" if diff < 3 else "存在明显差异 -> 标注流有叠加"
        print(f"[对比] 原始帧 vs 标注帧 平均绝对差 = {diff:.2f}  ({verdict})")
    elif raw is not None and lab is not None:
        print(f"[对比] 分辨率不同: raw={raw.shape} labeled={lab.shape}")

    print("\n判读：")
    print("  · labeled 不在推        -> 查 8771 /ready /status 与 main.py 控制台的 worker 日志")
    print("  · labeled 在推但无框     -> worker 侧管线问题（模型未检出人 / 未绘制）")
    print("  · labeled 有框前端无框   -> 前端画面切换或浏览器播放问题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
