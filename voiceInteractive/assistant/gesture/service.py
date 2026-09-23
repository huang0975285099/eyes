"""手势控制与表情调侃服务：管理 MediaPipe worker 子进程并产出动作事件。"""

from __future__ import annotations

import base64
import json
import queue
import random
import subprocess
import sys
import threading
import time

from ..paths import APP_DIR


GESTURE_LABELS = {
    "open_palm": "手掌",
    "fist": "握拳",
    "peace": "比耶",
    "thumbs_up": "点赞",
    "pointing": "指向",
}

EXPRESSION_LABELS = {
    "smile": "微笑",
    "yawn": "打哈欠",
    "sleepy": "犯困",
    "frown": "皱眉",
}

EXPRESSION_MESSAGES = {
    "smile": ["看你笑得挺开心，遇到什么好事了？", "笑一笑十年少，状态不错。"],
    "yawn": ["都打起哈欠了，累了就休息一会儿。", "困了就去眯一会儿，我帮你盯着。"],
    "sleepy": ["眼睛都快睁不开了，早点休息吧。"],
    "frown": ["怎么皱着眉，遇到烦心事了？", "眉毛都拧在一起了，放松点。"],
}


def classify_expression(expression: dict | None) -> str | None:
    """从 blendshape 系数摘要推断表情状态；无表情时返回 None。"""
    if not expression:
        return None
    smile = float(expression.get("smile", 0.0))
    jaw_open = float(expression.get("jaw_open", 0.0))
    blink = float(expression.get("blink", 0.0))
    brow_down = float(expression.get("brow_down", 0.0))
    if jaw_open >= 0.55 and blink >= 0.3:
        return "yawn"
    if smile >= 0.45:
        return "smile"
    if blink >= 0.65:
        return "sleepy"
    if brow_down >= 0.5:
        return "frown"
    return None


