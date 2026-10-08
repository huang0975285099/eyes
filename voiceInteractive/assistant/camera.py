"""摄像头帧存储与会话持久化（供 dashboard 生命周期事件用）。"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


class CameraFrameStore:
    def __init__(self, conversation_log_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._snapshot_ready = threading.Condition(self._lock)
        self.shutdown_event = threading.Event()
        self.restart_event = threading.Event()
        self._frame: bytes | None = None
        self._frame_time = 0.0
        self._snapshot_request_id = 0
        self._analysis_snapshot_id = 0
        self._analysis_snapshot: bytes | None = None
        self._assistant_status = "等待摄像头连接"
        self._last_answer = ""
        self._tray_active = False
        self._presence_enabled = True
        self._presence_initialized = False
        self._person_present = False
        self._presence_checking = False
        self._presence_status = "等待建立画面基线"
        self._presence_error = ""
        self._presence_event_id = 0
        self._presence_event_time = ""
        self._presence_snapshot: bytes | None = None
        self._pending_presence_alert = False
        self._scene_broadcast_enabled = False
        self._scene_broadcast_status = "动态画面播报已关闭"
        self._scene_broadcast_error = ""
        self._scene_broadcast_request_id = 0
        self._pending_scene_broadcast: tuple[int, bytes] | None = None
        self._processing_scene_broadcast_id: int | None = None
        self._scene_broadcast_last_requested_at = 0.0
        self._scene_broadcast_event_id = 0
        self._scene_broadcast_event_time = ""
        self._scene_broadcast_snapshot: bytes | None = None
        self._last_scene_description = ""
        self._conversation_log_path = conversation_log_path
        self._conversation_revision = 0
        self._conversations: list[dict] = []
        self._active_conversation_id: str | None = None
        self._load_conversations()

    @staticmethod
    def _conversation_time() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _load_conversations(self) -> None:
        path = self._conversation_log_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            conversations = payload.get("conversations", [])
            if not isinstance(conversations, list):
                return
            self._conversations = [
                item for item in conversations[:50] if isinstance(item, dict)
            ]
            for conversation in self._conversations:
                if conversation.get("active"):
                    conversation["active"] = False
                    conversation["ended_at"] = self._conversation_time()
                    conversation["end_reason"] = "程序重启"
            self._conversation_revision = int(payload.get("revision", 0)) + 1
            self._save_conversations_locked()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._conversations = []
            self._conversation_revision = 0

    def _save_conversations_locked(self) -> None:
        path = self._conversation_log_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "revision": self._conversation_revision,
                        "conversations": self._conversations,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            # 记录写盘失败不能影响语音主循环，内存中的本次记录仍可在页面查看。
            pass

    def _append_message_locked(self, role: str, text: str) -> bool:
        clean_text = str(text).strip()
        if role not in {"user", "assistant"} or not clean_text:
            return False
        conversation = next(
            (
                item
                for item in self._conversations
                if item.get("id") == self._active_conversation_id
                and item.get("active")
            ),
            None,
        )
        if conversation is None:
            return False
        conversation.setdefault("messages", []).insert(
            0,
            {
                "id": uuid.uuid4().hex,
                "role": role,
                "text": clean_text[:10000],
                "timestamp": self._conversation_time(),
            },
        )
        conversation["messages"] = conversation["messages"][:100]
        if role == "user" and not conversation.get("title") and "老叶" not in clean_text:
            conversation["title"] = clean_text[:36]
        self._conversation_revision += 1
        self._save_conversations_locked()
        return True

    def start_conversation(
        self, wake_text: str = "叮咚叮咚", greeting: str = "我在"
    ) -> str:
        with self._lock:
            self._end_conversation_locked("重新唤醒")
            conversation_id = uuid.uuid4().hex
            now = self._conversation_time()
            self._conversations.insert(
                0,
                {
                    "id": conversation_id,
                    "title": "",
                    "started_at": now,
                    "ended_at": None,
                    "end_reason": "",
                    "active": True,
                    "messages": [],
                },
            )
            self._conversations = self._conversations[:50]
            self._active_conversation_id = conversation_id
            self._conversation_revision += 1
            self._append_message_locked("user", wake_text)
            self._conversations[0]["title"] = ""
            self._append_message_locked("assistant", greeting)
            return conversation_id

    def add_conversation_message(self, role: str, text: str) -> bool:
        with self._lock:
            return self._append_message_locked(role, text)

    def _end_conversation_locked(self, reason: str) -> bool:
        conversation = next(
            (
                item
                for item in self._conversations
                if item.get("id") == self._active_conversation_id
                and item.get("active")
            ),
            None,
        )
        if conversation is None:
            self._active_conversation_id = None
            return False
        conversation["active"] = False
        conversation["ended_at"] = self._conversation_time()
        conversation["end_reason"] = reason
        self._active_conversation_id = None
        self._conversation_revision += 1
        self._save_conversations_locked()
        return True

    def end_conversation(
        self, reason: str = "再见", assistant_message: str | None = None
    ) -> bool:
        with self._lock:
            if assistant_message:
                self._append_message_locked("assistant", assistant_message)
            return self._end_conversation_locked(reason)

    def conversations(self) -> dict:
        with self._lock:
            # JSON round-trip同时生成与内部状态无共享引用的安全快照。
            items = json.loads(json.dumps(self._conversations, ensure_ascii=False))
            return {
                "revision": self._conversation_revision,
                "active_conversation_id": self._active_conversation_id,
                "conversations": items,
            }

    def update_frame(self, frame: bytes) -> None:
        with self._lock:
            self._frame = frame
            self._frame_time = time.time()

    def latest_frame(self, max_age_seconds: float) -> bytes | None:
        with self._lock:
            if self._frame is None or time.time() - self._frame_time > max_age_seconds:
                return None
            return self._frame

    def analysis_snapshot(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._analysis_snapshot_id, self._analysis_snapshot

    def record_analysis_snapshot(self, frame: bytes) -> int:
        """Expose the exact native frame used for a spoken visual answer."""
        with self._snapshot_ready:
            self._snapshot_request_id += 1
            self._analysis_snapshot_id = self._snapshot_request_id
            self._analysis_snapshot = frame
            self._snapshot_ready.notify_all()
            return self._analysis_snapshot_id

    def begin_presence_check(self) -> bool:
        with self._lock:
            if not self._presence_enabled or self._presence_checking:
                return False
            self._presence_checking = True
            self._presence_status = "正在确认画面是否有人"
            self._presence_error = ""
            return True

    def finish_presence_check(
        self, person_present: bool, frame: bytes, reason: str
    ) -> bool:
        with self._lock:
            if not self._presence_enabled:
                self._presence_checking = False
                return False
            was_initialized = self._presence_initialized
            previous = self._person_present
            self._presence_initialized = True
            self._person_present = person_present
            self._presence_checking = False
            self._presence_error = ""
            triggered = (
                reason != "baseline"
                and was_initialized
                and not previous
                and person_present
            )
            if triggered:
                self._presence_event_id += 1
                self._presence_event_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._presence_snapshot = frame
                self._pending_presence_alert = True
                self._presence_status = "检测到有人进入画面"
            else:
                self._presence_status = "画面中有人" if person_present else "画面中无人"
            return triggered

    def fail_presence_check(self, error: str) -> None:
        with self._lock:
            self._presence_checking = False
            if not self._presence_enabled:
                return
            self._presence_error = error
            self._presence_status = "人物检测暂时不可用"

    def set_presence_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._presence_enabled = enabled
            self._presence_initialized = False
            self._person_present = False
            self._presence_checking = False
            self._presence_error = ""
            self._pending_presence_alert = False
            self._presence_status = (
                "等待建立画面基线" if enabled else "动态人物监测已关闭"
            )

    def presence_snapshot(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._presence_event_id, self._presence_snapshot

    def consume_presence_alert(self) -> int | None:
        with self._lock:
            if not self._pending_presence_alert:
                return None
            self._pending_presence_alert = False
            return self._presence_event_id

    def set_scene_broadcast_enabled(self, enabled: bool) -> None:
        with self._lock:
            if self._scene_broadcast_enabled == enabled:
                return
            self._scene_broadcast_enabled = enabled
            self._pending_scene_broadcast = None
            self._processing_scene_broadcast_id = None
            self._scene_broadcast_last_requested_at = 0.0
            self._scene_broadcast_error = ""
            self._scene_broadcast_status = (
                "等待画面变化" if enabled else "动态画面播报已关闭"
            )

    def scene_broadcast_enabled(self) -> bool:
        with self._lock:
            return self._scene_broadcast_enabled

    def submit_scene_broadcast(
        self, frame: bytes, cooldown_seconds: float
    ) -> bool:
        with self._lock:
            now = time.monotonic()
            busy = (
                self._pending_scene_broadcast is not None
                or self._processing_scene_broadcast_id is not None
            )
            cooling_down = (
                now - self._scene_broadcast_last_requested_at < cooldown_seconds
            )
            if not self._scene_broadcast_enabled or busy or cooling_down:
                return False
            self._scene_broadcast_request_id += 1
            request_id = self._scene_broadcast_request_id
            self._pending_scene_broadcast = (request_id, frame)
            self._scene_broadcast_last_requested_at = now
            self._scene_broadcast_error = ""
            self._scene_broadcast_status = "已捕获变化画面，等待分析"
            return True

    def consume_scene_broadcast(self) -> tuple[int, bytes] | None:
        with self._lock:
            if not self._scene_broadcast_enabled:
                self._pending_scene_broadcast = None
                return None
            pending = self._pending_scene_broadcast
            if pending is None:
                return None
            self._pending_scene_broadcast = None
            self._processing_scene_broadcast_id = pending[0]
            self._scene_broadcast_status = "正在分析变化画面"
            return pending

    def finish_scene_broadcast(
        self, request_id: int, frame: bytes, description: str
    ) -> bool:
        with self._lock:
            if (
                not self._scene_broadcast_enabled
                or self._processing_scene_broadcast_id != request_id
            ):
                return False
            self._processing_scene_broadcast_id = None
            self._scene_broadcast_event_id = request_id
            self._scene_broadcast_event_time = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            self._scene_broadcast_snapshot = frame
            self._last_scene_description = description
            self._scene_broadcast_error = ""
            self._scene_broadcast_status = "画面内容已播报"
            return True

    def fail_scene_broadcast(self, request_id: int, error: str) -> None:
        with self._lock:
            if (
                not self._scene_broadcast_enabled
                or self._processing_scene_broadcast_id != request_id
            ):
                return
            self._processing_scene_broadcast_id = None
            self._scene_broadcast_error = error
            self._scene_broadcast_status = "画面播报暂时不可用"

    def scene_broadcast_snapshot(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._scene_broadcast_event_id, self._scene_broadcast_snapshot

    def set_assistant_status(self, status: str, answer: str | None = None) -> None:
        with self._lock:
            self._assistant_status = status
            if answer is not None:
                self._last_answer = answer

    def set_tray_active(self, active: bool) -> None:
        with self._lock:
            self._tray_active = active

    def status(self) -> dict:
        with self._lock:
            age = time.time() - self._frame_time if self._frame else None
            return {
                "camera_ready": self._frame is not None and age is not None and age < 4.0,
                "frame_age_seconds": round(age, 1) if age is not None else None,
                "snapshot_request_id": self._snapshot_request_id,
                "analysis_snapshot_id": self._analysis_snapshot_id,
                "presence_enabled": self._presence_enabled,
                "presence_initialized": self._presence_initialized,
                "person_present": self._person_present,
                "presence_checking": self._presence_checking,
                "presence_status": self._presence_status,
                "presence_error": self._presence_error,
                "presence_event_id": self._presence_event_id,
                "presence_event_time": self._presence_event_time,
                "scene_broadcast_enabled": self._scene_broadcast_enabled,
                "scene_broadcast_busy": (
                    self._pending_scene_broadcast is not None
                    or self._processing_scene_broadcast_id is not None
                ),
                "scene_broadcast_status": self._scene_broadcast_status,
                "scene_broadcast_error": self._scene_broadcast_error,
                "scene_broadcast_event_id": self._scene_broadcast_event_id,
                "scene_broadcast_event_time": self._scene_broadcast_event_time,
                "last_scene_description": self._last_scene_description,
                "assistant_status": self._assistant_status,
                "last_answer": self._last_answer,
                "tray_active": self._tray_active,
                "conversation_revision": self._conversation_revision,
                "active_conversation_id": self._active_conversation_id,
            }

    def request_shutdown(self) -> None:
        with self._snapshot_ready:
            self._assistant_status = "正在关闭语音助手"
            self.shutdown_event.set()
            self._snapshot_ready.notify_all()

    def request_restart(self) -> None:
        with self._snapshot_ready:
            self._assistant_status = "正在重启语音助手"
            self.restart_event.set()
            self.shutdown_event.set()
            self._snapshot_ready.notify_all()

