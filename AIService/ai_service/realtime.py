from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Callable, Iterable
from urllib.parse import urlsplit

from .api_client import MediaAPIClient, MediaAPIError
from .config import Settings
from .events import AggregatedEvent, EventAggregator, EventEvidenceWriter, EventPolicy
from .modules import AnalyzerRegistry, VideoFrame
from .state import ServiceState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RealtimeRule:
    algorithm_code: str
    config: dict[str, Any]

    def sample_fps(self, maximum: float) -> float:
        try:
            value = float(self.config.get("sample_fps", 1.0))
        except (TypeError, ValueError):
            value = 1.0
        return max(0.05, min(maximum, value))


@dataclass(frozen=True)
class StreamConfig:
    video_source_id: int
    customer_id: int
    stream_name: str
    input_url: str
    fallback_url: str
    rules: tuple[RealtimeRule, ...]

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "StreamConfig":
        source_id = int(value.get("video_source_id", 0) or 0)
        stream_name = str(value.get("stream_name", "")).strip()
        if source_id <= 0 or not stream_name:
            raise ValueError("realtime stream identity is invalid")
        input_url = _validated_stream_url(str(value.get("input_url", "")))
        fallback_value = str(value.get("fallback_url", "")).strip()
        fallback_url = _validated_stream_url(fallback_value) if fallback_value else ""
        raw_rules = value.get("rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError("realtime stream rules must be a list")
        rules: list[RealtimeRule] = []
        seen: set[str] = set()
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                continue
            code = str(raw_rule.get("algorithm_code", "")).strip().lower()
            config = raw_rule.get("config", {})
            if not code or code in seen or not isinstance(config, dict):
                continue
            seen.add(code)
            rules.append(RealtimeRule(code, dict(config)))
        return cls(
            video_source_id=source_id,
            customer_id=int(value.get("customer_id", 0) or 0),
            stream_name=stream_name,
            input_url=input_url,
            fallback_url=fallback_url,
            rules=tuple(rules),
        )


class FrameRingBuffer:
    def __init__(self, retain_seconds: int) -> None:
        self._retain = timedelta(seconds=retain_seconds)
        self._frames: deque[VideoFrame] = deque()
        self._lock = threading.Lock()

    def add(self, frame: VideoFrame) -> None:
        cutoff = frame.captured_at - self._retain
        with self._lock:
            self._frames.append(frame)
            while self._frames and self._frames[0].captured_at < cutoff:
                self._frames.popleft()

    def snapshot(self) -> list[VideoFrame]:
        with self._lock:
            return list(self._frames)


FrameCallback = Callable[[StreamConfig, RealtimeRule, VideoFrame, FrameRingBuffer], None]
RemoveCallback = Callable[[StreamConfig, FrameRingBuffer], None]


class StreamSession:
    """Owns exactly one FFmpeg decoder for one source and fans frames out."""

    def __init__(
        self,
        config: StreamConfig,
        *,
        ffmpeg_path: str,
        max_fps: float,
        ring_seconds: int,
        reconnect_seconds: float,
        frame_width: int,
        ring_fps: float,
        callback: FrameCallback,
    ) -> None:
        self.config = config
        self.ring = FrameRingBuffer(ring_seconds)
        self._ffmpeg_path = ffmpeg_path
        self._max_fps = max_fps
        self._reconnect_seconds = reconnect_seconds
        self._frame_width = frame_width
        self._ring_interval = 1.0 / ring_fps
        self._callback = callback
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"stream-{config.video_source_id}",
            daemon=True,
        )
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._sequence = 0
        self._next_due: dict[str, float] = {}
        self._last_ring_at = 0.0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
        self._thread.join(timeout=10)
        if process is not None and process.poll() is None:
            process.kill()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def _run(self) -> None:
        urls = list(dict.fromkeys(filter(None, [self.config.input_url, self.config.fallback_url])))
        while not self._stop.is_set():
            produced = False
            for url in urls:
                if self._stop.is_set():
                    break
                try:
                    produced = self._consume(url) or produced
                except Exception:
                    logger.exception(
                        "realtime stream failed source=%s url=%s",
                        self.config.video_source_id,
                        url,
                    )
                if produced:
                    break
            if not self._stop.is_set():
                self._stop.wait(self._reconnect_seconds)

    def _consume(self, url: str) -> bool:
        decode_fps = max(
            rule.sample_fps(self._max_fps) for rule in self.config.rules
        )
        command = [
            self._ffmpeg_path,
            "-nostdin",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-analyzeduration",
            "1000000",
            "-probesize",
            "1000000",
            "-i",
            url,
            "-map",
            "0:v:0",
            "-vf",
            f"scale=w='min(iw,{self._frame_width})':h=-2,fps={decode_fps:g}",
            "-q:v",
            "4",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        with self._process_lock:
            self._process = process
        produced = False
        try:
            if process.stdout is None:
                return False
            for jpeg in iter_mjpeg(process.stdout, self._stop):
                produced = True
                self._sequence += 1
                frame = VideoFrame(
                    sequence=self._sequence,
                    captured_at=datetime.now(timezone.utc),
                    jpeg=jpeg,
                )
                now = time.monotonic()
                if now - self._last_ring_at >= self._ring_interval:
                    self.ring.add(frame)
                    self._last_ring_at = now
                self._dispatch(frame)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            with self._process_lock:
                if self._process is process:
                    self._process = None
        return produced

    def _dispatch(self, frame: VideoFrame) -> None:
        now = time.monotonic()
        for rule in self.config.rules:
            due = self._next_due.get(rule.algorithm_code, 0.0)
            if now < due:
                continue
            interval = 1.0 / rule.sample_fps(self._max_fps)
            self._next_due[rule.algorithm_code] = now + interval
            try:
                self._callback(self.config, rule, frame, self.ring)
            except Exception:
                logger.exception(
                    "realtime frame callback failed source=%s algorithm=%s",
                    self.config.video_source_id,
                    rule.algorithm_code,
                )


class StreamManager:
    def __init__(
        self,
        *,
        ffmpeg_path: str,
        max_fps: float,
        ring_seconds: int,
        reconnect_seconds: float,
        frame_width: int,
        ring_fps: float,
        callback: FrameCallback,
        remove_callback: RemoveCallback,
    ) -> None:
        self._ffmpeg_path = ffmpeg_path
        self._max_fps = max_fps
        self._ring_seconds = ring_seconds
        self._reconnect_seconds = reconnect_seconds
        self._frame_width = frame_width
        self._ring_fps = ring_fps
        self._callback = callback
        self._remove_callback = remove_callback
        self._sessions: dict[int, StreamSession] = {}
        self._lock = threading.Lock()

    def sync(self, configs: Iterable[StreamConfig]) -> None:
        desired = {config.video_source_id: config for config in configs if config.rules}
        removed: list[StreamSession] = []
        added: list[StreamSession] = []
        with self._lock:
            for source_id, session in list(self._sessions.items()):
                replacement = desired.get(source_id)
                if replacement != session.config:
                    removed.append(self._sessions.pop(source_id))
            for source_id, config in desired.items():
                if source_id in self._sessions:
                    continue
                session = StreamSession(
                    config,
                    ffmpeg_path=self._ffmpeg_path,
                    max_fps=self._max_fps,
                    ring_seconds=self._ring_seconds,
                    reconnect_seconds=self._reconnect_seconds,
                    frame_width=self._frame_width,
                    ring_fps=self._ring_fps,
                    callback=self._callback,
                )
                self._sessions[source_id] = session
                added.append(session)
        for session in removed:
            session.stop()
            self._remove_callback(session.config, session.ring)
        for session in added:
            session.start()

    def stop(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()
            self._remove_callback(session.config, session.ring)

    @property
    def active_streams(self) -> int:
        with self._lock:
            return len(self._sessions)


class RealtimeRuntime:
    def __init__(
        self,
        settings: Settings,
        client: MediaAPIClient,
        registry: AnalyzerRegistry,
        state: ServiceState,
    ) -> None:
        self._settings = settings
        self._client = client
        self._registry = registry
        self._state = state
        self._aggregator = EventAggregator()
        self._evidence = EventEvidenceWriter(
            settings.evidence_root,
            settings.ffmpeg_path,
            settings.ffmpeg_timeout_seconds,
            settings.event_pre_seconds,
            settings.event_post_seconds,
            settings.event_clip_fps,
        )
        self._event_executor = ThreadPoolExecutor(
            # Preserve open/close ordering for a given event. Event generation
            # is infrequent; inference remains independently parallel.
            max_workers=1,
            thread_name_prefix="event-evidence",
        )
        self._inference_executor = ThreadPoolExecutor(
            max_workers=settings.realtime_concurrency,
            thread_name_prefix="realtime-inference",
        )
        self._busy: set[tuple[int, str]] = set()
        self._busy_lock = threading.Lock()

    def handle_frame(
        self,
        stream: StreamConfig,
        rule: RealtimeRule,
        frame: VideoFrame,
        ring: FrameRingBuffer,
    ) -> None:
        key = (stream.video_source_id, rule.algorithm_code)
        with self._busy_lock:
            if key in self._busy:
                return
            self._busy.add(key)
        future = self._inference_executor.submit(
            self._analyze, stream, rule, frame, ring
        )
        future.add_done_callback(lambda _future: self._release(key))

    def _analyze(
        self,
        stream: StreamConfig,
        rule: RealtimeRule,
        frame: VideoFrame,
        ring: FrameRingBuffer,
    ) -> None:
        analyzer = self._registry.get_realtime(rule.algorithm_code)
        try:
            detections = analyzer.analyze_frame(frame, rule.config)
        except Exception as exc:
            logger.exception(
                "realtime analyzer failed source=%s algorithm=%s",
                stream.video_source_id,
                rule.algorithm_code,
            )
            self._state.update(realtime_last_error=str(exc))
            return
        self._state.increment("processed_frames")
        policy = EventPolicy.from_config(rule.config)
        if policy.clear_after_seconds < self._settings.event_post_seconds:
            policy = EventPolicy(
                min_hits=policy.min_hits,
                max_gap_seconds=policy.max_gap_seconds,
                clear_after_seconds=self._settings.event_post_seconds,
                cooldown_seconds=policy.cooldown_seconds,
            )
        events = self._aggregator.process(
            video_source_id=stream.video_source_id,
            stream_name=stream.stream_name,
            algorithm_code=rule.algorithm_code,
            model_version=analyzer.model_version,
            detections=detections,
            captured_at=frame.captured_at,
            policy=policy,
        )
        frames = ring.snapshot()
        for event in events:
            self._event_executor.submit(self._publish, event, frames)

    def _release(self, key: tuple[int, str]) -> None:
        with self._busy_lock:
            self._busy.discard(key)

    def remove_stream(self, stream: StreamConfig, ring: FrameRingBuffer) -> None:
        frames = ring.snapshot()
        for rule in stream.rules:
            for event in self._aggregator.flush_stream(
                stream.video_source_id, rule.algorithm_code
            ):
                self._event_executor.submit(self._publish, event, frames)

    def stop(self) -> None:
        self._inference_executor.shutdown(wait=True, cancel_futures=False)
        self._event_executor.shutdown(wait=True, cancel_futures=False)

    def _publish(self, event: AggregatedEvent, frames: list[VideoFrame]) -> None:
        try:
            snapshot_path, clip_path = self._evidence.write(event, frames)
            self._client.report_events(
                self._settings.worker_id,
                [event.api_payload(snapshot_path, clip_path)],
            )
            self._state.increment("emitted_events")
        except Exception as exc:
            logger.exception("failed to persist realtime event=%s", event.event_id)
            self._state.update(realtime_last_error=str(exc))


class RealtimeCoordinator:
    def __init__(
        self,
        settings: Settings,
        client: MediaAPIClient,
        registry: AnalyzerRegistry,
        state: ServiceState,
    ) -> None:
        self._settings = settings
        self._client = client
        self._registry = registry
        self._state = state
        self._runtime = RealtimeRuntime(settings, client, registry, state)
        self._manager = StreamManager(
            ffmpeg_path=settings.ffmpeg_path,
            max_fps=settings.realtime_max_fps,
            ring_seconds=settings.realtime_ring_seconds,
            reconnect_seconds=settings.realtime_reconnect_seconds,
            frame_width=settings.realtime_frame_width,
            ring_fps=settings.event_clip_fps,
            callback=self._runtime.handle_frame,
            remove_callback=self._runtime.remove_stream,
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="realtime-config", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(
            timeout=self._settings.request_timeout_seconds
            + self._settings.realtime_sync_seconds
            + 5
        )
        self._manager.stop()
        self._runtime.stop()
        self._state.update(active_streams=0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                response = self._client.get_realtime_config(
                    self._settings.worker_id,
                    self._registry.realtime_capabilities,
                )
                values = response.get("streams", [])
                if not isinstance(values, list):
                    raise MediaAPIError("realtime config streams is not a list")
                capabilities = set(self._registry.realtime_capabilities)
                configs: list[StreamConfig] = []
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    config = StreamConfig.from_payload(value)
                    rules = tuple(
                        rule
                        for rule in config.rules
                        if rule.algorithm_code in capabilities
                    )
                    if rules:
                        configs.append(
                            StreamConfig(
                                video_source_id=config.video_source_id,
                                customer_id=config.customer_id,
                                stream_name=config.stream_name,
                                input_url=config.input_url,
                                fallback_url=config.fallback_url,
                                rules=rules,
                            )
                        )
                self._manager.sync(configs)
                self._state.update(
                    active_streams=self._manager.active_streams,
                    realtime_last_sync_at=datetime.now(timezone.utc).isoformat(),
                    realtime_last_error="",
                )
            except Exception as exc:
                logger.warning("realtime config sync failed: %s", exc)
                self._state.update(realtime_last_error=str(exc))
            self._stop.wait(self._settings.realtime_sync_seconds)


def iter_mjpeg(stream: BinaryIO, stop: threading.Event) -> Iterable[bytes]:
    """Extract complete JPEG images from an FFmpeg image2pipe stream."""

    buffer = bytearray()
    while not stop.is_set():
        chunk = stream.read(64 * 1024)
        if not chunk:
            return
        buffer.extend(chunk)
        while True:
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                if len(buffer) > 1:
                    del buffer[:-1]
                break
            end = buffer.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start > 0:
                    del buffer[:start]
                if len(buffer) > 32 * 1024 * 1024:
                    raise RuntimeError("MJPEG frame exceeds 32MB")
                break
            yield bytes(buffer[start : end + 2])
            del buffer[: end + 2]


def _validated_stream_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"rtmp", "rtmps", "http", "https"} or not parsed.netloc:
        raise ValueError("realtime stream URL is invalid")
    return value
