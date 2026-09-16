"""Windows 10/11 原生通知队列。"""

from __future__ import annotations

import queue
import threading
import webbrowser
from collections import deque
from collections.abc import Callable


class WindowsNotificationService:
    """在专用线程发送系统 Toast，避免摄像头线程被通知 API 阻塞。"""

    def __init__(
        self,
        dashboard_url: str,
        fallback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.dashboard_url = dashboard_url
        self.fallback = fallback
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=20)
        self._recent_toasts: deque[object] = deque(maxlen=20)
        self._thread = threading.Thread(
            target=self._run,
            name="windows-notification-service",
            daemon=True,
        )
        self._thread.start()

    def show(self, title: str, message: str) -> None:
        item = (title, message)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass

    def stop(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=2)

    def _fallback(self, title: str, message: str) -> None:
        if self.fallback is None:
            return
        try:
            self.fallback(title, message)
        except Exception:
            pass

    def _run(self) -> None:
        try:
            from windows_toasts import Toast, WindowsToaster

            toaster = WindowsToaster("老叶视觉助手")
        except Exception:
            toaster = None
            Toast = None

        while True:
            item = self._queue.get()
            if item is None:
                return
            title, message = item
            if toaster is None or Toast is None:
                self._fallback(title, message)
                continue
            try:
                toast = Toast()
                toast.text_fields = [title, message]
                toast.on_activated = lambda _event: webbrowser.open(
                    self.dashboard_url
                )
                toaster.show_toast(toast)
                # 保留对象，确保 Windows 回调在通知仍可点击时不会被回收。
                self._recent_toasts.append(toast)
            except Exception:
                self._fallback(title, message)
