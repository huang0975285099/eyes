"""Qwen3-ASR 独立转写服务。

独立进程加载 torch + Qwen3-ASR 模型，避免与主应用的 DLL 冲突。
前端 web/index-voice.js 直连 http://localhost:8770/ 的以下端点：
/transcribe（语音识别）、/tts（合成）、/chat（对话）、/vision（画面描述）、
/cameras（远端摄像头查询）、/log（前端日志转发）。

启动：python -m assistant.asr_server
"""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import torch
from qwen_asr import Qwen3ASRModel

from .config import load_config
from .llm import OllamaClient
from .paths import APP_DIR
from .remote_cameras import RemoteCameraRegistry

_ASR_PORT = 8770
_MODEL: Qwen3ASRModel | None = None

# 繁简转换：Qwen3-ASR 可能输出繁体，统一转简体；opencc 不可用时原样返回
try:
    from opencc import OpenCC
    _T2S = OpenCC("t2s")
except Exception:
    _T2S = None


def _to_simplified(text: str) -> str:
    if _T2S is None or not text:
        return text
    return _T2S.convert(text)


_CONFIG = load_config(APP_DIR / "config.json")
_OLLAMA = OllamaClient(_CONFIG)
_REMOTE_CAMERAS = RemoteCameraRegistry(_CONFIG)
_REMOTE_CAMERAS.start()


def _tts_bytes(text: str) -> bytes:
    """用 edge-tts 生成语音 MP3。优先走配置代理，失败时直连重试。"""
    import edge_tts

    async def _generate(proxy):
        communicate = edge_tts.Communicate(
            text,
            _CONFIG.tts_voice,
            rate=_CONFIG.tts_rate,
            volume=_CONFIG.tts_volume,
            proxy=proxy,
        )
        parts = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                parts.append(chunk["data"])
        return b"".join(parts)

    proxy = _CONFIG.tts_proxy or None
    try:
        return asyncio.run(_generate(proxy))
    except Exception:
        if proxy:
            return asyncio.run(_generate(None))
        raise


def _load_audio_bytes(data: bytes):
    """用 PyAV 从内存直接解码音频为 (numpy_array, sample_rate)。"""
    import av
    import io
    import numpy as np

    container = av.open(io.BytesIO(data))
    sample_rate = container.streams.audio[0].rate
    arrays = []
    for frame in container.decode(audio=0):
        arrays.append(frame.to_ndarray())
    container.close()
    if not arrays:
        raise ValueError("音频数据中没有音频帧")
    audio = np.concatenate(arrays, axis=1)
    if audio.ndim > 1 and audio.shape[0] > 1:
        audio = audio.mean(axis=0)
    elif audio.ndim > 1:
        audio = audio[0]
    return audio, sample_rate


def _get_model() -> Qwen3ASRModel:
    global _MODEL
    if _MODEL is None:
        print("正在加载 Qwen3-ASR 模型到 GPU …")
        _MODEL = Qwen3ASRModel.from_pretrained(
            str(APP_DIR / "models" / "Qwen3-ASR-0.6B"),
            dtype=torch.float16,
            device_map="cuda",
            max_inference_batch_size=1,
            max_new_tokens=256,
        )
        print(f"模型加载完成，显存占用 {torch.cuda.memory_allocated() / 1024**2:.1f} MB")
    return _MODEL


def _check_ollama() -> None:
    """启动前探测 Ollama；未运行则打印警告（ASR/TTS 仍可用，仅对话不可用）。"""
    ready, status = _OLLAMA.check()
    if ready:
        return
    if "未安装模型" in status:
        print(f"⚠ 警告：Ollama 在运行，但未找到模型，请执行：ollama pull {_CONFIG.ollama_model}")
    else:
        print(f"⚠ 警告：Ollama 未启动或不可用 / {status}，语音对话将不可用（识别/播报不受影响）。")
        print("  启动命令：ollama serve  （或启动 Ollama 应用程序）")


class ASRHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/cameras":
            query = parse_qs(parsed.query).get("q", [""])[0]
            try:
                items = _REMOTE_CAMERAS.search(query)
                self._respond({"items": items})
            except Exception as error:  # noqa: BLE001
                self._respond(
                    {"items": [], "error": str(error)},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > 1024 * 1024:
            raise ValueError("无效的请求内容")
        return json.loads(self.rfile.read(content_length).decode("utf-8"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/log":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                msg = self.rfile.read(content_length).decode("utf-8", "ignore") if content_length > 0 else ""
                print(msg)
            except Exception:
                pass
            self._respond({"ok": True})
            return
        if path == "/chat":
            try:
                payload = self._read_json_body()
                messages = payload.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise ValueError("消息列表为空")
                answer = _OLLAMA.chat(messages)
                print(f"AI：{answer}")
                self._respond({"text": answer})
            except Exception as error:
                self._respond(
                    {"text": "", "error": str(error)},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if path == "/vision":
            try:
                import base64
                payload = self._read_json_body()
                question = str(payload.get("text", "")).strip()
                image_b64 = str(payload.get("image", "")).strip()
                if not question or not image_b64:
                    raise ValueError("缺少问题或图像")
                if image_b64.startswith("data:") and "," in image_b64:
                    image_b64 = image_b64.split(",", 1)[1]
                image_bytes = base64.b64decode(image_b64)
                if len(image_bytes) > 8 * 1024 * 1024:
                    raise ValueError("图像过大")
                answer = _OLLAMA.vision(question, image_bytes)
                print(f"[视觉] {question} -> {answer}")
                self._respond({"text": answer})
            except Exception as error:
                self._respond(
                    {"text": "", "error": str(error)},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if path == "/tts":
            try:
                payload = self._read_json_body()
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("文本为空")
                mp3 = _tts_bytes(text)
                self._respond_binary(mp3, "audio/mpeg")
            except Exception as error:
                self._respond(
                    {"text": "", "error": str(error)},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if path != "/transcribe":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        import time

        t0 = time.perf_counter()
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0:
                raise ValueError("音频数据为空")
            audio_bytes = self.rfile.read(content_length)
            t_read = time.perf_counter()
            model = _get_model()
            context = str(query.get("context", [""])[0] or "")[:200]
            audio_array, sample_rate = _load_audio_bytes(audio_bytes)
            t_decode = time.perf_counter()
            result = model.transcribe((audio_array, sample_rate), context=context)
            t_infer = time.perf_counter()
            text = _to_simplified(result[0].text if result else "")
            print(
                f"[transcribe] 音频{content_length/1024:.0f}KB "
                f"读取{(t_read-t0)*1000:.0f}ms 解码{(t_decode-t_read)*1000:.0f}ms "
                f"推理{(t_infer-t_decode)*1000:.0f}ms"
            )
            print(f"用户：{text}")
            self._respond({"text": text})
        except Exception as error:
            self._respond({"text": "", "error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _respond(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_binary(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    _check_ollama()
    _get_model()
    server = ThreadingHTTPServer(("127.0.0.1", _ASR_PORT), ASRHandler)
    print(f"ASR 转写服务已启动：http://127.0.0.1:{_ASR_PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nASR 服务正在停止……")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
