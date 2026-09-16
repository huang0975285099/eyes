"""本地人脸人员库、识别服务与视频样本提取。"""

from .service import (
    FaceDatabase,
    FaceRecognitionService,
    OpenCVFaceExtractor,
    describe_position,
    match_face_embeddings,
)

__all__ = [
    "FaceDatabase",
    "FaceRecognitionService",
    "OpenCVFaceExtractor",
    "describe_position",
    "match_face_embeddings",
]
