"""Ollama / 在线 Qwen 大模型客户端与路由。"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import urllib.request
from pathlib import Path

from .config import Config
from .platform_utils import build_proxy_opener
from .textutils import (
    build_accent_aware_question,
    parse_person_presence,
    take_speech_segments,
)


class OllamaClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.history: list[dict[str, str]] = []

    @property
    def _history_message_limit(self) -> int:
        return self.config.conversation_history_turns * 2

    def start_conversation(self) -> None:
        """Start a fresh wake-word session without leaking an older topic."""
        self.history.clear()

    def end_conversation(self) -> None:
        self.history.clear()

    def remember(self, question: str, answer: str) -> None:
        self.history.extend(
            (
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            )
        )
        self.history = self.history[-self._history_message_limit :]

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            f"{self.config.ollama_url}{path}", data=data, headers=headers
        )
        with urllib.request.urlopen(
            request, timeout=self.config.ollama_timeout_seconds
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def _stream_request(self, path: str, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.ollama_url}{path}",
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(
            request, timeout=self.config.ollama_timeout_seconds
        ) as response:
            for raw_line in response:
                line = raw_line.strip()
                if line:
                    yield json.loads(line.decode("utf-8"))

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

    def _chat(
        self,
        messages: list[dict],
        temperature: float,
        num_predict: int,
        on_segment=None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        payload = {
            "model": self.config.ollama_model,
            "messages": messages,
            "stream": True,
            "think": False,
            "keep_alive": self.config.ollama_keep_alive,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        answer_parts: list[str] = []
        speech_buffer = ""
        for chunk in self._stream_request("/api/chat", payload):
            if cancel_event is not None and cancel_event.is_set():
                break
            content = str(chunk.get("message", {}).get("content", ""))
            if not content:
                continue
            answer_parts.append(content)
            speech_buffer += content
            segments, speech_buffer = take_speech_segments(speech_buffer)
            if on_segment:
                for segment in segments:
                    on_segment(segment)
        if cancel_event is None or not cancel_event.is_set():
            segments, _ = take_speech_segments(speech_buffer, flush=True)
            if on_segment:
                for segment in segments:
                    on_segment(segment)
        answer = "".join(answer_parts).strip()
        if not answer and (cancel_event is None or not cancel_event.is_set()):
            raise RuntimeError("Ollama 没有返回回答")
        return answer

    def ask(
        self,
        question: str,
        on_segment=None,
        cancel_event: threading.Event | None = None,
        recognition_candidates: list[str] | None = None,
    ) -> str:
        spoken_question = build_accent_aware_question(
            question, recognition_candidates or []
        )
        messages = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {"role": "user", "content": spoken_question},
        ]
        answer = self._chat(messages, 0.4, 160, on_segment, cancel_event)
        if cancel_event is None or not cancel_event.is_set():
            self.remember(question, answer)
        return answer

    def generate_code(self, request: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是代码生成器。只输出简短、可运行的纯代码，不要Markdown代码块，"
                    "不要解释。用户没有指定语言时使用Python。不要生成用于打开应用、"
                    "执行Shell命令或控制电脑的代码，除非编程需求明确要求这些功能。"
                ),
            },
            {"role": "user", "content": request},
        ]
        return self._chat(messages, 0.2, 400)

    def fix_code(self, request: str, code: str, error: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是代码修复器。只输出修复后的完整纯代码，不要Markdown代码块，"
                    "不要解释。不要添加文件、网络、Shell或电脑控制操作。"
                ),
            },
            {
                "role": "user",
                "content": f"原始需求：{request}\n当前代码：\n{code}\n运行错误：\n{error}",
            },
        ]
        return self._chat(messages, 0.1, 600)

    def ask_vision(
        self,
        question: str,
        image_bytes: bytes,
        on_segment=None,
        cancel_event: threading.Event | None = None,
        local_identity_note: str = "",
    ) -> str:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages: list[dict] = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {
                "role": "user",
                "content": (
                    f"{question}\n请根据这张摄像头的当前画面直接回答。"
                    "只描述确实能看到的内容，不确定的地方要明确说明。"
                    "不要根据外貌猜测人物姓名或身份，本机人员库会单独核对。"
                ),
                "images": [image_base64],
            },
        ]
        model_answer = self._chat(messages, 0.2, 180, on_segment, cancel_event)
        answer = model_answer
        if cancel_event is None or not cancel_event.is_set():
            if local_identity_note:
                if on_segment is not None:
                    on_segment(local_identity_note)
                answer = f"{answer.rstrip()} {local_identity_note}".strip()
            # Keep local identities out of later model requests too: history is
            # sent back to the provider on the next turn.
            self.remember(question, model_answer)
        return answer

    def detect_person(self, image_bytes: bytes) -> bool:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "system",
                "content": (
                    "判断图像中是否出现真实的人，包括只露出部分身体的人。"
                    "有人只回答PERSON，无人只回答EMPTY，不要解释。"
                ),
            },
            {"role": "user", "content": "检查画面。", "images": [image_base64]},
        ]
        return parse_person_presence(self._chat(messages, 0.0, 8))

    def describe_scene(self, image_bytes: bytes) -> str:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "system",
                "content": (
                    "你是摄像头画面播报器。只描述当前图片中明确可见的主体、动作和变化，"
                    "用一句自然中文，不超过60个汉字，不要猜测，不要Markdown。"
                ),
            },
            {
                "role": "user",
                "content": "简短播报这张画面。",
                "images": [image_base64],
            },
        ]
        return self._chat(messages, 0.2, 80).strip()


class OnlineQwenClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.history: list[dict[str, str]] = []
        self._opener = build_proxy_opener(getattr(config, "network_proxy", ""))
        self.base_url = config.online_api_base_url
        self.api_key = config.online_api_key
        self._connection_error = ""
        try:
            self.base_url, self.api_key = self._resolve_connection()
        except Exception as error:
            self._connection_error = str(error)

    @property
    def _history_message_limit(self) -> int:
        return self.config.conversation_history_turns * 2

    def _resolve_connection(self) -> tuple[str, str]:
        environment_key = (
            os.environ.get(self.config.online_api_key_env, "").strip()
            if self.config.online_api_key_env
            else ""
        )
        api_key = environment_key or self.api_key
        base_url = self.base_url
        if api_key and base_url:
            return base_url, api_key

        database_path = self.config.online_config_db
        if database_path is None or not database_path.is_file():
            raise RuntimeError("没有找到在线模型 API 配置或密钥")
        database_uri = database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as database:
            rows = dict(
                database.execute(
                    "SELECT key, value FROM config "
                    "WHERE key IN ('openai.api_base_urls', 'openai.api_keys')"
                )
            )
        urls = json.loads(rows.get("openai.api_base_urls", "[]"))
        keys = json.loads(rows.get("openai.api_keys", "[]"))
        if not urls or not keys:
            raise RuntimeError("qwenchat 中没有可用的在线模型连接")
        if base_url:
            normalized_base = base_url.rstrip("/")
            for index, candidate in enumerate(urls):
                if str(candidate).rstrip("/") == normalized_base and index < len(keys):
                    return normalized_base, str(keys[index])
            raise RuntimeError("qwenchat 中没有找到匹配的在线接口地址")
        return str(urls[0]).rstrip("/"), str(keys[0])

    def _require_connection(self) -> None:
        if self._connection_error:
            raise RuntimeError(self._connection_error)
        if not self.base_url or not self.api_key:
            raise RuntimeError("在线模型连接配置不完整")

    def _request(self, path: str, payload: dict | None = None) -> dict:
        self._require_connection()
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with self._opener.open(
            request, timeout=self.config.online_timeout_seconds
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def _stream_request(self, path: str, payload: dict):
        self._require_connection()
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "text/event-stream",
            },
        )
        with self._opener.open(
            request, timeout=self.config.online_timeout_seconds
        ) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                event_data = line[5:].strip()
                if not event_data or event_data == "[DONE]":
                    continue
                yield json.loads(event_data)

    def check(self) -> tuple[bool, str]:
        try:
            response = self._request("/models")
            model_ids = {
                str(model.get("id", "")) for model in response.get("data", [])
            }
            if self.config.online_model not in model_ids:
                return False, f"服务端没有模型 {self.config.online_model}"
            return True, self.config.online_model
        except Exception as error:
            return False, str(error)

    def warm_up(self) -> None:
        # Online models do not need a paid warm-up request.
        return None

    def start_conversation(self) -> None:
        self.history.clear()

    def end_conversation(self) -> None:
        self.history.clear()

    def remember(self, question: str, answer: str) -> None:
        self.history.extend(
            (
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            )
        )
        self.history = self.history[-self._history_message_limit :]

    def _chat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        on_segment=None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        payload = {
            "model": self.config.online_model,
            "messages": messages,
            "stream": True,
            "enable_thinking": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        answer_parts: list[str] = []
        speech_buffer = ""
        for chunk in self._stream_request("/chat/completions", payload):
            if cancel_event is not None and cancel_event.is_set():
                break
            choices = chunk.get("choices", [])
            if not choices:
                continue
            content = str(choices[0].get("delta", {}).get("content") or "")
            if not content:
                continue
            answer_parts.append(content)
            speech_buffer += content
            segments, speech_buffer = take_speech_segments(speech_buffer)
            if on_segment:
                for segment in segments:
                    on_segment(segment)
        if cancel_event is None or not cancel_event.is_set():
            segments, _ = take_speech_segments(speech_buffer, flush=True)
            if on_segment:
                for segment in segments:
                    on_segment(segment)
        answer = "".join(answer_parts).strip()
        if not answer and (cancel_event is None or not cancel_event.is_set()):
            raise RuntimeError("在线 Qwen 没有返回回答")
        return answer

    def ask(
        self,
        question: str,
        on_segment=None,
        cancel_event: threading.Event | None = None,
        recognition_candidates: list[str] | None = None,
    ) -> str:
        spoken_question = build_accent_aware_question(
            question, recognition_candidates or []
        )
        messages = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {"role": "user", "content": spoken_question},
        ]
        answer = self._chat(messages, 0.4, 160, on_segment, cancel_event)
        if cancel_event is None or not cancel_event.is_set():
            self.remember(question, answer)
        return answer

    def generate_code(self, request: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是代码生成器。只输出简短、可运行的纯代码，不要Markdown代码块，"
                    "不要解释。用户没有指定语言时使用Python。不要生成用于打开应用、"
                    "执行Shell命令或控制电脑的代码，除非编程需求明确要求这些功能。"
                ),
            },
            {"role": "user", "content": request},
        ]
        return self._chat(messages, 0.2, 400)

    def fix_code(self, request: str, code: str, error: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是代码修复器。只输出修复后的完整纯代码，不要Markdown代码块，"
                    "不要解释。不要添加文件、网络、Shell或电脑控制操作。"
                ),
            },
            {
                "role": "user",
                "content": f"原始需求：{request}\n当前代码：\n{code}\n运行错误：\n{error}",
            },
        ]
        return self._chat(messages, 0.1, 600)

    def ask_vision(
        self,
        question: str,
        image_bytes: bytes,
        on_segment=None,
        cancel_event: threading.Event | None = None,
        local_identity_note: str = "",
    ) -> str:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages: list[dict] = [
            {"role": "system", "content": self.config.ollama_system_prompt},
            *self.history[-self._history_message_limit :],
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{question}\n请根据这张摄像头的当前画面直接回答。"
                            "只描述确实能看到的内容，不确定的地方要明确说明。"
                            "不要根据外貌猜测人物姓名或身份，本机人员库会单独核对。"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        },
                    },
                ],
            },
        ]
        model_answer = self._chat(messages, 0.2, 180, on_segment, cancel_event)
        answer = model_answer
        if cancel_event is None or not cancel_event.is_set():
            if local_identity_note:
                if on_segment is not None:
                    on_segment(local_identity_note)
                answer = f"{answer.rstrip()} {local_identity_note}".strip()
            self.remember(question, model_answer)
        return answer

    def detect_person(self, image_bytes: bytes) -> bool:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "system",
                "content": (
                    "判断图像中是否出现真实的人，包括只露出部分身体的人。"
                    "有人只回答PERSON，无人只回答EMPTY，不要解释。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "检查画面。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                ],
            },
        ]
        return parse_person_presence(self._chat(messages, 0.0, 8))

    def describe_scene(self, image_bytes: bytes) -> str:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "system",
                "content": (
                    "你是摄像头画面播报器。只描述当前图片中明确可见的主体、动作和变化，"
                    "用一句自然中文，不超过60个汉字，不要猜测，不要Markdown。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "简短播报这张画面。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                ],
            },
        ]
        return self._chat(messages, 0.2, 80).strip()


class ModelRouter:
    PROVIDER_ALIASES = {
        "online": "online",
        "qwen": "online",
        "openai": "online",
        "ollama": "ollama",
        "local": "ollama",
    }

    def __init__(self, config: Config, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.online = OnlineQwenClient(config)
        self.local = OllamaClient(config)
        self._lock = threading.RLock()
        self._provider = self.PROVIDER_ALIASES.get(config.llm_provider, "ollama")

    def _client(self) -> OllamaClient | OnlineQwenClient:
        with self._lock:
            return self.online if self._provider == "online" else self.local

    def info(self) -> dict[str, str]:
        with self._lock:
            if self._provider == "online":
                return {
                    "provider": "online",
                    "model": self.config.online_model,
                    "label": self.config.online_model,
                }
            return {
                "provider": "ollama",
                "model": self.config.ollama_model,
                "label": self.config.ollama_model,
            }

    def check(self) -> tuple[bool, str]:
        return self._client().check()

    def warm_up(self) -> None:
        self._client().warm_up()

    def start_conversation(self) -> None:
        self._client().start_conversation()

    def end_conversation(self) -> None:
        self._client().end_conversation()

    def remember(self, question: str, answer: str) -> None:
        self._client().remember(question, answer)

    def ask(
        self,
        question: str,
        on_segment=None,
        cancel_event: threading.Event | None = None,
        recognition_candidates: list[str] | None = None,
    ) -> str:
        return self._client().ask(
            question, on_segment, cancel_event, recognition_candidates
        )

    def generate_code(self, request: str) -> str:
        return self._client().generate_code(request)

    def fix_code(self, request: str, code: str, error: str) -> str:
        return self._client().fix_code(request, code, error)

    def ask_vision(
        self,
        question: str,
        image_bytes: bytes,
        on_segment=None,
        cancel_event: threading.Event | None = None,
        local_identity_note: str = "",
    ) -> str:
        return self._client().ask_vision(
            question, image_bytes, on_segment, cancel_event, local_identity_note
        )

    def detect_person(self, image_bytes: bytes) -> bool:
        return self._client().detect_person(image_bytes)

    def describe_scene(self, image_bytes: bytes) -> str:
        return self._client().describe_scene(image_bytes)

    def switch(self, provider: str) -> dict[str, str]:
        normalized = self.PROVIDER_ALIASES.get(provider.strip().casefold())
        if normalized is None:
            raise ValueError("不支持的模型类型")
        target = self.online if normalized == "online" else self.local
        ready, status = target.check()
        if not ready:
            raise RuntimeError(status)
        with self._lock:
            if normalized != self._provider:
                self.online.end_conversation()
                self.local.end_conversation()
                self._provider = normalized
                self._persist_provider(normalized)
            return self.info()

    def _persist_provider(self, provider: str) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["llm_provider"] = provider
        self.config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
