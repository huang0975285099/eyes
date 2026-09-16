"""Windows 控制台、网络代理与音频设备工具。"""

from __future__ import annotations

import ctypes
import os
import sys
import urllib.request

import sounddevice as sd

from .config import AudioDevice


def build_proxy_opener(proxy_url: str):
    normalized = str(proxy_url).strip()
    proxy_mapping = {"http": normalized, "https": normalized} if normalized else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxy_mapping))


def configure_windows_console() -> None:
    """Configure UTF-8 and a CJK-capable font in the legacy Windows console."""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

        if os.environ.get("WT_SESSION") or not sys.stdout.isatty():
            return

        class Coord(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class ConsoleFontInfoEx(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("nFont", ctypes.c_ulong),
                ("dwFontSize", Coord),
                ("FontFamily", ctypes.c_uint),
                ("FontWeight", ctypes.c_uint),
                ("FaceName", ctypes.c_wchar * 32),
            ]

        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel32.GetStdHandle(-11)
        info = ConsoleFontInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
            if info.FaceName.casefold() in {
                "consolas",
                "lucida console",
                "terminal",
            }:
                info.FaceName = "NSimSun"
                kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info))
    except Exception:
        # Console setup is cosmetic and must never prevent voice interaction.
        pass


def _host_api_name(index: int) -> str:
    return str(sd.query_hostapis(index)["name"])


def list_audio_devices() -> None:
    print("可用录音设备：")
    for index, device in enumerate(sd.query_devices()):
        if int(device["max_input_channels"]) > 0:
            print(f"  [{index:>2}] {device['name']} ({_host_api_name(device['hostapi'])})")
    print("\n可用播放设备：")
    for index, device in enumerate(sd.query_devices()):
        if int(device["max_output_channels"]) > 0:
            print(f"  [{index:>2}] {device['name']} ({_host_api_name(device['hostapi'])})")


def find_audio_device(selector: str | int, direction: str) -> AudioDevice:
    if direction not in {"input", "output"}:
        raise ValueError(f"未知设备方向：{direction}")
    devices = sd.query_devices()
    channel_key = f"max_{direction}_channels"

    if isinstance(selector, int) or str(selector).strip().isdigit():
        index = int(selector)
        if index < 0 or index >= len(devices):
            raise RuntimeError(f"音频设备编号 {index} 不存在")
        candidates = [(index, devices[index])]
    else:
        needle = str(selector).strip().casefold()
        candidates = [
            (index, device)
            for index, device in enumerate(devices)
            if needle in str(device["name"]).casefold()
        ]

    candidates = [item for item in candidates if int(item[1][channel_key]) > 0]
    if not candidates:
        kind = "录音" if direction == "input" else "播放"
        raise RuntimeError(
            f"找不到匹配 {selector!r} 的{kind}设备。运行 --list-devices 查看设备名。"
        )

    def score(item: tuple[int, object]) -> tuple[int, float]:
        device = item[1]
        api = _host_api_name(int(device["hostapi"]))
        api_score = {
            "Windows WASAPI": 4,
            "Windows DirectSound": 3,
            "MME": 2,
            "Windows WDM-KS": 1,
        }.get(api, 0)
        latency = float(device[f"default_low_{direction}_latency"])
        return api_score, -latency

    index, device = max(candidates, key=score)
    return AudioDevice(
        index=index,
        name=str(device["name"]),
        host_api=_host_api_name(int(device["hostapi"])),
        sample_rate=int(round(float(device["default_samplerate"]))),
        channels=int(device[channel_key]),
    )
