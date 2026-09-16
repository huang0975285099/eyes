"""语音合成与播放。"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

import edge_tts
import miniaudio
import numpy as np
import sounddevice as sd

from .config import AudioDevice, Config
from .paths import APP_DIR
from .textutils import resample_pcm


@dataclass(frozen=True)
class PreparedAudio:
    samples: np.ndarray
    cache_path: Path


class Speaker:
    CACHE_DIR = APP_DIR / ".tts-cache"

    def __init__(self, device: AudioDevice, config: Config) -> None:
        self.device = device
        self.config = config

    @classmethod
    def clear_cache(cls) -> int:
        if not cls.CACHE_DIR.is_dir():
            return 0
        removed = 0
        for cache_path in cls.CACHE_DIR.glob("*.mp3"):
            try:
                cache_path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
        return removed

    def chime(self, success: bool = True) -> None:
        duration = 0.23
        sample_rate = self.device.sample_rate
        timeline = np.arange(round(duration * sample_rate)) / sample_rate
        frequency = 880 if success else 330
        wave = np.sin(2 * np.pi * frequency * timeline)
        if success:
            wave += 0.45 * np.sin(2 * np.pi * frequency * 1.5 * timeline)
        envelope = np.exp(-8 * timeline)
        mono = (0.16 * wave * envelope).astype(np.float32)
        stereo = np.column_stack((mono, mono))
        sd.play(stereo, sample_rate, device=self.device.index, blocking=True)

    async def _synthesize(self, text: str) -> bytes:
        communicate = edge_tts.Communicate(
            text,
            self.config.tts_voice,
            rate=self.config.tts_rate,
            volume=self.config.tts_volume,
            proxy=self.config.tts_proxy or None,
        )
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    def _cache_path(self, text: str) -> Path:
        cache_key = "|".join(
            (self.config.tts_voice, self.config.tts_rate, self.config.tts_volume, text)
        )
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        return self.CACHE_DIR / f"{digest}.mp3"

    def prepare(
        self, text: str, cancel_event: threading.Event | None = None
    ) -> PreparedAudio | None:
        if cancel_event is not None and cancel_event.is_set():
            return None
        cache_path = self._cache_path(text)
        try:
            if cache_path.exists():
                audio_bytes = cache_path.read_bytes()
            else:
                audio_bytes = asyncio.run(self._synthesize(text))
                if audio_bytes:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(audio_bytes)
            if not audio_bytes:
                raise RuntimeError("语音合成没有返回音频")
            if cancel_event is not None and cancel_event.is_set():
                cache_path.unlink(missing_ok=True)
                return None
            decoded = miniaudio.decode(
                audio_bytes, output_format=miniaudio.SampleFormat.SIGNED16
            )
            samples = np.asarray(decoded.samples, dtype=np.int16)
            samples = samples.reshape(-1, decoded.nchannels)
            samples = resample_pcm(
                samples, decoded.sample_rate, self.device.sample_rate
            )
            if samples.shape[1] == 1:
                samples = np.repeat(samples, 2, axis=1)
            return PreparedAudio(samples=samples, cache_path=cache_path)
        except Exception:
            cache_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def discard_prepared(prepared: PreparedAudio) -> None:
        prepared.cache_path.unlink(missing_ok=True)

    def play_prepared(
        self,
        prepared: PreparedAudio,
        cancel_event: threading.Event | None = None,
    ) -> None:
        try:
            if cancel_event is not None and cancel_event.is_set():
                return
            sd.play(
                prepared.samples,
                self.device.sample_rate,
                device=self.device.index,
                blocking=True,
            )
        finally:
            self.discard_prepared(prepared)

    def say(self, text: str, cancel_event: threading.Event | None = None) -> None:
        prepared = self.prepare(text, cancel_event)
        if prepared is not None:
            self.play_prepared(prepared, cancel_event)
