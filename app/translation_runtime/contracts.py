"""翻译边界只使用基础数据；后端不依赖截图、OCR 或 Qt。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class TranslationContextError(ValueError):
    """提示本身超过上下文容量，可由路由拆分原文后重试。"""


class IncompleteTranslationError(RuntimeError):
    """部分非空原文没有译文，不能作为完整结果派发或缓存。"""


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


