"""应用生命周期事件（关机/重启信号），供 dashboard 与 tray 协调退出。"""

from __future__ import annotations

import threading


class AppLifecycle:
    """跨线程的生命周期事件总线：dashboard 等待 shutdown，tray 触发 shutdown/restart。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.restart_event = threading.Event()

    def request_shutdown(self) -> None:
        with self._lock:
            self.shutdown_event.set()

    def request_restart(self) -> None:
        with self._lock:
            self.restart_event.set()
            self.shutdown_event.set()
