from __future__ import annotations

import argparse
import argparse
import base64
import json
import sys

import cv2
import numpy as np


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenCV 本地人脸特征提取进程")
    parser.add_argument("--detector", required=True)
    parser.add_argument("--recognizer", required=True)
    parser.add_argument("--score-threshold", type=float, default=0.88)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    detector = cv2.FaceDetectorYN.create(
        args.detector,
        "",
        (320, 320),
        score_threshold=args.score_threshold,
        nms_threshold=0.3,
        top_k=30,
    )
    recognizer = cv2.FaceRecognizerSF.create(args.recognizer, "")
    print(json.dumps({"ready": True}), flush=True)

    for raw_line in sys.stdin:
        request_id = None
        try:
            request = json.loads(raw_line)
            request_id = request.get("id")
            encoded = request.get("jpeg", "")
            image_data = base64.b64decode(encoded, validate=True)
            frame = cv2.imdecode(
                np.frombuffer(image_data, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if frame is None:
                raise ValueError("无法解码JPEG画面")

            height, width = frame.shape[:2]
            detector.setInputSize((width, height))
            _, detected = detector.detect(frame)
            faces = []
            if detected is not None:
                for face in detected:
                    aligned = recognizer.alignCrop(frame, face)
                    feature = recognizer.feature(aligned).reshape(-1).astype(np.float32)
                    norm = float(np.linalg.norm(feature))
                    if norm <= 0:
                        continue
                    feature /= norm
                    x, y, box_width, box_height = [float(value) for value in face[:4]]
                    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                    faces.append(
                        {
                            "bbox": [x, y, box_width, box_height],
                            "confidence": float(face[-1]),
                            "blur": round(blur, 2),
                            "embedding": feature.tolist(),
                        }
                    )
            print(
                json.dumps(
                    {
                        "id": request_id,
                        "width": width,
                        "height": height,
                        "faces": faces,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as error:
            print(
                json.dumps(
                    {"id": request_id, "error": str(error)}, ensure_ascii=False
                ),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
