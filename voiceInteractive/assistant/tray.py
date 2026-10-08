"""Windows 系统托盘入口。"""

from __future__ import annotations

import threading
import webbrowser
from typing import TYPE_CHECKING, Any

from .windows_notification import WindowsNotificationService

if TYPE_CHECKING:
    from .dashboard import CameraDashboard


class SystemTray:
    def __init__(self, dashboard: CameraDashboard, notifications_enabled: bool) -> None:
        import pystray
        from PIL import Image, ImageDraw

        self.dashboard = dashboard
        self.notifications_enabled = notifications_enabled
        image = Image.new("RGBA", (64, 64), (5, 31, 25, 255))
        draw = ImageDraw.Draw(image)
        green = (45, 214, 166, 255)
        draw.rounded_rectangle((3, 3, 60, 60), radius=18, outline=green, width=4)
        draw.ellipse((13, 20, 51, 44), outline=green, width=4)
        draw.ellipse((26, 25, 39, 39), fill=green)

        menu = pystray.Menu(
            pystray.MenuItem("打开老叶视觉助手", self.open_dashboard, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("测试通知", self.test_notification),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出老叶", self.exit_application),
        )
        self.icon = pystray.Icon(
            "LaoYeVoiceAssistant", image, "老叶视觉语音助手", menu
        )
        self.notifier = WindowsNotificationService(
            dashboard.url,
            fallback=self._notify_via_tray,
        )

    def start(self) -> None:
        self.icon.run_detached()

    def stop(self) -> None:
        self.notifier.stop()
        try:
            self.icon.stop()
        except Exception:
            pass

    def open_dashboard(self, *_: Any) -> None:
        webbrowser.open(self.dashboard.url)

    def _notify_via_tray(self, title: str, message: str) -> None:
        self.icon.notify(message, title)

    def test_notification(self, *_: Any) -> None:
        if not self.notifications_enabled:
            return
        self.notifier.show("老叶视觉助手", "Windows 原生后台通知工作正常")

    def exit_application(self, *_: Any) -> None:
        self.icon.stop()
        threading.Thread(
            target=self.dashboard.store.request_shutdown,
            name="tray-shutdown",
            daemon=True,
        ).start()
