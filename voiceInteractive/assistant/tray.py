"""Windows 系统托盘入口。"""

from __future__ import annotations

import threading
import webbrowser
from typing import TYPE_CHECKING, Any

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
            pystray.MenuItem(
                "后台摄像头接管",
                self.toggle_native_camera,
                checked=lambda _: self.dashboard.native_camera.enabled,
            ),
            pystray.MenuItem(
                "动态人物监测",
                self.toggle_person_monitor,
                checked=lambda _: self.dashboard.presence_monitor.enabled,
            ),
            pystray.MenuItem(
                "动态画面播报",
                self.toggle_scene_broadcast,
                checked=lambda _: self.dashboard.store.scene_broadcast_enabled(),
            ),
            pystray.MenuItem(
                "人脸识别",
                self.toggle_face_recognition,
                checked=lambda _: self.dashboard.face_service.enabled,
            ),
            pystray.MenuItem("测试通知", self.test_notification),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出老叶", self.exit_application),
        )
        self.icon = pystray.Icon(
            "LaoYeVoiceAssistant", image, "老叶视觉语音助手", menu
        )
        self.dashboard.native_camera.add_event_listener(self.notify_motion)

    def start(self) -> None:
        self.icon.run_detached()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass

    def open_dashboard(self, *_: Any) -> None:
        webbrowser.open(self.dashboard.url)

    def _refresh(self, message: str) -> None:
        self.icon.update_menu()
        if self.notifications_enabled:
            try:
                self.icon.notify(message, "老叶视觉助手")
            except Exception:
                pass

    def toggle_native_camera(self, *_: Any) -> None:
        enabled = not self.dashboard.native_camera.enabled
        self.dashboard.native_camera.set_enabled(enabled)
        self._refresh("后台摄像头接管已开启" if enabled else "后台摄像头接管已关闭")

    def toggle_person_monitor(self, *_: Any) -> None:
        enabled = not self.dashboard.presence_monitor.enabled
        self.dashboard.presence_monitor.set_enabled(enabled)
        self._refresh("动态人物监测已开启" if enabled else "动态人物监测已关闭")

    def toggle_scene_broadcast(self, *_: Any) -> None:
        enabled = not self.dashboard.store.scene_broadcast_enabled()
        self.dashboard.store.set_scene_broadcast_enabled(enabled)
        if enabled:
            frame = self.dashboard.store.latest_frame(
                self.dashboard.config.camera_frame_max_age_seconds
            )
            if frame is not None:
                self.dashboard.store.submit_scene_broadcast(
                    frame, self.dashboard.config.scene_broadcast_cooldown_seconds
                )
        self._refresh("动态画面播报已开启" if enabled else "动态画面播报已关闭")

    def toggle_face_recognition(self, *_: Any) -> None:
        enabled = not self.dashboard.face_service.enabled
        self.dashboard.face_service.set_enabled(enabled)
        self._refresh("人脸识别已开启" if enabled else "人脸识别已关闭")

    def notify_motion(self, event: dict) -> None:
        if not self.notifications_enabled:
            return
        score = float(event.get("motion_score", 0.0))
        try:
            self.icon.notify(
                f"检测到持续画面变化，变化面积 {score:.2f}%",
                "老叶视觉助手",
            )
        except Exception:
            pass

    def test_notification(self, *_: Any) -> None:
        if not self.notifications_enabled:
            return
        try:
            self.icon.notify("托盘与后台通知工作正常", "老叶视觉助手")
        except Exception:
            pass

    def exit_application(self, *_: Any) -> None:
        self.icon.stop()
        threading.Thread(
            target=self.dashboard.store.request_shutdown,
            name="tray-shutdown",
            daemon=True,
        ).start()
