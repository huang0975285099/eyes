"""常驻 MediaPipe 手势/表情检测子进程。

主程序通过 stdin 逐行发送 {"id": N, "jpeg": base64}，本进程对每帧运行
HandLandmarker（21 关键点 → 几何手势分类）与 FaceLandmarker（52 blendshape
→ 表情系数摘要），并以 {"id": N, "hands": [...], "expression": {...}} 回复。
运行在外部 Python（需 mediapipe >= 1.0、cv2、numpy）。
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python import vision


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent MediaPipe gesture worker")
    parser.add_argument("--hand-model", required=True)
    parser.add_argument("--face-model", default="")
    parser.add_argument("--num-hands", type=int, default=2)
    parser.add_argument("--min-confidence", type=float, default=0.5)
    return parser


def send(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True), flush=True)


# --- 手势几何分类（21 归一化关键点，y 轴向下） ---

WRIST = 0
THUMB_MCP, THUMB_IP, THUMB_TIP = 2, 3, 4
INDEX_MCP, INDEX_TIP = 5, 8
MIDDLE_MCP, MIDDLE_TIP = 9, 12
RING_MCP, RING_TIP = 13, 16
PINKY_MCP, PINKY_TIP = 17, 20

EXTEND_RATIO = 1.55
GESTURES = ("open_palm", "fist", "peace", "thumbs_up", "pointing")


def _dist(a, b) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _extension_margin(tip, mcp, wrist) -> float:
    """伸展裕度：0=完全弯曲，1=明显伸直。阈值 1.55 附近为过渡区。"""
    base = max(_dist(mcp, wrist), 1e-6)
    ratio = _dist(tip, wrist) / base
    return max(0.0, min(1.0, (ratio - EXTEND_RATIO) / 0.6 + 0.5))


def classify_hand(points) -> tuple[str | None, float]:
    """返回 (手势名, 置信度)。无法归入已知手势时返回 (None, 0)。"""
    wrist = points[WRIST]
    fingers = [
        _extension_margin(points[tip], points[mcp], wrist)
        for tip, mcp in (
            (INDEX_TIP, INDEX_MCP),
            (MIDDLE_TIP, MIDDLE_MCP),
            (RING_TIP, RING_MCP),
            (PINKY_TIP, PINKY_MCP),
        )
    ]
    extended = [margin > 0.5 for margin in fingers]
    thumb_tip, thumb_ip = points[THUMB_TIP], points[THUMB_IP]
    thumb_margin = 0.5 + (
        _dist(thumb_tip, points[INDEX_MCP]) - 1.1 * _dist(points[THUMB_MCP], points[INDEX_MCP])
    ) / (0.5 * max(_dist(thumb_ip, points[INDEX_MCP]), 1e-6))
    thumb_margin = max(0.0, min(1.0, thumb_margin))
    thumb_up = thumb_tip.y < wrist.y - 0.02

    margins = [thumb_margin, *fingers]
    if all(extended):
        name = "open_palm"
    elif not any(extended):
        if thumb_margin > 0.5 and thumb_up:
            name = "thumbs_up"
        else:
            name = "fist"
    elif extended[0] and extended[1] and not extended[2] and not extended[3]:
        name = "peace"
    elif extended[0] and not extended[1] and not extended[2] and not extended[3]:
        name = "pointing"
    else:
        return None, 0.0
    score = sum(1.0 - abs(margin - (1.0 if flag else 0.0)) for margin, flag in
                zip(margins, [thumb_margin > 0.5, *extended])) / len(margins)
    return name, round(max(0.0, min(1.0, score)), 3)


def hand_bbox(points, width: int, height: int) -> list[float]:
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    x1, x2 = min(xs), max(xs)
    y1, y2 = min(ys), max(ys)
    return [
        round(x1 * width, 1),
        round(y1 * height, 1),
        round((x2 - x1) * width, 1),
        round((y2 - y1) * height, 1),
    ]


# --- 表情 blendshape 摘要 ---

BLENDSHAPE_KEYS = {
    "mouthSmileLeft": "smile_l",
    "mouthSmileRight": "smile_r",
    "jawOpen": "jaw_open",
    "eyeBlinkLeft": "blink_l",
    "eyeBlinkRight": "blink_r",
    "browDownLeft": "brow_down_l",
    "browDownRight": "brow_down_r",
    "browInnerUp": "brow_inner_up",
    "mouthPucker": "pucker",
}


def summarize_expression(categories) -> dict | None:
    raw = {category.category_name: float(category.score) for category in categories}
    if not any(key in raw for key in BLENDSHAPE_KEYS):
        return None
    def avg(left: str, right: str) -> float:
        return (raw.get(left, 0.0) + raw.get(right, 0.0)) / 2
    return {
        "smile": round(avg("mouthSmileLeft", "mouthSmileRight"), 3),
        "jaw_open": round(raw.get("jawOpen", 0.0), 3),
        "blink": round(avg("eyeBlinkLeft", "eyeBlinkRight"), 3),
        "brow_down": round(avg("browDownLeft", "browDownRight"), 3),
        "brow_inner_up": round(raw.get("browInnerUp", 0.0), 3),
        "pucker": round(raw.get("mouthPucker", 0.0), 3),
    }


def main() -> int:
    args = build_parser().parse_args()
    hand_landmarker = vision.HandLandmarker.create_from_options(
        vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=args.hand_model),
            running_mode=vision.RunningMode.IMAGE,
            num_hands=max(1, args.num_hands),
            min_hand_detection_confidence=args.min_confidence,
            min_hand_presence_confidence=args.min_confidence,
        )
    )
    face_landmarker = None
    if args.face_model:
        face_landmarker = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=args.face_model),
                running_mode=vision.RunningMode.IMAGE,
                output_face_blendshapes=True,
            )
        )

    # 预热一次，避免首个真实请求承担图编译开销。
    warmup = np.zeros((320, 320, 3), dtype=np.uint8)
    hand_landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=warmup))
    if face_landmarker is not None:
        face_landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=warmup))
    send({"ready": True, "hand_model": args.hand_model})

    for raw_line in sys.stdin:
        request = {}
        try:
            request = json.loads(raw_line)
            request_id = int(request["id"])
            jpeg = base64.b64decode(request["jpeg"], validate=True)
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("invalid JPEG image")
            height, width = image.shape[:2]
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

            hands = []
            hand_result = hand_landmarker.detect(mp_image)
            for points, handedness in zip(
                hand_result.hand_landmarks, hand_result.handedness
            ):
                if not handedness:
                    continue
                category = handedness[0]
                gesture, gesture_score = classify_hand(points)
                hands.append(
                    {
                        "handedness": category.category_name,
                        "score": round(float(category.score), 3),
                        "gesture": gesture,
                        "gesture_score": gesture_score,
                        "bbox": hand_bbox(points, width, height),
                    }
                )

            expression = None
            if face_landmarker is not None and request.get("face", True):
                face_result = face_landmarker.detect(mp_image)
                if face_result.face_blendshapes:
                    expression = summarize_expression(face_result.face_blendshapes[0])

            send({"id": request_id, "hands": hands, "expression": expression})
        except Exception as error:
            send(
                {
                    "id": request.get("id") if isinstance(request, dict) else None,
                    "error": str(error),
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
