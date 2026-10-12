"""命令行入口与启动流程。"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config
from .dashboard import CameraDashboard
from .llm import OllamaClient
from .native_camera import list_native_cameras
from .paths import APP_DIR
from .platform_utils import configure_windows_console
from .tray import SystemTray

# 飞桨服务使用独立 venv（paddle 与 torch DLL 冲突，必须物理隔离）
_PADDLE_PYTHON = APP_DIR / ".venv-paddle" / "Scripts" / "python.exe"
_PADDLE_PORT = 8771
_PADDLE_STOP_GRACE_SECONDS = 40.0


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _stop_paddle_service(process: subprocess.Popen) -> None:
    """先让 8771 自行停掉所有 worker，再兜底 terminate。

    直接 terminate() 在 Windows 是 TerminateProcess，paddle_server 的 finally
    不会执行，它拉起的 paddle_worker 与 ffmpeg 会变孤儿：继续占显存并推 RTMP
    标注流。故先走 HTTP /shutdown 触发其正常退出路径。
    """
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{_PADDLE_PORT}/shutdown",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Local-Shutdown": "1",
            },
            method="POST",
        )
        # 显式禁用代理：环境里的 HTTP_PROXY 会把本机请求也转发出去
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5):
            pass
        process.wait(timeout=_PADDLE_STOP_GRACE_SECONDS)
        return
    # KeyboardInterrupt 不是 Exception 的子类：连按 Ctrl+C 时必须显式兜住，
    # 否则 8771 不会被 terminate，worker 与 ffmpeg 变孤儿
    except (Exception, KeyboardInterrupt) as error:
        print(f"飞桨分析服务优雅退出失败，转为强制结束：{error}", file=sys.stderr)
    try:
        process.terminate()
        try:
            process.wait(timeout=10)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            process.kill()
    except OSError:
        pass  # 已退出


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MM101S USB 摄像头语音交互")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径"
    )
    parser.add_argument(
        "--list-cameras", action="store_true", help="探测 OpenCV 摄像头索引"
    )
    parser.add_argument("--no-tray", action="store_true", help="不显示 Windows 托盘图标")
    return parser


def main() -> int:
    configure_windows_console()
    args = build_parser().parse_args()
    if args.list_cameras:
        list_native_cameras()
        return 0

    config = load_config(args.config.resolve())
    ollama = OllamaClient(config)
    model_info = ollama.info()
    model_service_name = model_info["label"]
    ollama_ready, ollama_status = ollama.check()
    if ollama_ready:
        print(f"{model_service_name}：已连接 / {ollama_status}")
        try:
            print("正在预热 Ollama 模型……")
            ollama.warm_up()
            print("Ollama：模型已预热 / thinking 已关闭")
        except Exception as error:
            print(f"Ollama：模型预热失败，将在首次提问时重试 / {error}")
    else:
        print(f"{model_service_name}：不可用 / {ollama_status}")
    dashboard = CameraDashboard(config) if config.web_enabled else None
    tray = None
    asr_process = None
    paddle_process = None
    if dashboard:
        dashboard.start()
        if not _port_in_use(8770):
            try:
                asr_process = subprocess.Popen(
                    [sys.executable, "-u", "-m", "assistant.asr_server"],
                )
                print("ASR 服务（8770）后台启动中……")
            except Exception as error:
                print(f"ASR 服务启动失败：{error}", file=sys.stderr)
        if _PADDLE_PYTHON.exists() and not _port_in_use(_PADDLE_PORT):
            try:
                paddle_process = subprocess.Popen(
                    [
                        str(_PADDLE_PYTHON), "-u", "-m",
                        "assistant.paddle_service.paddle_server",
                    ],
                )
                print("飞桨分析服务（8771）后台启动中……")
            except Exception as error:
                print(f"飞桨分析服务启动失败：{error}", file=sys.stderr)
        if config.tray_enabled and not args.no_tray:
            try:
                tray = SystemTray(dashboard, config.tray_notifications_enabled)
                tray.start()
                print("系统托盘：已启动")
            except Exception as error:
                print(f"系统托盘启动失败，语音助手仍可继续运行：{error}", file=sys.stderr)
    try:
        if dashboard:
            services = ["dashboard"]
            if asr_process is not None:
                services.append(f"ASR {8770}")
            if paddle_process is not None:
                services.append(f"飞桨 {_PADDLE_PORT}")
            print(
                "语音助手已启动（" + " + ".join(services)
                + " 模式，浏览器访问 http://localhost:8765/）"
            )
            # Windows 上无超时的 Event.wait() 无法被 Ctrl+C 中断，用短超时循环让信号能被处理
            while not dashboard.store.shutdown_event.is_set():
                dashboard.store.shutdown_event.wait(timeout=0.5)
        else:
            print("未启用 Web 服务，按 Ctrl+C 退出。")
    except KeyboardInterrupt:
        print("\n正在退出语音助手……")
    finally:
        if asr_process is not None:
            try:
                asr_process.terminate()
                # KeyboardInterrupt：用户连按 Ctrl+C 时不能让 ASR 变孤儿进程
                try:
                    asr_process.wait(timeout=5)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    asr_process.kill()
            except OSError:
                pass  # 已退出
        if paddle_process is not None:
            _stop_paddle_service(paddle_process)
        if tray:
            tray.stop()
        if dashboard:
            dashboard.stop()
    if dashboard and dashboard.store.restart_event.is_set():
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
