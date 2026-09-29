"""翻译边界只使用基础数据；后端不依赖截图、OCR 或 Qt。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class TranslationRequest:
    text: str
    source_language: str
    target_language: str
    mode: str
    model_id: str
    session_version: int
    cancelled: Callable[[], bool]


@dataclass(frozen=True)
class TranslationResult:
    text: str
    source_language: str
    target_language: str
    mode: str
    model_id: str
    session_version: int


class TranslationBackend(Protocol):
    def preload(self, model_path: str, device: str) -> None: ...

    def translate(self, request: TranslationRequest) -> TranslationResult: ...

    def close(self) -> None: ...
