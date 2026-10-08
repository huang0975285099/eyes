"""本机摄像头探测工具（供 main.py --list-cameras 诊断用）。

NativeCameraMonitor（后台监控 USB 摄像头）已随 dashboard 精简移除，
此处仅保留 list_native_cameras 探测函数。
"""

from __future__ import annotations

import cv2


def list_native_cameras(max_index: int = 9) -> None:
    """Probe OpenCV indexes for the camera configuration diagnostic."""
    found = 0
    print("OpenCV 摄像头索引：")
    for index in range(max_index + 1):
        capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not capture.isOpened():
            capture.release()
            continue
        ok, frame = capture.read()
        if ok and frame is not None:
            height, width = frame.shape[:2]
            print(f"  [{index}] 可用 / {width}x{height}")
            found += 1
        capture.release()
    if not found:
        print("  未找到可读取的摄像头；请先关闭占用摄像头的浏览器或程序。")
