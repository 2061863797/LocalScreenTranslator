# -*- coding: utf-8 -*-
"""不依赖 Qt 的一次性与持续翻译业务状态。"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable

import numpy as np

from .ocr_engine import OcrEngine, OcrLine
from .ocr_service import OcrService
from .translation_runtime.router import TranslationRouter


class GenerationTracker:
    """Thread-safe monotonic generation tracker for async OCR and translation cycles.

    Guarantees:
    - next_generation(): Monotonically increments and returns new active generation ID.
    - next_generation_if_active(expected): Advances only if `expected` is still active.
    - is_active(gen_id): Returns True if gen_id matches current active generation.
    - reset(): Advances the active generation to invalidate all in-flight generations.
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

@dataclass(frozen=True)
class OneShotResult:
    source: str
    translation: str
    lines: tuple[OcrLine, ...] = ()


class OneShotPipeline:
    """输入图片或文本并返回统一结果；线程与信号由调用方负责。"""

    def __init__(self, ocr: OcrEngine, translator: TranslationRouter):
        self._ocr = ocr
        self._ocr_service = OcrService(ocr_engine=ocr)
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
            lines = tuple(self._ocr_service.recognize_frame(image))
            source = "\n".join(line.text for line in lines).strip()
        else:
            source = text.strip()
        if cancelled():
            return None
        translation = ""
        if translate and source:
            if lines and hasattr(self._translator, "translate_lines"):
                values = self._translator.translate_lines(
                    [line.text for line in lines], target_language,
                    session_tag="one_shot",
                )
                valid_values = [v.strip() for v in values if v and v.strip()]
                translation = "\n".join(valid_values) if valid_values else ""
            else:
                try:
                    translation = self._translator.translate(
                        source, target_language, session_tag="one_shot"
                    )
                except TypeError:
                    translation = self._translator.translate(source, target_language)
        if cancelled():
            return None
        return OneShotResult(source, translation, lines)


