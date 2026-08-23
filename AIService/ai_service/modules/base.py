from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class AnalysisError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class AnalysisResult:
    frames: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class Analyzer(ABC):
    code: str

    @abstractmethod
    def analyze(self, job: dict[str, Any]) -> AnalysisResult:
        raise NotImplementedError


class AnalyzerRegistry:
    def __init__(self) -> None:
        self._analyzers: dict[str, Analyzer] = {}

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

    @property
    def capabilities(self) -> list[str]:
        return sorted(self._analyzers)
