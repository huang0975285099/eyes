from __future__ import annotations

import argparse
import argparse
import base64
import json

import cv2
import numpy as np


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="从本地录制视频提取人脸样本")
    result.add_argument("--video", required=True)
    result.add_argument("--detector", required=True)
    result.add_argument("--recognizer", required=True)
    result.add_argument("--max-samples", type=int, default=18)
    result.add_argument("--min-size", type=int, default=80)
    result.add_argument("--min-blur", type=float, default=35.0)
    result.add_argument("--score-threshold", type=float, default=0.88)
    return result


def main() -> int:
    args = parser().parse_args()
    detector = cv2.FaceDetectorYN.create(
        args.detector, "", (320, 320), args.score_threshold, 0.3, 30
    )
    recognizer = cv2.FaceRecognizerSF.create(args.recognizer, "")
    capture = cv2.VideoCapture(args.video)
    if not capture.isOpened():
        raise RuntimeError("无法读取浏览器录制的视频")

    fps = float(capture.get(cv2.CAP_PROP_FPS))
    if not np.isfinite(fps) or fps <= 1:
        fps = 25.0
    step = max(1, round(fps * 0.4))
    frame_index = 0
    selected_vectors: list[np.ndarray] = []
    first_vector: np.ndarray | None = None
    samples = []
    statistics = {"checked": 0, "no_face": 0, "multiple_faces": 0, "quality": 0, "duplicate": 0}

    while len(samples) < max(1, min(args.max_samples, 30)):
        ok, frame = capture.read()
        if not ok:
            break
        current = frame_index
        frame_index += 1
        if current % step:
            continue
        statistics["checked"] += 1
        height, width = frame.shape[:2]
        detector.setInputSize((width, height))
        _, faces = detector.detect(frame)
        if faces is None or len(faces) == 0:
            statistics["no_face"] += 1
            continue
        if len(faces) != 1:
            statistics["multiple_faces"] += 1
            continue
        face = faces[0]
        _, _, face_width, face_height = face[:4]
        if min(face_width, face_height) < args.min_size:
            statistics["quality"] += 1
            continue
        aligned = recognizer.alignCrop(frame, face)
        gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if blur < args.min_blur:
            statistics["quality"] += 1
            continue
        vector = recognizer.feature(aligned).reshape(-1).astype(np.float32)
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            statistics["quality"] += 1
            continue
        vector /= norm
        if first_vector is not None and float(np.dot(vector, first_vector)) < 0.36:
            statistics["quality"] += 1
            continue
        if selected_vectors and max(float(np.dot(vector, item)) for item in selected_vectors) >= 0.995:
            statistics["duplicate"] += 1
            continue
        if first_vector is None:
            first_vector = vector
        selected_vectors.append(vector)
        encoded_ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not encoded_ok:
            continue
        samples.append(
            {
                "jpeg": base64.b64encode(encoded.tobytes()).decode("ascii"),
                "embedding": vector.tolist(),
                "confidence": float(face[-1]),
                "blur": round(blur, 2),
                "time_seconds": round(current / fps, 2),
            }
        )

    capture.release()
    print(json.dumps({"samples": samples, "statistics": statistics}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