class MediaPipeGestureWorker:
    """gesture_worker.py 子进程的宿主：握手、收发、超时重启。"""

    def __init__(self, config) -> None:
        self.python_executable = config.gesture_python_executable
        self.hand_model_path = config.gesture_hand_model_path
        self.face_model_path = config.gesture_face_model_path
        self.min_confidence = config.gesture_min_confidence
        self.timeout_seconds = config.gesture_timeout_seconds
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
        self._responses.put({"error": "手势识别进程已经退出"})

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
        if not self.hand_model_path.is_file():
            raise RuntimeError("手势模型尚未安装，请检查 models/gesture 目录")
        worker = APP_DIR / "gesture_worker.py"
        command = [
            self.python_executable, "-u", str(worker),
            "--hand-model", str(self.hand_model_path),
            "--num-hands", "2",
            "--min-confidence", str(self.min_confidence),
        ]
        if self.face_model_path.is_file():
            command += ["--face-model", str(self.face_model_path)]
        logs = APP_DIR / "logs"
        logs.mkdir(exist_ok=True)
        self._stderr_file = (logs / "gesture-error.log").open("a", encoding="utf-8")
        self._process = subprocess.Popen(
            command,
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
            raise RuntimeError("手势模型加载超时") from error
        if not ready.get("ready"):
            message = str(ready.get("error") or "手势模型加载失败")
            self._stop()
            raise RuntimeError(message)

    def detect(self, image_bytes: bytes, include_face: bool = True) -> dict:
        with self._lock:
            self._start()
            process = self._process
            if process is None or process.stdin is None:
                raise RuntimeError("手势识别进程不可用")
            self._request_id += 1
            request_id = self._request_id
            request = {
                "id": request_id,
                "jpeg": base64.b64encode(image_bytes).decode("ascii"),
                "face": bool(include_face),
            }
            try:
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self._stop()
                raise RuntimeError("无法向手势识别进程发送图像") from error
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop()
                    raise RuntimeError("手势识别超时")
                try:
                    response = self._responses.get(timeout=remaining)
                except queue.Empty as error:
                    self._stop()
                    raise RuntimeError("手势识别超时") from error
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


class GestureService:
    """连续帧手势/表情识别：防抖确认后把动作事件交给语音主循环消费。"""

    def __init__(self, config) -> None:
        self.config = config
        self.worker = MediaPipeGestureWorker(config)
        self.enabled = bool(config.gesture_control_enabled)
        # 手势确认后的即时回调（如停播控制），在识别线程里触发，保证实时性。
        self.on_gesture_confirmed: "callable[[str], None] | None" = None
        self._lock = threading.RLock()
        self._busy = False
        self._status = "等待手势" if self.enabled else "手势控制已关闭"
        self._error = ""
        self._result_time = 0.0
        self._hands: list[dict] = []
        self._gesture: str | None = None
        self._expression: str | None = None
        self._events: queue.Queue[dict] = queue.Queue(maxsize=8)
        self._gesture_streak: tuple[str | None, int] = (None, 0)
        self._gesture_fired_at: dict[str, float] = {}
        self._expression_streak: tuple[str | None, int] = (None, 0)
        self._expression_fired_at: dict[str, float] = {}
        self._generation = 0
        self._suspended = False

    def _clear_events_locked(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def _reset_pending_locked(self, status: str) -> None:
        # Invalidate a worker response that may arrive after a switch changed.
        self._generation += 1
        self._clear_events_locked()
        self._hands = []
        self._gesture = None
        self._expression = None
        self._result_time = 0.0
        self._error = ""
        self._gesture_streak = (None, 0)
        self._expression_streak = (None, 0)
        self._status = status

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            self._reset_pending_locked(
                "等待手势" if enabled else "手势控制已关闭"
            )

    def pause(self) -> None:
        """Pause pending actions without changing the user's child switch."""
        with self._lock:
            self._suspended = True
            self._reset_pending_locked(
                "监控中心已暂停手势控制" if self.enabled else "手势控制已关闭"
            )

    def resume(self) -> None:
        with self._lock:
            self._suspended = False
            self._status = "等待手势" if self.enabled else "手势控制已关闭"

    def submit(self, image_bytes: bytes) -> bool:
        with self._lock:
            if not self.enabled or self._suspended or self._busy:
                return False
            self._busy = True
            self._status = "正在识别手势"
            generation = self._generation
        threading.Thread(
            target=self._process, args=(image_bytes, generation), daemon=True
        ).start()
        return True

    def _track_gesture(self, gesture: str | None, now: float) -> None:
        """连续 confirm_frames 帧出现同一手势时触发一次动作事件（含冷却）。"""
        previous, count = self._gesture_streak
        if gesture == previous:
            count += 1
        else:
            previous, count = gesture, 1
        self._gesture_streak = (previous, count)
        if (
            gesture is not None
            and count >= self.config.gesture_confirm_frames
            and now - self._gesture_fired_at.get(gesture, 0.0)
            >= self.config.gesture_action_cooldown_seconds
        ):
            self._gesture_fired_at[gesture] = now
            # 触发后计数归零：保持手势不动时每隔冷却期重复触发一次（长按重复），
            # 冷却期内的确认不会把计数卡在“恰好等于确认帧数”之外。
            self._gesture_streak = (gesture, 0)
            callback = self.on_gesture_confirmed
            if callback is not None:
                try:
                    callback(gesture)
                except Exception as error:
                    print(f"[手势即时回调失败] {error}", file=sys.stderr)
            self._enqueue(
                {"type": "gesture", "name": gesture, "at": now}
            )

    def _track_expression(self, expression: str | None, now: float) -> None:
        """连续 confirm_frames 帧出现同一表情时触发一次调侃事件（含冷却）。"""
        if not self.config.gesture_expression_enabled:
            self._expression_streak = (expression, 0)
            return
        previous, count = self._expression_streak
        if expression == previous:
            count += 1
        else:
            previous, count = expression, 1
        self._expression_streak = (previous, count)
        if (
            expression is not None
            and count >= self.config.gesture_confirm_frames
            and now - self._expression_fired_at.get(expression, 0.0)
            >= self.config.gesture_expression_cooldown_seconds
        ):
            self._expression_fired_at[expression] = now
            # 同手势：触发后归零，避免冷却拒绝后计数卡死在确认帧数之外。
            self._expression_streak = (expression, 0)
            message = random.choice(EXPRESSION_MESSAGES[expression])
            self._enqueue({"type": "expression", "name": expression, "message": message, "at": now})

    def _enqueue(self, event: dict) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
                self._events.put_nowait(event)
            except queue.Empty:
                pass

    def consume_event(self) -> dict | None:
        """取走一条待处理的手势/表情事件；没有时返回 None。"""
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    def _process(self, image_bytes: bytes, generation: int | None = None) -> None:
        try:
            response = self.worker.detect(
                image_bytes, include_face=self.config.gesture_expression_enabled
            )
            now = time.time()
            hands = [
                hand for hand in response.get("hands", [])
                if isinstance(hand, dict)
            ]
            frame_gesture = None
            frame_gesture_score = 0.0
            for hand in hands:
                name = hand.get("gesture")
                score = float(hand.get("gesture_score", 0.0))
                if (
                    name
                    and score >= self.config.gesture_min_confidence
                    and score > frame_gesture_score
                ):
                    frame_gesture, frame_gesture_score = name, score
            expression = classify_expression(response.get("expression"))
            with self._lock:
                if generation is not None and generation != self._generation:
                    return
                if not self.enabled or self._suspended:
                    return
                self._track_gesture(frame_gesture, now)
                self._track_expression(expression, now)
                self._hands = hands
                self._gesture = frame_gesture
                self._expression = expression
                self._result_time = now
                if frame_gesture:
                    self._status = "看到手势：" + GESTURE_LABELS.get(
                        frame_gesture, "手势"
                    )
                elif hands:
                    self._status = "检测到手部，手势置信度不足"
                elif expression:
                    self._status = f"表情：{EXPRESSION_LABELS[expression]}"
                else:
                    self._status = "没有检测到手势"
                self._error = ""
        except Exception as error:
            with self._lock:
                self._error = str(error)
                self._status = "手势识别暂时不可用"
        finally:
            with self._lock:
                self._busy = False

    def status(self) -> dict:
        with self._lock:
            age = time.time() - self._result_time if self._result_time else None
            fresh = age is not None and age <= self.config.gesture_result_max_age_seconds
            return {
                "enabled": self.enabled,
                "busy": self._busy,
                "status": self._status,
                "error": self._error,
                "result_age_seconds": round(age, 1) if age is not None else None,
                "gesture": GESTURE_LABELS.get(self._gesture) if fresh else None,
                "expression": EXPRESSION_LABELS.get(self._expression) if fresh else None,
                "hands": list(self._hands) if fresh else [],
            }

    def close(self) -> None:
        self.worker.close()
