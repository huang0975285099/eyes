"""本地 Ollama 客户端。

提供 check / warm_up / info / chat / vision，供主进程启动预热（main.py）与
ASR 服务（asr_server.py）共享。二者各自独立进程 import 本模块，无共享运行时状态。
"""

from __future__ import annotations

import base64
import json
import urllib.request

from .config import Config


class OllamaClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._vision_api_ok = False

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
            names = {str(model.get("name", "")) for model in response.get("models", [])}
            if self.config.ollama_model not in names:
                return False, f"未安装模型 {self.config.ollama_model}"
            return True, self.config.ollama_model
        except Exception as error:
            return False, str(error)

    def warm_up(self) -> None:
        """Load the model before the first request to avoid cold-start delay."""
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

    def info(self) -> dict[str, str]:
        return {
            "provider": "ollama",
            "model": self.config.ollama_model,
            "label": self.config.ollama_model,
        }

    def chat(self, messages: list[dict], temperature: float = 0.4, num_predict: int = 512) -> str:
        system_prompt = self.config.ollama_system_prompt
        all_messages = (
            [{"role": "system", "content": system_prompt}] if system_prompt else []
        ) + messages
        result = self._request(
            "/api/chat",
            {
                "model": self.config.ollama_model,
                "messages": all_messages,
                "stream": False,
                "think": False,
                "keep_alive": self.config.ollama_keep_alive,
                "options": {"temperature": temperature, "num_predict": num_predict},
            },
        )
        return str(result.get("message", {}).get("content", "")).strip()

    def vision(self, question: str, image_bytes: bytes, source: str = "camera", num_predict: int = 256) -> str:
        # 优先用远端视觉推理 API（qwen3-vl-8b），不可用或失败则回退本地 Ollama
        if self._vision_api_ok:
            try:
                return self.vision_via_api(question, image_bytes, source, num_predict)
            except Exception as error:  # noqa: BLE001
                print(f"[vision] 推理API失败，回退本地模型：{error}", flush=True)
                self._vision_api_ok = False
        return self.vision_local(question, image_bytes, source, num_predict)

    def _vision_prompt(self, question: str, source: str) -> str:
        if source == "desktop":
            return (
                f"{question}\n这是电脑桌面的推流画面，不是物理摄像头。"
                "请从桌面角度回答：当前正在使用什么应用程序、用户在做什么操作。"
                "只描述确实能看到的内容，不确定的要明确说明。"
            )
        return (
            f"{question}\n请根据这张摄像头的当前画面直接回答。"
            "只描述确实能看到的内容，不确定的要明确说明。"
            "不要根据外貌猜测人物姓名或身份。"
        )

    def vision_local(self, question: str, image_bytes: bytes, source: str = "camera", num_predict: int = 256) -> str:
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        messages: list[dict] = []
        if self.config.ollama_system_prompt:
            messages.append({"role": "system", "content": self.config.ollama_system_prompt})
        messages.append(
            {"role": "user", "content": self._vision_prompt(question, source), "images": [image_b64]}
        )
        result = self._request(
            "/api/chat",
            {
                "model": self.config.ollama_model,
                "messages": messages,
                "stream": False,
                "think": False,
                "keep_alive": self.config.ollama_keep_alive,
                "options": {"temperature": 0.4, "num_predict": num_predict},
            },
        )
        return str(result.get("message", {}).get("content", "")).strip()

    def vision_via_api(self, question: str, image_bytes: bytes, source: str = "camera", num_predict: int = 256) -> str:
        """通过 OpenAI 兼容的视觉推理 API（qwen3-vl-8b）描述图片。"""
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        messages: list[dict] = []
        if self.config.ollama_system_prompt:
            messages.append({"role": "system", "content": self.config.ollama_system_prompt})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": self._vision_prompt(question, source)},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }
        )
        payload = {
            "model": self.config.vision_api_model,
            "messages": messages,
            "stream": False,
            "max_tokens": num_predict,
            "temperature": 0.4,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.vision_api_base}/chat/completions",
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(
            request, timeout=self.config.ollama_timeout_seconds
        ) as response:
            result = json.loads(response.read().decode("utf-8"))
        return str(result["choices"][0]["message"]["content"]).strip()

    def check_vision_api(self) -> bool:
        """探测远端视觉推理 API 是否可用；可用则后续 vision 走该 API。"""
        self._vision_api_ok = False
        if not self.config.vision_api_enabled or not self.config.vision_api_base:
            return False
        try:
            request = urllib.request.Request(f"{self.config.vision_api_base}/models")
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            self._vision_api_ok = bool(data.get("data"))
            return self._vision_api_ok
        except Exception as error:  # noqa: BLE001
            print(f"[vision-api] 探测失败，视觉将走本地模型：{error}", flush=True)
            self._vision_api_ok = False
            return False
