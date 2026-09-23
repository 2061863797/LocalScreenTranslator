# -*- coding: utf-8 -*-
"""不依赖 Qt 的一次性与持续翻译业务状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import threading
import time
from typing import Any, Callable

import numpy as np

from .ocr_engine import OcrEngine, OcrLine
from .translator import Translator


@dataclass(frozen=True)
class WatchCycleContext:
    generation_id: int
    timestamp: float
    target_language: str


class GenerationTracker:
    """Thread-safe monotonic generation tracker for async OCR and translation cycles.

    Guarantees:
    - next_generation(): Monotonically increments and returns new active generation ID.
    - is_active(gen_id): Returns True if gen_id matches current active generation.
    - reset(): Advances the active generation to invalidate all in-flight generations.
    - dispatch_if_active(): Invokes callback only if generation ID is still active.
    """

    def __init__(self, initial_generation: int = 0):
        self._lock = threading.Lock()
        self._current_gen: int = initial_generation

    def next_generation(self) -> int:
        """Monotonically increments and returns new generation ID."""
        with self._lock:
            self._current_gen += 1
            return self._current_gen

    def next_generation_if_active(self, expected: int) -> int | None:
        """仅在处理期间未被重置时原子地推进世代。"""
        with self._lock:
            if self._current_gen != expected:
                return None
            self._current_gen += 1
            return self._current_gen

    def is_active(self, gen_id: int) -> bool:
        """Returns True if gen_id matches current active generation."""
        with self._lock:
            return gen_id == self._current_gen

    def reset(self, value: int | None = None) -> int:
        """Resets and returns new active generation.
        If value is specified, sets to that value; otherwise increments to invalidate prior gens.
        """
        with self._lock:
            if value is not None:
                self._current_gen = value
            else:
                self._current_gen += 1
            return self._current_gen

    @property
    def current_generation(self) -> int:
        with self._lock:
            return self._current_gen

    def dispatch_if_active(
        self, gen_id: int, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> bool:
        """Execute callback only if gen_id matches current active generation.
        Returns True if executed, False if dropped.
        """
        if self.is_active(gen_id):
            callback(*args, **kwargs)
            return True
        return False



@dataclass(frozen=True)
class OneShotResult:
    source: str
    translation: str
    lines: tuple[OcrLine, ...] = ()


class OneShotPipeline:
    """输入图片或文本并返回统一结果；线程与信号由调用方负责。"""

    def __init__(self, ocr: OcrEngine, translator: Translator):
        self._ocr = ocr
        self._translator = translator

    def run(
        self,
        *,
        image: np.ndarray | None = None,
        text: str | None = None,
        translate: bool,
        target_language: str,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> OneShotResult | None:
        if cancelled():
            return None
        lines: tuple[OcrLine, ...] = ()
        if text is None:
            lines = tuple(self._ocr.recognize(image))
            source = OcrEngine.lines_to_text(list(lines))
        else:
            source = text.strip()
        if cancelled():
            return None
        translation = self._translator.translate(source, target_language) if translate and source else ""
        if cancelled():
            return None
        return OneShotResult(source, translation, lines)


@dataclass
class LiveTranslationState:
    """持续 OCR 的纯状态机：变化判定、空帧清除和逐行译文缓存。"""

    last_text: str = ""
    empty_frames: int = 0
    line_cache: dict[str, str] = field(default_factory=dict)

    def reset(self, *, clear_cache: bool = True) -> None:
        self.last_text = ""
        self.empty_frames = 0
        if clear_cache:
            self.line_cache.clear()

    def observe(self, lines: list[OcrLine], threshold: float) -> tuple[str, str]:
        text = "\n".join(line.text for line in lines).strip()
        if not text:
            self.empty_frames += 1
            if self.empty_frames >= 2 and self.last_text:
                self.last_text = ""
                return "clear", ""
            return "none", ""
        self.empty_frames = 0
        if text == self.last_text:
            return "none", text
        l1, l2 = len(self.last_text), len(text)
        # 数学理论上限短路：ratio <= 2 * min(l1, l2) / (l1 + l2)。
        # 当理论上限小于阈值时，必定不满足匹配，跳过昂贵的 LCS 动态规划
        if (l1 + l2 > 0) and (2.0 * min(l1, l2) / (l1 + l2) < threshold):
            self.last_text = text
            return "change", text
        if SequenceMatcher(None, self.last_text, text).ratio() >= threshold:
            return "none", text
        self.last_text = text
        return "change", text

    def prune_cache(self, active_lines: list[str], limit: int = 400) -> None:
        if len(self.line_cache) > limit:
            active = set(active_lines)
            self.line_cache = {key: value for key, value in self.line_cache.items() if key in active}
