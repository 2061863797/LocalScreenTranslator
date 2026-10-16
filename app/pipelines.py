# -*- coding: utf-8 -*-
"""不依赖 Qt 的一次性与持续翻译业务状态。"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable

import numpy as np

from .ocr_engine import OcrEngine, OcrLine
from .ocr_service import OcrService
from .replacement import ReplacementPayload, build_payload, group_blocks, join_block_text
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
    # 图片输入时的原位替换 payload（含冻结底图）；未要求或纯文本输入时为 None
    replacement: ReplacementPayload | None = None
    # 图片输入的段落及其译文：结果窗口切到原位替换时据此补建 payload
    blocks: tuple[tuple[OcrLine, ...], ...] = ()
    block_translations: tuple[str, ...] = ()


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
        replacement: bool = False,
    ) -> OneShotResult | None:
        """replacement=True 时顺带生成原位替换 payload（取色、修补较耗时，按需才做）。"""
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
        payload = None
        blocks: list[list[OcrLine]] = []
        parts: list[str] = []
        if translate and source:
            if lines:
                # 图片按段落翻译：结果窗口显示整段译文，也能随时切到原位替换
                blocks = group_blocks(lines, image)
                parts = []
                for block in blocks:
                    if cancelled():
                        return None
                    block_source = join_block_text(line.text for line in block)
                    value = self._translator.translate(
                        block_source, target_language, session_tag="one_shot"
                    )
                    parts.append(str(value or "").strip() or block_source)
                translation = "\n".join(parts)
                if replacement:
                    payload = build_payload(image, list(zip(blocks, parts)), frozen=True)
            else:
                try:
                    translation = self._translator.translate(
                        source, target_language, session_tag="one_shot"
                    )
                except TypeError:
                    translation = self._translator.translate(source, target_language)
        if cancelled():
            return None
        return OneShotResult(
            source, translation, lines, payload,
            tuple(tuple(block) for block in blocks), tuple(parts),
        )


