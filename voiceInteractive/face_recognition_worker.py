"""兼容入口；实际实现位于 ``assistant.face.recognition_worker``。"""

from assistant.face.recognition_worker import main


if __name__ == "__main__":
    raise SystemExit(main())
