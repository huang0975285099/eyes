"""语音助手启动入口：python run.py

等价于 python -m assistant，调 assistant.main.main()。
命令行参数透传给 main()：--config、--list-cameras、--no-tray。
"""

from __future__ import annotations

import sys

from assistant.main import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已退出。")
        raise SystemExit(0)
    except Exception as error:
        print(f"\n启动失败：{error}", file=sys.stderr)
        raise SystemExit(1)
