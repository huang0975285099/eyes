# -*- coding: utf-8 -*-
"""PaddleHub pyramidbox_lite_server_mask 口罩检测（不依赖 paddlehub 包）。

官方模块是两阶段管线：
  1) pyramidbox_lite_server：人脸检测（PyramidBox-Lite，输出归一化人脸框）
  2) 口罩分类：对每张人脸 128x128 仿射裁剪后二分类（0=NO MASK / 1=MASK）
模型包为旧版 fluid 推理格式（目录内 __model__ + 散参数），用 paddle.inference
以目录方式直接加载，前后处理严格复刻官方 data_feed.py / processor.py：
  PaddleHub release/v2.1 modules/image/face_detection/pyramidbox_lite_server{,_mask}
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import paddle

# 模型解包目录（见 third_party/paddlehub_pkgs/ 内的原始 tar.gz）
MODEL_ROOT = Path(__file__).resolve().parents[2] / "third_party" / "paddlehub_modules"
FACE_DIR = MODEL_ROOT / "pyramidbox_lite_server" / "pyramidbox_lite_server_face_detection"
MASK_DIR = MODEL_ROOT / "pyramidbox_lite_server_mask" / "pyramidbox_lite_server_mask_model"

# 官方默认参数
_SHRINK = 0.5            # 人脸检测前缩放比例（720p → 360p）
_FACE_THRESH = 0.6       # 人脸框置信度阈值
_FACE_MEAN = np.array([104.0, 117.0, 123.0], dtype=np.float32)
_FACE_SCALE = 0.007843
_CROP_SIZE = 128
_CROP_SCALE = 1.5
_LABELS = ("no_mask", "mask")  # 与官方 label_list ['NO MASK','MASK'] 顺序一致


def _make_predictor(model_dir: Path, gpu_mem_mb: int = 200):
    if not (model_dir / "__model__").exists():
        raise FileNotFoundError(f"模型目录不存在或不完整: {model_dir}")
    config = paddle.inference.Config(str(model_dir))  # 旧版散参数目录格式
    config.disable_glog_info()
    config.enable_use_gpu(gpu_mem_mb, 0)
    return paddle.inference.create_predictor(config)


def _face_preprocess(frame_bgr: np.ndarray):
    """复刻 pyramidbox_lite_server/data_feed.py:preprocess。"""
    image = frame_bgr.copy()
    h0, w0 = image.shape[:2]
    if _SHRINK != 1:
        h1, w1 = int(h0 * _SHRINK), int(w0 * _SHRINK)
        image = cv2.resize(image, (w1, h1), cv2.INTER_NEAREST)
    else:
        h1, w1 = h0, w0
    image = np.swapaxes(np.swapaxes(image, 1, 2), 1, 0)  # HWC -> CHW
    image = image.astype(np.float32)
    image -= _FACE_MEAN[:, np.newaxis, np.newaxis]
    image *= _FACE_SCALE
    return image[np.newaxis, ...], h1, w1


def _face_crop(frame_bgr: np.ndarray, face: dict) -> np.ndarray:
    """复刻 pyramidbox_lite_server_mask/data_feed.py:crop + process_image。"""
    pts = np.array([
        face["left"], face["top"], face["right"], face["top"],
        face["left"], face["bottom"], face["right"], face["bottom"],
    ]).reshape(4, 2).astype(np.float32)
    # 官方 crop() 的角度分支只在关键点为 -1 时关闭，本场景四角均有限值：
    # alpha 由同一 y 坐标的上边两点算出（恒为 0），保留完整写法以备行为一致
    idx1, idx2 = 0, 1
    alpha = 0.0
    if pts[idx2, 0] != -1 and pts[idx2, 1] != -1 and pts[idx1, 0] != -1 and pts[idx1, 1] != -1:
        alpha = math.atan2(pts[idx2, 1] - pts[idx1, 1],
                           pts[idx2, 0] - pts[idx1, 0]) * 180.0 / math.pi
    pts[pts == -1] = np.inf
    coord_min = np.min(pts, 0)
    pts[pts == np.inf] = -1
    coord_max = np.max(pts, 0)
    c = np.array([
        coord_max[0] - (coord_max[0] - coord_min[0]) / 2.0,
        coord_max[1] - (coord_max[1] - coord_min[1]) / 2.0,
    ])
    max_wh = max((coord_max[0] - coord_min[0]) / 2.0, (coord_max[1] - coord_min[1]) / 2.0)
    m = cv2.getRotationMatrix2D((c[0], c[1]), alpha, _CROP_SIZE / (2.0 * max_wh * _CROP_SCALE))
    m[0, 2] = m[0, 2] - (c[0] - _CROP_SIZE / 2.0)
    m[1, 2] = m[1, 2] - (c[1] - _CROP_SIZE / 2.0)
    image_in = cv2.warpAffine(frame_bgr, m, (_CROP_SIZE, _CROP_SIZE))
    image_in = image_in / 256.0
    # color_normalize(mean=0.5)：仅逐通道减均值，无 std 除法
    h, w, ch = image_in.shape
    image_in = np.transpose(image_in, (2, 0, 1))
    image_in = np.subtract(
        image_in.reshape(ch, -1), np.full((ch, 1), 0.5, dtype=np.float32)).reshape(-1, h, w)
    image_in = np.transpose(image_in, (1, 2, 0))
    return image_in.astype(np.float32).transpose([2, 0, 1]).reshape(-1, 3, _CROP_SIZE, _CROP_SIZE)


class MaskDetector:
    """人脸 + 口罩双 predictor。predictor 只在固定一个线程内调用（worker 捕获线程）。"""

    def __init__(self) -> None:
        self._face_pred = _make_predictor(FACE_DIR, gpu_mem_mb=200)
        self._mask_pred = _make_predictor(MASK_DIR, gpu_mem_mb=200)
        self._face_in = self._face_pred.get_input_names()[0]
        self._face_out = self._face_pred.get_output_names()[0]
        self._mask_in = self._mask_pred.get_input_names()[0]
        self._mask_out = self._mask_pred.get_output_names()[0]

    def _run_face(self, blob: np.ndarray) -> np.ndarray:
        handle = self._face_pred.get_input_handle(self._face_in)
        handle.reshape(blob.shape)
        handle.copy_from_cpu(blob)
        self._face_pred.run()
        return self._face_pred.get_output_handle(self._face_out).copy_to_cpu()

    def _run_mask(self, blob: np.ndarray) -> np.ndarray:
        handle = self._mask_pred.get_input_handle(self._mask_in)
        handle.reshape(blob.shape)
        handle.copy_from_cpu(blob)
        self._mask_pred.run()
        return self._mask_pred.get_output_handle(self._mask_out).copy_to_cpu()

    def detect(self, frame_bgr: np.ndarray) -> list[dict]:
        """返回 [{label:'mask'|'no_mask', confidence:float, box:[l,t,r,b]}]，坐标为原图像素。"""
        if frame_bgr is None or frame_bgr.size == 0:
            return []
        h0, w0 = frame_bgr.shape[:2]
        blob, h1, w1 = _face_preprocess(frame_bgr)
        det = self._run_face(blob)
        faces: list[dict] = []
        for row in det:
            # 每行: [label, confidence, left, top, right, bottom]
            # 坐标已实测确认为**归一化 0~1**（放开阈值后跑满 89 行：left/top/right/bottom
            # 值域 ~0~1.0，偶有 1.0x 溢出），故直接乘原图宽高。这里不沿 _SHRINK 反推，
            # 避免 w1=int(w0*_SHRINK) 在奇数分辨率取整时引入偏移。
            if float(row[1]) <= _FACE_THRESH:
                continue
            face = {
                "confidence": float(row[1]),
                "left": int(max(min(w0 * float(row[2]), w0), 0.0)),
                "top": int(max(min(h0 * float(row[3]), h0), 0.0)),
                "right": int(max(min(w0 * float(row[4]), w0), 0.0)),
                "bottom": int(max(min(h0 * float(row[5]), h0), 0.0)),
            }
            # 过滤退化框
            if face["right"] - face["left"] < 8 or face["bottom"] - face["top"] < 8:
                continue
            faces.append(face)
        if not faces:
            return []
        crops = np.concatenate([_face_crop(frame_bgr, f) for f in faces], axis=0)
        scores = self._run_mask(crops)  # (N, 2)，官方直接把输出值当置信度
        results: list[dict] = []
        for face, score in zip(faces, scores):
            idx = int(np.argmax(score))
            conf = float(score[idx])
            results.append({
                "label": _LABELS[idx],
                "confidence": round(conf, 4),
                "box": [face["left"], face["top"], face["right"], face["bottom"]],
            })
        return results
