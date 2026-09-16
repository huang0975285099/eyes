"""老叶语音助手核心包。

包初始化保持轻量，避免仅加载配置时同时初始化声音、视觉和网络模块。
兼容导出由项目根目录的 ``voice_assistant.py`` 提供。
"""

from .config import (
    AudioDevice,
    Config,
    DEFAULT_CONFIG,
    DEFAULT_MODEL_URL,
    load_config,
)
from .paths import APP_DIR
__all__ = [
    "APP_DIR",
    "AudioDevice",
    "Config",
    "DEFAULT_CONFIG",
    "DEFAULT_MODEL_URL",
    "load_config",
]
