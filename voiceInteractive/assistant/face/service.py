from __future__ import annotations

import base64
from datetime import datetime
import json
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

import numpy as np

from ..paths import APP_DIR


def _clean_name(value: str) -> str:
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "", str(value)).strip()
    if not name:
        raise ValueError("姓名不能为空")
    if len(name) > 30:
        raise ValueError("姓名不能超过30个字符")
    return name


def _identity_key(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).casefold()


def _clean_aliases(values) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,，、;；]", values)
    if not isinstance(values, list):
        raise ValueError("别名格式不正确")
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = _clean_name(value) if str(value).strip() else ""
        key = _identity_key(alias)
        if alias and key not in seen:
            aliases.append(alias)
            seen.add(key)
    return aliases[:10]


class FaceDatabase:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.photos_dir = self.database_path.parent / "photos"
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path, check_same_thread=False
        )
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS people (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS face_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id TEXT NOT NULL,
                embedding BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                image_path TEXT NOT NULL,
                confidence REAL NOT NULL,
                blur REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE
            );
            """
        )
        self._connection.commit()

    def list_people(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT p.id, p.name, p.aliases_json, p.created_at, COUNT(s.id)
                FROM people p LEFT JOIN face_samples s ON s.person_id = p.id
                WHERE p.enabled = 1
                GROUP BY p.id ORDER BY p.created_at, p.name
                """
            ).fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "aliases": json.loads(row[2]),
                "created_at": row[3],
                "sample_count": int(row[4]),
            }
            for row in rows
        ]

    def add_person(self, name: str, aliases=None) -> dict:
        clean_name = _clean_name(name)
        name_key = _identity_key(clean_name)
        clean_aliases = [
            item for item in _clean_aliases(aliases or [])
            if _identity_key(item) != name_key
        ]
        person_id = uuid.uuid4().hex
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        requested_keys = {_identity_key(item) for item in [clean_name, *clean_aliases]}
        try:
            with self._lock:
                existing_rows = self._connection.execute(
                    "SELECT name, aliases_json FROM people WHERE enabled=1"
                ).fetchall()
                for existing_name, aliases_json in existing_rows:
                    if _identity_key(existing_name) == name_key:
                        raise ValueError(f"人员“{clean_name}”已经存在")
                    existing_values = [existing_name, *json.loads(aliases_json)]
                    conflict = next(
                        (
                            item
                            for item in existing_values
                            if _identity_key(item) in requested_keys
                        ),
                        None,
                    )
                    if conflict is not None:
                        raise ValueError(f"姓名或别名“{conflict}”已经被其他人员使用")
                self._connection.execute(
                    "INSERT INTO people(id,name,aliases_json,created_at) VALUES(?,?,?,?)",
                    (person_id, clean_name, json.dumps(clean_aliases, ensure_ascii=False), created_at),
                )
                self._connection.commit()
        except sqlite3.IntegrityError as error:
            raise ValueError(f"人员“{clean_name}”已经存在") from error
        return {
            "id": person_id,
            "name": clean_name,
            "aliases": clean_aliases,
            "created_at": created_at,
            "sample_count": 0,
        }

    def update_person(self, person_id: str, name: str, aliases=None) -> dict:
        clean_name = _clean_name(name)
        name_key = _identity_key(clean_name)
        clean_aliases = [
            item for item in _clean_aliases(aliases or [])
            if _identity_key(item) != name_key
        ]
        requested_keys = {_identity_key(item) for item in [clean_name, *clean_aliases]}
        with self._lock:
            row = self._connection.execute(
                "SELECT id FROM people WHERE id=? AND enabled=1", (person_id,)
            ).fetchone()
            if row is None:
                raise ValueError("没有找到要修改的人员")
            existing_rows = self._connection.execute(
                "SELECT id, name, aliases_json FROM people WHERE enabled=1"
            ).fetchall()
            for existing_id, existing_name, aliases_json in existing_rows:
                if existing_id == person_id:
                    continue
                if _identity_key(existing_name) == name_key:
                    raise ValueError(f"人员“{clean_name}”已经存在")
                existing_values = [existing_name, *json.loads(aliases_json)]
                conflict = next(
                    (
                        item
                        for item in existing_values
                        if _identity_key(item) in requested_keys
                    ),
                    None,
                )
                if conflict is not None:
                    raise ValueError(f"姓名或别名“{conflict}”已经被其他人员使用")
            self._connection.execute(
                "UPDATE people SET name=?, aliases_json=? WHERE id=?",
                (clean_name, json.dumps(clean_aliases, ensure_ascii=False), person_id),
            )
            self._connection.commit()
        person = self.get_person(person_id)
        if person is None:
            raise ValueError("没有找到要修改的人员")
        return person

    def get_person(self, person_id: str) -> dict | None:
        return next((p for p in self.list_people() if p["id"] == person_id), None)

    def add_sample(
        self,
        person_id: str,
        embedding: np.ndarray,
        image_bytes: bytes,
        confidence: float,
        blur: float,
    ) -> int:
        if self.get_person(person_id) is None:
            raise ValueError("没有找到要录入的人员")
        vector = np.asarray(embedding, dtype="<f4").reshape(-1)
        if vector.size == 0:
            raise ValueError("人脸特征为空")
        person_dir = self.photos_dir / person_id
        person_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.jpg"
        image_path = person_dir / filename
        image_path.write_bytes(image_bytes)
        try:
            with self._lock:
                cursor = self._connection.execute(
                    """
                    INSERT INTO face_samples(
                        person_id,embedding,dimensions,image_path,confidence,blur,created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        person_id,
                        vector.tobytes(),
                        int(vector.size),
                        str(image_path.relative_to(self.database_path.parent)),
                        float(confidence),
                        float(blur),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                self._connection.commit()
                return int(cursor.lastrowid)
        except Exception:
            image_path.unlink(missing_ok=True)
            raise

    def list_samples(self, person_id: str) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, confidence, blur, created_at
                FROM face_samples
                WHERE person_id=?
                ORDER BY id DESC
                """,
                (person_id,),
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "confidence": round(float(row[1]), 3),
                "blur": round(float(row[2]), 1),
                "created_at": row[3],
            }
            for row in rows
        ]

    def sample_photo_path(self, person_id: str, sample_id: int) -> Path | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT image_path FROM face_samples WHERE id=? AND person_id=?",
                (sample_id, person_id),
            ).fetchone()
        if row is None:
            return None
        photo_path = (self.database_path.parent / row[0]).resolve()
        if not photo_path.is_relative_to(self.photos_dir.resolve()):
            return None
        return photo_path

    def delete_sample(self, person_id: str, sample_id: int) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT image_path FROM face_samples WHERE id=? AND person_id=?",
                (sample_id, person_id),
            ).fetchone()
            if row is None:
                return False
            self._connection.execute("DELETE FROM face_samples WHERE id=?", (sample_id,))
            self._connection.commit()
        photo_path = (self.database_path.parent / row[0]).resolve()
        if photo_path.is_relative_to(self.photos_dir.resolve()):
            photo_path.unlink(missing_ok=True)
        return True

    def embeddings(self) -> list[tuple[str, str, np.ndarray]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT p.id,p.name,s.embedding,s.dimensions
                FROM people p JOIN face_samples s ON s.person_id=p.id
                WHERE p.enabled=1
                """
            ).fetchall()
        return [
            (row[0], row[1], np.frombuffer(row[2], dtype="<f4", count=row[3]).copy())
            for row in rows
        ]

    def resolve_person(self, text: str) -> dict | None:
        normalized = _identity_key(text)
        matches = []
        for person in self.list_people():
            candidates = [person["name"], *person["aliases"]]
            for candidate in candidates:
                key = _identity_key(candidate)
                if key and key in normalized:
                    matches.append((len(key), person))
        return max(matches, key=lambda item: item[0])[1] if matches else None

    def delete_person(self, person_id: str) -> bool:
        person = self.get_person(person_id)
        if person is None:
            return False
        with self._lock:
            self._connection.execute("DELETE FROM people WHERE id=?", (person_id,))
            self._connection.commit()
        photo_dir = (self.photos_dir / person_id).resolve()
        if photo_dir.parent == self.photos_dir.resolve() and photo_dir.is_dir():
            shutil.rmtree(photo_dir)
        return True

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class OpenCVFaceExtractor:
    def __init__(self, config) -> None:
        self.python_executable = config.face_python_executable
        self.detector_path = config.face_detector_model_path
        self.recognizer_path = config.face_recognizer_model_path
        self.timeout_seconds = config.face_timeout_seconds
        self.detector_score_threshold = config.face_detector_score_threshold
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._responses: queue.Queue[dict] = queue.Queue()
        self._stderr_file = None
        self._request_id = 0

    def _reader(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            try:
                payload = json.loads(line)
                if isinstance(payload, dict):
                    self._responses.put(payload)
            except json.JSONDecodeError:
                continue
        self._responses.put({"error": "人脸识别进程已经退出"})

    def _stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._stderr_file is not None:
            self._stderr_file.close()
            self._stderr_file = None
        self._responses = queue.Queue()

    def _start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if not self.detector_path.is_file() or not self.recognizer_path.is_file():
            raise RuntimeError("人脸模型尚未安装，请检查 models/face 目录")
        worker = Path(__file__).resolve().parent / "recognition_worker.py"
        logs = APP_DIR / "logs"
        logs.mkdir(exist_ok=True)
        self._stderr_file = (logs / "face-recognition-error.log").open("a", encoding="utf-8")
        self._process = subprocess.Popen(
            [
                self.python_executable, "-u", str(worker),
                "--detector", str(self.detector_path),
                "--recognizer", str(self.recognizer_path),
                "--score-threshold", str(self.detector_score_threshold),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._reader, args=(self._process,), daemon=True).start()
        try:
            ready = self._responses.get(timeout=self.timeout_seconds)
        except queue.Empty as error:
            self._stop()
            raise RuntimeError("人脸模型加载超时") from error
        if not ready.get("ready"):
            message = str(ready.get("error") or "人脸模型加载失败")
            self._stop()
            raise RuntimeError(message)

    def extract(self, image_bytes: bytes) -> dict:
        with self._lock:
            self._start()
            process = self._process
            if process is None or process.stdin is None:
                raise RuntimeError("人脸识别进程不可用")
            self._request_id += 1
            request_id = self._request_id
            request = {"id": request_id, "jpeg": base64.b64encode(image_bytes).decode("ascii")}
            try:
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self._stop()
                raise RuntimeError("无法向人脸识别进程发送图像") from error
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop()
                    raise RuntimeError("人脸识别超时")
                try:
                    response = self._responses.get(timeout=remaining)
                except queue.Empty as error:
                    self._stop()
                    raise RuntimeError("人脸识别超时") from error
                if response.get("id") not in {None, request_id}:
                    continue
                if response.get("error"):
                    message = str(response["error"])
                    self._stop()
                    raise RuntimeError(message)
                return response

    def close(self) -> None:
        with self._lock:
            self._stop()


def describe_position(bbox: list[float], width: int, height: int) -> str:
    x, y, box_width, box_height = bbox
    center_x = (x + box_width / 2) / max(1, width)
    horizontal = "画面左侧" if center_x < 0.36 else "画面右侧" if center_x > 0.64 else "画面中间"
    face_ratio = box_width * box_height / max(1, width * height)
    distance = "，离摄像头较近" if face_ratio >= 0.075 else "，距离摄像头较远" if face_ratio <= 0.012 else ""
    return horizontal + distance


def match_face_embeddings(
    face_embeddings: list[np.ndarray],
    samples: list[tuple[str, str, np.ndarray]],
    threshold: float,
    margin: float,
) -> list[dict]:
    """Match faces conservatively and assign each identity at most once per frame."""
    provisional: list[dict] = []
    for vector in face_embeddings:
        grouped: dict[str, dict] = {}
        for person_id, name, sample in samples:
            entry = grouped.setdefault(person_id, {"name": name, "scores": []})
            entry["scores"].append(float(np.dot(vector, sample)))
        ranked: list[tuple[str, str, float]] = []
        for person_id, entry in grouped.items():
            strongest = sorted(entry["scores"], reverse=True)[:3]
            score = float(np.mean(strongest))
            ranked.append((person_id, entry["name"], score))
        ranked.sort(key=lambda item: item[2], reverse=True)
        top_score = ranked[0][2] if ranked else 0.0
        second_score = ranked[1][2] if len(ranked) > 1 else -1.0
        accepted = bool(
            ranked
            and top_score >= threshold
            and top_score - second_score >= margin
        )
        provisional.append(
            {
                "person_id": ranked[0][0] if accepted else None,
                "name": ranked[0][1] if accepted else "未知人员",
                "known": accepted,
                "score": top_score,
            }
        )

    best_face_for_person: dict[str, int] = {}
    for index, item in enumerate(provisional):
        person_id = item["person_id"]
        if not item["known"] or person_id is None:
            continue
        previous_index = best_face_for_person.get(person_id)
        if previous_index is None or item["score"] > provisional[previous_index]["score"]:
            best_face_for_person[person_id] = index
    for index, item in enumerate(provisional):
        person_id = item["person_id"]
        if person_id is not None and best_face_for_person.get(person_id) != index:
            item["person_id"] = None
            item["name"] = "未知人员"
            item["known"] = False
    return provisional


class FaceRecognitionService:
    def __init__(self, config) -> None:
        self.config = config
        self.database = FaceDatabase(config.face_database_path)
        self.extractor = OpenCVFaceExtractor(config)
        self.enabled = bool(config.face_recognition_enabled)
        self._lock = threading.RLock()
        self._busy = False
        self._results: list[dict] = []
        self._result_time = 0.0
        self._status = "等待识别" if self.enabled else "人脸识别已关闭"
        self._error = ""
        self._recent_candidates: list[set[str]] = []

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            self._results = []
            self._result_time = 0.0
            self._error = ""
            self._recent_candidates = []
            self._status = "等待识别" if enabled else "人脸识别已关闭"

    def enroll(self, person_id: str, image_bytes: bytes) -> dict:
        extracted = self.extractor.extract(image_bytes)
        faces = extracted.get("faces", [])
        if len(faces) != 1:
            if not faces:
                raise ValueError("没有检测到清晰正脸，请正对摄像头再试")
            raise ValueError("画面中检测到多张脸，录入时请只保留本人")
        face = faces[0]
        _, _, width, height = face["bbox"]
        if min(width, height) < self.config.face_min_size:
            raise ValueError("人脸太小，请靠近摄像头再试")
        if float(face["blur"]) < self.config.face_min_blur:
            raise ValueError("画面较模糊，请保持不动再试")
        sample_id = self.database.add_sample(
            person_id,
            np.asarray(face["embedding"], dtype=np.float32),
            image_bytes,
            float(face["confidence"]),
            float(face["blur"]),
        )
        person = self.database.get_person(person_id)
        return {"sample_id": sample_id, "person": person}

    def enroll_video(self, person_id: str, video_bytes: bytes, content_type: str) -> dict:
        person = self.database.get_person(person_id)
        if person is None:
            raise ValueError("没有找到要录入的人员")
        extension = ".mp4" if "mp4" in content_type.casefold() else ".webm"
        video_dir = self.database.photos_dir / person_id / "videos"
        video_dir.mkdir(parents=True, exist_ok=True)
        video_path = video_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}{extension}"
        video_path.write_bytes(video_bytes)
        importer = Path(__file__).resolve().parent / "video_import.py"
        try:
            completed = subprocess.run(
                [
                    self.config.face_python_executable,
                    str(importer),
                    "--video", str(video_path),
                    "--detector", str(self.config.face_detector_model_path),
                    "--recognizer", str(self.config.face_recognizer_model_path),
                    "--max-samples", "18",
                    "--min-size", str(self.config.face_min_size),
                    "--min-blur", str(self.config.face_min_blur),
                    "--score-threshold", str(self.config.face_detector_score_threshold),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(30.0, self.config.face_timeout_seconds * 3),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = completed.stdout.strip().splitlines()
            payload = json.loads(output[-1]) if output else {}
            if completed.returncode != 0 or payload.get("error"):
                detail = payload.get("error") or completed.stderr.strip() or "视频解析失败"
                raise RuntimeError(str(detail))
            samples = payload.get("samples", [])
            if not samples:
                raise ValueError("视频里没有提取到合格正脸，请靠近摄像头并慢慢左右转头")
            existing = [item[2] for item in self.database.embeddings() if item[0] == person_id]
            added = 0
            for sample in samples:
                vector = np.asarray(sample["embedding"], dtype=np.float32)
                if existing and max(float(np.dot(vector, item)) for item in existing) >= 0.998:
                    continue
                image_bytes = base64.b64decode(sample["jpeg"])
                self.database.add_sample(
                    person_id,
                    vector,
                    image_bytes,
                    float(sample["confidence"]),
                    float(sample["blur"]),
                )
                existing.append(vector)
                added += 1
            if added == 0:
                raise ValueError("这段视频与现有样本过于相似，没有新增有效角度")
            return {
                "added_samples": added,
                "person": self.database.get_person(person_id),
                "statistics": payload.get("statistics", {}),
                "video_name": video_path.name,
            }
        except Exception:
            # A failed or unusable enrollment should not leave sensitive raw
            # video behind. Successful recordings remain available locally.
            video_path.unlink(missing_ok=True)
            raise

    def _recognize(self, image_bytes: bytes) -> None:
        try:
            extracted = self.extractor.extract(image_bytes)
            samples = self.database.embeddings()
            width, height = int(extracted["width"]), int(extracted["height"])
            faces = extracted.get("faces", [])
            vectors = [
                np.asarray(face["embedding"], dtype=np.float32) for face in faces
            ]
            matches = match_face_embeddings(
                vectors,
                samples,
                self.config.face_match_threshold,
                self.config.face_match_margin,
            )
            results: list[dict] = []
            current_candidates: set[str] = set()
            for face, match in zip(faces, matches):
                if match["known"]:
                    current_candidates.add(match["person_id"])
                bbox = [round(float(value), 1) for value in face["bbox"]]
                results.append(
                    {
                        "person_id": match["person_id"],
                        "name": match["name"],
                        "known": match["known"],
                        "score": round(match["score"], 3),
                        "bbox": bbox,
                        "frame_width": width,
                        "frame_height": height,
                        "position": describe_position(bbox, width, height),
                    }
                )
            with self._lock:
                self._recent_candidates.append(current_candidates)
                self._recent_candidates = self._recent_candidates[-3:]
                for item in results:
                    candidate_id = item["person_id"]
                    if not item["known"] or candidate_id is None:
                        continue
                    confirmations = sum(
                        candidate_id in candidates
                        for candidates in self._recent_candidates
                    )
                    if confirmations < 2:
                        item["known"] = False
                        item["person_id"] = None
                        item["name"] = "确认中"
                self._results = results
                self._result_time = time.time()
                known_count = sum(1 for item in results if item["known"])
                self._status = f"识别到{len(results)}张脸，其中{known_count}位已知人员" if results else "当前没有检测到人脸"
                self._error = ""
        except Exception as error:
            with self._lock:
                self._error = str(error)
                self._status = "人脸识别暂时不可用"
        finally:
            with self._lock:
                self._busy = False

    def submit(self, image_bytes: bytes) -> bool:
        with self._lock:
            if not self.enabled or self._busy:
                return False
            self._busy = True
            self._status = "正在识别人脸"
        threading.Thread(target=self._recognize, args=(image_bytes,), daemon=True).start()
        return True

    def answer_location(self, text: str) -> str | None:
        person = self.database.resolve_person(text)
        if person is None:
            return None
        with self._lock:
            results = list(self._results)
            age = time.time() - self._result_time if self._result_time else None
            enabled = self.enabled
        if not enabled:
            return "人脸识别还没有开启，请先在页面打开人脸识别开关。"
        if age is None or age > self.config.face_result_max_age_seconds:
            return "暂时没有最新的人脸画面，请确认摄像头页面正在运行。"
        match = next((item for item in results if item["person_id"] == person["id"]), None)
        if match:
            return f"{person['name']}在{match['position']}。"
        return f"当前画面没有确认到{person['name']}，可能没有正对镜头，或者不在画面内。"

    def known_people_summary(self) -> list[dict]:
        """返回最近一帧里已确认的人员（姓名与位置），供视觉问答引用。

        结果过期或没有识别到已知人员时返回空列表，调用方据此跳过提示。
        """
        with self._lock:
            age = time.time() - self._result_time if self._result_time else None
            if not self.enabled or age is None or age > self.config.face_result_max_age_seconds:
                return []
            results = list(self._results)
        return [
            {"name": item["name"], "position": item["position"]}
            for item in results
            if item["known"]
        ]

    def status(self) -> dict:
        with self._lock:
            age = time.time() - self._result_time if self._result_time else None
            return {
                "enabled": self.enabled,
                "busy": self._busy,
                "status": self._status,
                "error": self._error,
                "result_age_seconds": round(age, 1) if age is not None else None,
                "faces": list(self._results) if age is not None and age <= self.config.face_result_max_age_seconds else [],
            }

    def close(self) -> None:
        self.extractor.close()
        self.database.close()
