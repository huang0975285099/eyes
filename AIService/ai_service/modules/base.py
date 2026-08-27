from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class AnalysisError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class AnalysisResult:
    frames: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Detection:
    """One realtime algorithm hit before temporal event aggregation."""

    event_type: str
    confidence: float
    key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VideoFrame:
    sequence: int
    captured_at: datetime
    jpeg: bytes


class Analyzer(ABC):
    code: str

    @abstractmethod
    def analyze(self, job: dict[str, Any]) -> AnalysisResult:
        raise NotImplementedError


class RealtimeAnalyzer(ABC):
    """A frame consumer. Stream ownership stays in the realtime core."""

    code: str
    model_version: str = ""

    @abstractmethod
    def analyze_frame(
        self, frame: VideoFrame, config: dict[str, Any]
    ) -> list[Detection]:
        raise NotImplementedError


class AnalyzerRegistry:
    def __init__(self) -> None:
        self._analyzers: dict[str, Analyzer] = {}
        self._realtime_analyzers: dict[str, RealtimeAnalyzer] = {}

    def register(self, analyzer: Analyzer) -> None:
        code = analyzer.code.strip().lower()
        if not code:
            raise ValueError("analyzer code cannot be empty")
        if code in self._analyzers:
            raise ValueError(f"analyzer {code!r} is already registered")
        self._analyzers[code] = analyzer

    def get(self, code: str) -> Analyzer:
        try:
            return self._analyzers[code]
        except KeyError as exc:
            raise AnalysisError(
                f"unsupported analyzer: {code}", retryable=False
            ) from exc

    def register_realtime(self, analyzer: RealtimeAnalyzer) -> None:
        code = analyzer.code.strip().lower()
        if not code:
            raise ValueError("realtime analyzer code cannot be empty")
        if code in self._realtime_analyzers:
            raise ValueError(f"realtime analyzer {code!r} is already registered")
        self._realtime_analyzers[code] = analyzer

    def get_realtime(self, code: str) -> RealtimeAnalyzer:
        try:
            return self._realtime_analyzers[code]
        except KeyError as exc:
            raise AnalysisError(
                f"unsupported realtime analyzer: {code}", retryable=False
            ) from exc

    @property
    def capabilities(self) -> list[str]:
        return sorted(self._analyzers)

    @property
    def realtime_capabilities(self) -> list[str]:
        return sorted(self._realtime_analyzers)
