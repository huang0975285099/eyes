from __future__ import annotations

import argparse
import base64
import json
import sys

import cv2
import numpy as np
from ultralytics import YOLO


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent YOLO person detector")
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", default="0")
    parser.add_argument("--confidence", type=float, default=0.35)
    parser.add_argument("--image-size", type=int, default=640)
    return parser


def send(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def main() -> int:
    args = build_parser().parse_args()
    model = YOLO(args.model)
    if str(model.names.get(0, "")).casefold() != "person":
        raise RuntimeError("YOLO model class 0 is not person")

    # Force one small warm-up inference so the first real event is not delayed.
    model.predict(
        source=np.zeros((320, 320, 3), dtype=np.uint8),
        classes=[0],
        device=args.device,
        conf=args.confidence,
        imgsz=args.image_size,
        verbose=False,
    )
    send({"ready": True, "device": args.device, "model": args.model})

    for raw_line in sys.stdin:
        request = {}
        try:
            request = json.loads(raw_line)
            request_id = int(request["id"])
            jpeg = base64.b64decode(request["jpeg"], validate=True)
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("invalid JPEG image")
            result = model.predict(
                source=image,
                classes=[0],
                device=args.device,
                conf=args.confidence,
                imgsz=args.image_size,
                verbose=False,
            )[0]
            confidences = (
                result.boxes.conf.detach().cpu().tolist()
                if result.boxes is not None
                else []
            )
            send(
                {
                    "id": request_id,
                    "person": bool(confidences),
                    "count": len(confidences),
                    "confidence": max(confidences, default=0.0),
                }
            )
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
