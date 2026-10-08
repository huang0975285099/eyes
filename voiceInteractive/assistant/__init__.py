"""老叶语音助手核心包。

包初始化保持轻量，避免仅加载配置时同时初始化声音、视觉和网络模块。
"""

from .config import (
    AudioDevice,
    Config,
    DEFAULT_CONFIG,
    load_config,
)
from .paths import APP_DIR
__all__ = [
    "APP_DIR",
    "AudioDevice",
    "Config",
    "DEFAULT_CONFIG",
    "load_config",
]
