"""命令行入口与启动流程。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vosk import Model, SetLogLevel

from .config import DEFAULT_CONFIG, load_config
from .core import VoiceAssistant
from .dashboard import CameraDashboard
from .llm import ModelRouter
from .native_camera import list_native_cameras
from .platform_utils import (
    configure_windows_console,
    find_audio_device,
    list_audio_devices,
)
from .speaker import Speaker
from .textutils import ensure_model
from .tools import OnlineSearchTools
from .tray import SystemTray


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MM101S USB 摄像头语音交互")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径"
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="列出 PortAudio 音频设备"
    )
    parser.add_argument(
        "--list-cameras", action="store_true", help="探测 OpenCV 摄像头索引"
    )
    parser.add_argument(
        "--download-model", action="store_true", help="只下载并校验识别模型"
    )
    parser.add_argument(
        "--test-speaker", action="store_true", help="从配置的扬声器播放测试语音"
    )
    parser.add_argument("--no-tray", action="store_true", help="不显示 Windows 托盘图标")
    return parser


def main() -> int:
    configure_windows_console()
    args = build_parser().parse_args()
    if args.list_devices:
        list_audio_devices()
        return 0
    if args.list_cameras:
        list_native_cameras()
        return 0

    config = load_config(args.config.resolve())
    model_path = ensure_model(config.model_path, config.model_url)
    if args.download_model:
        print(f"模型已就绪：{model_path}")
        return 0

    input_device = find_audio_device(config.input_device, "input")
    output_device = find_audio_device(config.output_device, "output")
    print(
        f"录音：[{input_device.index}] {input_device.name} / "
        f"{input_device.host_api} / {input_device.sample_rate} Hz"
    )
    print(
        f"播放：[{output_device.index}] {output_device.name} / "
        f"{output_device.host_api} / {output_device.sample_rate} Hz"
    )
    speaker = Speaker(output_device, config)
    removed_audio_files = speaker.clear_cache()
    if removed_audio_files:
        print(f"已清理上次遗留的语音缓存：{removed_audio_files} 个文件")
    if args.test_speaker:
        speaker.say("老叶语音助手已连接成功。")
        print("扬声器测试完成。")
        return 0

    SetLogLevel(-1)
    ollama = ModelRouter(config, args.config.resolve())
    model_info = ollama.info()
    model_service_name = model_info["label"]
    online_tools = OnlineSearchTools(config)
    ollama_ready, ollama_status = ollama.check()
    if ollama_ready:
        print(f"{model_service_name}：已连接 / {ollama_status}")
        if config.llm_provider not in {"online", "qwen", "openai"}:
            try:
                print("正在预热 Ollama 模型……")
                ollama.warm_up()
                print("Ollama：模型已预热 / thinking 已关闭")
            except Exception as error:
                print(f"Ollama：模型预热失败，将在首次提问时重试 / {error}")
    else:
        print(f"{model_service_name}：不可用 / {ollama_status}")
    print("正在加载离线中文识别模型……")
    model = Model(str(model_path))
    dashboard = CameraDashboard(config, ollama) if config.web_enabled else None
    tray = None
    if dashboard:
        dashboard.start()
        if config.tray_enabled and not args.no_tray:
            try:
                tray = SystemTray(dashboard, config.tray_notifications_enabled)
                tray.start()
                dashboard.store.set_tray_active(True)
                print("系统托盘：已启动")
            except Exception as error:
                print(f"系统托盘启动失败，语音助手仍可继续运行：{error}", file=sys.stderr)
    try:
        VoiceAssistant(
            config,
            model,
            input_device,
            speaker,
            ollama,
            online_tools,
            dashboard,
        ).run()
    finally:
        if tray:
            if dashboard:
                dashboard.store.set_tray_active(False)
            tray.stop()
        if dashboard:
            dashboard.stop()
    if dashboard and dashboard.store.restart_event.is_set():
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
