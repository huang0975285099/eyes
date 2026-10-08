"""Ollama 大模型客户端。

仅保留本地 Ollama 接口（``info`` / ``check`` / ``warm_up``），供 Web Dashboard
与启动流程使用。在线 Qwen 客户端与多提供方路由（``OnlineQwenClient`` /
``ModelRouter.switch`` 等）已随在线大模型支持一并移除。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .config import Config


class OllamaClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _request(
        self, path: str, payload: dict | None = None, timeout_seconds: float | None = None
    ) -> dict:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            f"{self.config.ollama_url}{path}", data=data, headers=headers
        )
        with urllib.request.urlopen(
            request,
            timeout=timeout_seconds or self.config.ollama_timeout_seconds,
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def check(self) -> tuple[bool, str]:
        if not self.config.ollama_enabled:
            return False, "配置中已关闭"
        try:
            response = self._request("/api/tags")
            names = {
                str(model.get("name", "")) for model in response.get("models", [])
            }
            if self.config.ollama_model not in names:
                return False, f"未安装模型 {self.config.ollama_model}"
            return True, self.config.ollama_model
        except Exception as error:
            return False, str(error)

    def warm_up(self) -> None:
        """Load the model before the first spoken question to avoid cold-start delay."""
        self._request(
            "/api/generate",
            {
                "model": self.config.ollama_model,
                "prompt": "",
                "stream": False,
                "think": False,
                "keep_alive": self.config.ollama_keep_alive,
            },
        )


class ModelRouter:
    """LLM 入口；仅本地 Ollama，保留 config_path 供 Dashboard 持久化音频设备选择。"""

    def __init__(self, config: Config, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.local = OllamaClient(config)

    def info(self) -> dict[str, str]:
        return {
            "provider": "ollama",
            "model": self.config.ollama_model,
            "label": self.config.ollama_model,
        }

    def check(self) -> tuple[bool, str]:
        return self.local.check()

    def warm_up(self) -> None:
        self.local.warm_up()
