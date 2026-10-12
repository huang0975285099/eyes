# -*- coding: utf-8 -*-
"""标注流叠加绘制：口罩人脸框 / 禁区多边形 / 入侵目标高亮。

帧为 **BGR**：PP-Human 的 visualize_video 开头已做 RGB->BGR，ffmpeg 按 bgr24 读取，
所以本模块所有颜色常量都按 (B, G, R) 书写。

中文标签走 PIL 渲染（cv2.putText 只支持 ASCII，画中文会得到 "??"），但**不做整帧 PIL 往返**：
标签先渲染成 RGBA 小位图并缓存（文字/颜色/字号为键），再按 alpha 贴到目标位置的小 ROI 上。
实测 720p 叠加（1 禁区 + 2 人脸框 + 1 入侵框）约 8ms → 1~2ms，不拖累 10~17fps 的推理主循环。
"""
from __future__ import annotations

import os

import cv2
import numpy as np

# BGR
COLOR_MASK_OK = (0, 200, 0)       # 绿：已戴口罩
COLOR_MASK_BAD = (0, 0, 255)      # 红：未戴口罩
COLOR_REGION = (0, 170, 255)      # 橙：禁区边界
COLOR_INTRUSION = (0, 90, 255)    # 橙红：入侵目标

_MASK_LABELS = {"no_mask": "未戴口罩", "mask": "口罩"}
_FONT_CACHE: dict[int, object] = {}
_TEXT_CACHE: dict[tuple, np.ndarray] = {}   # (text, color, size) -> RGBA 位图
_TEXT_CACHE_MAX = 400
_FONT_CANDIDATES = (
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simhei.ttf"),
)


def _font(size: int):
    font = _FONT_CACHE.get(size)
    if font is None:
        from PIL import ImageFont
        for path in _FONT_CANDIDATES:
            if os.path.exists(path):
                try:
                    font = ImageFont.truetype(path, size)
                    break
                except Exception:  # noqa: BLE001
                    font = None
        if font is None:
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:  # 老版本 Pillow 不支持 size 参数
                font = ImageFont.load_default()
        _FONT_CACHE[size] = font
    return font


def _text_bitmap(text: str, color: tuple, size: int) -> np.ndarray | None:
    """文字渲染成 RGBA 位图并缓存；PIL 不可用时返回 None。"""
    key = (text, color, size)
    bmp = _TEXT_CACHE.get(key)
    if bmp is not None:
        return bmp
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return None
    try:
        if len(_TEXT_CACHE) >= _TEXT_CACHE_MAX:  # 置信度数字会持续产生新键，简单清空即可
            _TEXT_CACHE.clear()
        font = _font(size)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
        img = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((-left, -top), text, font=font,
                                 fill=(color[2], color[1], color[0], 255))
        bmp = np.asarray(img)          # (h, w, 4)，通道顺序 R,G,B,A
        _TEXT_CACHE[key] = bmp
        return bmp
    except Exception:  # noqa: BLE001
        return None


def _blit(frame: np.ndarray, bmp: np.ndarray, x: int, y: int) -> None:
    """把 RGBA 位图按 alpha 贴到 frame（BGR）的 (x, y) 处，越界自动裁剪。"""
    fh, fw = frame.shape[:2]
    bh, bw = bmp.shape[:2]
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + bw, fw), min(y + bh, fh)
    if x1 <= x0 or y1 <= y0:
        return
    sub = bmp[y0 - y:y1 - y, x0 - x:x1 - x]
    roi = frame[y0:y1, x0:x1]
    alpha = sub[:, :, 3:4].astype(np.float32) / 255.0
    bgr = sub[:, :, :3][:, :, ::-1].astype(np.float32)   # R,G,B -> B,G,R
    roi[:] = (roi.astype(np.float32) * (1.0 - alpha) + bgr * alpha).astype(np.uint8)


def _draw_texts(frame: np.ndarray, texts: list) -> None:
    """texts: [(text, (x, y), (b, g, r), size)]，原地写回 frame。"""
    for text, (x, y), color, size in texts or ():
        bmp = _text_bitmap(text, color, size)
        if bmp is not None:
            _blit(frame, bmp, x, y)


def draw_overlays(frame: np.ndarray, regions=(), faces=(), intrusions=()) -> None:
    """在已标注的帧上叠加禁区与口罩结果（原地修改）。"""
    texts: list = []

    for region in regions or ():
        pts = np.array([region.points_int], dtype=np.int32)
        # 半透明填充只在多边形包围盒内做，避免每帧整帧 copy（720p 约省 2.7MB 拷贝）
        try:
            x, y, w, h = cv2.boundingRect(pts)
            roi = frame[y:y + h, x:x + w]
            if roi.size:
                local = pts - np.array([[x, y]], dtype=np.int32)
                layer = roi.copy()
                cv2.fillPoly(layer, local, COLOR_REGION)
                cv2.addWeighted(layer, 0.12, roi, 0.88, 0, roi)  # roi 是视图，就地写回
                cv2.polylines(roi, local, True, COLOR_REGION, 2, cv2.LINE_AA)
        except Exception:  # noqa: BLE001
            cv2.polylines(frame, pts, True, COLOR_REGION, 2, cv2.LINE_AA)
        anchor = region.points_int[0]
        texts.append((f"禁区 {region.name}", (anchor[0], max(anchor[1] - 22, 2)),
                      COLOR_REGION, 20))

    for face in faces or ():
        try:
            left, top, right, bottom = [int(v) for v in face["box"]]
        except Exception:  # noqa: BLE001
            continue
        bad = face.get("label") == "no_mask"
        color = COLOR_MASK_BAD if bad else COLOR_MASK_OK
        cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
        label = _MASK_LABELS.get(face.get("label"), face.get("label") or "")
        conf = face.get("confidence") or 0.0
        texts.append((f"{label} {float(conf):.2f}", (left, max(top - 20, 2)), color, 18))

    for item in intrusions or ():
        try:
            left, top, right, bottom = [int(v) for v in item["box"]]
        except Exception:  # noqa: BLE001
            continue
        cv2.rectangle(frame, (left, top), (right, bottom), COLOR_INTRUSION, 3)
        texts.append((f"禁区:{item.get('region', '')}", (left, max(top - 24, 2)),
                      COLOR_INTRUSION, 20))

    _draw_texts(frame, texts)
