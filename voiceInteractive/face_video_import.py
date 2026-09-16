"""兼容入口；实际实现位于 ``assistant.face.video_import``。"""

from assistant.face.video_import import main


if __name__ == "__main__":
    raise SystemExit(main())
