# -*- coding: utf-8 -*-
"""翻译管理器与增量翻译器 (PR 7 & PR 9)。

功能说明：
- SubtitleIncrementalTranslator:
  维护行级差异，对未变化的行直接从 TranslationCache (L1/L2) 复用，
  只将新增或变动的行批量送入翻译模型，随后严格按原始行序组装回完整字幕文本。
- TranslationManager:
  翻译管线调度器，协调字幕增量翻译、逐行标注增量翻译与一次性直译。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, List, Optional, Tuple

from .textlang import is_already_target_language
from .translation_cache import TranslationCache

_log = logging.getLogger("st.trans_mgr")

# 备注缓存哨兵：已是目标语，跳过翻译且不叠标签
_SKIP_TARGET = "\x00SKIP_TARGET"


class SubtitleIncrementalTranslator:
    """字幕增量翻译器 (PR 7)。

    以行为最小比对单元：
    1. 过滤空行并对有效行逐行查询两级缓存 (L1 内存 / L2 SQLite)；
    2. 仅对未命中或发生变动的行进行批量翻译；
    3. 将翻译得到的新译文存入两级缓存；
    4. 严格按照输入原始行顺序，重组为换行拼接的完整字幕文本。
    """

    def __init__(
        self,
        translator_or_cache: Any = None,
        *,
        translator: Any = None,
        cache: TranslationCache | None = None,
        mock_translate_fn: Callable[[list[str], str], list[str]] | None = None,
        model_id: str = "default_model",
        prompt_version: str = "v1",
    ):
        self.mock_translate_fn = mock_translate_fn
        self.model_id = model_id
        self.prompt_version = prompt_version
        self.llm_calls_count: int = 0
        self.lines_translated_count: int = 0

        self.translator = None
        self.cache: TranslationCache | None = None

        if isinstance(translator_or_cache, TranslationCache):
            self.cache = translator_or_cache
        elif translator_or_cache is not None:
            if hasattr(translator_or_cache, "cache") and isinstance(translator_or_cache.cache, TranslationCache):
                self.cache = translator_or_cache.cache
                self.translator = translator_or_cache
            elif hasattr(translator_or_cache, "get") and hasattr(translator_or_cache, "put"):
                self.cache = translator_or_cache
            else:
                self.translator = translator_or_cache

        if translator is not None:
            self.translator = translator
            if self.cache is None and hasattr(translator, "cache"):
                self.cache = translator.cache

        if cache is not None:
            self.cache = cache

        if self.cache is None:
            self.cache = TranslationCache()

        if hasattr(self.translator, "model_id") and self.model_id == "default_model":
            self.model_id = str(getattr(self.translator, "model_id"))
        if hasattr(self.translator, "prompt_version") and self.prompt_version == "v1":
            self.prompt_version = str(getattr(self.translator, "prompt_version"))

    def _batch_translate(self, lines: list[str], target_language: str) -> list[str]:
        """批量翻译未命中缓存的行。"""
        self.llm_calls_count += 1
        self.lines_translated_count += len(lines)

        if self.mock_translate_fn is not None:
            return self.mock_translate_fn(lines, target_language)

        if self.translator is not None:
            if hasattr(self.translator, "translate_lines"):
                try:
                    return self.translator.translate_lines(lines, target_language, session_tag="watcher")
                except TypeError:
                    return self.translator.translate_lines(lines, target_language)
            elif hasattr(self.translator, "translate"):
                try:
                    return [self.translator.translate(line, target_language, session_tag="watcher") for line in lines]
                except TypeError:
                    return [self.translator.translate(line, target_language) for line in lines]

        # 默认回退（用于独立/测试环境）
        return [f"[{target_language}]{line}" for line in lines]

    def translate_subtitle_lines(
        self,
        current_lines: list[str],
        target_language: str,
        model_id: str | None = None,
        prompt_version: str | None = None,
    ) -> tuple[str, bool]:
        """增量翻译多行字幕。

        参数:
            current_lines: 当前提取的各行文本列表。
            target_language: 目标语言。
            model_id: 可选覆盖模型标识。
            prompt_version: 可选覆盖提示词版本。

        返回:
            (full_subtitle, used_cache)
            full_subtitle: 严格按原行序拼接的译文（换行分隔）。
            used_cache: 本次是否复用了缓存（命中数 > 0 且存在未命中，或者部分复用）。
        """
        if not current_lines:
            return "", False

        clean_lines = [line.strip() for line in current_lines if line.strip()]
        if not clean_lines:
            return "", False

        mid = model_id or self.model_id
        pver = prompt_version or self.prompt_version

        resolved: list[str | None] = [None] * len(clean_lines)
        miss_indices: list[int] = []
        miss_lines: list[str] = []

        # 逐行检查缓存 (L1 / L2)
        for i, line in enumerate(clean_lines):
            cached = self.cache.get(line, target_language, mid, pver)
            if cached is not None:
                resolved[i] = cached
            else:
                miss_indices.append(i)
                miss_lines.append(line)

        used_cache = len(miss_lines) < len(clean_lines)

        # 批量翻译未命中的行
        if miss_lines:
            translated = self._batch_translate(miss_lines, target_language)
            for idx, line, trans in zip(miss_indices, miss_lines, translated):
                resolved[idx] = trans
                self.cache.put(line, target_language, mid, pver, trans)

        full_subtitle = "\n".join(str(r) for r in resolved)
        return full_subtitle, used_cache


class TranslationManager:
    """管线翻译管理器，统一调度字幕增量翻译、逐行标注翻译与其它模式翻译 (PR 9)。"""

    def __init__(self, translator: Any, cache: TranslationCache | None = None):
        self.translator = translator
        self.cache = cache or getattr(translator, "cache", None) or TranslationCache()
        self.subtitle_translator = SubtitleIncrementalTranslator(self.translator, cache=self.cache)
        self._line_cache: dict[str, str] = {}
        self._cache_lock = threading.RLock()

    @property
    def line_cache(self) -> dict[str, str]:
        with self._cache_lock:
            return self._line_cache

    @line_cache.setter
    def line_cache(self, value: dict[str, str]) -> None:
        with self._cache_lock:
            self._line_cache = dict(value)

    def clear_cache(self) -> None:
        """清空标注行缓存。"""
        with self._cache_lock:
            self._line_cache.clear()

    def prune_cache(self, active_lines: list[str], limit: int = 400) -> None:
        """修剪行缓存。"""
        with self._cache_lock:
            if len(self._line_cache) > limit:
                active = set(active_lines)
                self._line_cache = {
                    k: v for k, v in self._line_cache.items()
                    if (k[0] if isinstance(k, tuple) else k) in active
                }

    def translate_subtitle_lines(
        self,
        current_lines: list[str],
        target_language: str,
        model_id: str | None = None,
        prompt_version: str | None = None,
    ) -> tuple[str, bool]:
        """增量翻译多行字幕。"""
        return self.subtitle_translator.translate_subtitle_lines(
            current_lines, target_language, model_id=model_id, prompt_version=prompt_version
        )

    def translate_subtitle(
        self,
        text_or_lines: str | list[str] | list[Any],
        target_language: str,
        gen_id: int | None = None,
    ) -> str:
        """翻译字幕文本（支持单字符串或行列表，按行增量缓存复用）。"""
        if not text_or_lines:
            return ""

        if isinstance(text_or_lines, str):
            raw_text = text_or_lines.strip()
            if not raw_text:
                return ""
            # 持续字幕模式下输入为视觉段落，优先保证跨行自然语意连贯性与两级持久化缓存
            if self.translator is not None and hasattr(self.translator, "translate"):
                try:
                    return str(self.translator.translate(raw_text, target_language, session_tag="watcher"))
                except TypeError:
                    return str(self.translator.translate(raw_text, target_language))
            lines = [line.strip() for line in text_or_lines.splitlines() if line.strip()]
            sub, _ = self.subtitle_translator.translate_subtitle_lines(lines, target_language)
            return sub
        elif isinstance(text_or_lines, list):
            clean_lines = [
                ln.text.strip() if hasattr(ln, "text") else str(ln).strip()
                for ln in text_or_lines
            ]
            clean_lines = [s for s in clean_lines if s]
            sub, _ = self.subtitle_translator.translate_subtitle_lines(clean_lines, target_language)
            return sub

        return ""

    def translate_annotations(
        self,
        lines: list[Any],
        target_language: str,
        gen_id: int | None = None,
        *,
        skip_target: bool = False,
        is_running_fn: Callable[[], bool] | None = None,
    ) -> tuple[list[tuple[Any, str]], str]:
        """备注模式：按行增量翻译，稳定原文走缓存，只请求变化行。

        支持可选跳过已是目标语言的行（不调模型、不叠标签）。

        Args:
            lines: OCR 识别行对象列表（每个具有 .text 和 .box 属性）。
            target_language: 目标语言。
            gen_id: 可选当前世代标识。
            skip_target: 是否跳过已是目标语言的文本行。
            is_running_fn: 可选运行状态检查函数。

        Returns:
            (items, joined_translation):
            items 为 [(box, 译文), ...]；
            joined_translation 为拼接后的完整译文字符串。
        """
        srcs = [
            ln.text.strip() if hasattr(ln, "text") else str(ln).strip()
            for ln in lines
        ]
        todo_text: list[str] = []
        skipped = 0

        with self._cache_lock:
            cache = self._line_cache
            for s in srcs:
                if not s:
                    continue
                cache_key = (s, target_language)
                if cache_key in cache or s in cache:
                    continue
                if skip_target and is_already_target_language(s, target_language):
                    cache[cache_key] = _SKIP_TARGET
                    skipped += 1
                    continue
                todo_text.append(s)

        can_run = is_running_fn() if is_running_fn is not None else True
        if todo_text and can_run:
            unique = list(dict.fromkeys(todo_text))
            if self.translator is not None and hasattr(self.translator, "translate_lines"):
                try:
                    trs = self.translator.translate_lines(unique, target_language, session_tag="watcher")
                except TypeError:
                    trs = self.translator.translate_lines(unique, target_language)
            elif self.translator is not None and hasattr(self.translator, "translate"):
                try:
                    trs = [self.translator.translate(u, target_language, session_tag="watcher") for u in unique]
                except TypeError:
                    trs = [self.translator.translate(u, target_language) for u in unique]
            else:
                trs = [f"[{target_language}]{u}" for u in unique]

            with self._cache_lock:
                for s, tr in zip(unique, trs):
                    if tr:
                        self._line_cache[(s, target_language)] = tr
                self.prune_cache(srcs)

        items: list[tuple[Any, str]] = []
        parts: list[str] = []

        with self._cache_lock:
            cache = self._line_cache
            for ln, s in zip(lines, srcs):
                if not s:
                    continue
                tr = cache.get((s, target_language))
                if tr is None:
                    tr = cache.get(s, "")
                if not tr or tr == _SKIP_TARGET:
                    continue
                box = ln.box if hasattr(ln, "box") else getattr(ln, "box", None)
                items.append((box, tr))
                parts.append(tr)
            cache_size = len(cache)

        _log.info(
            "备注增量译 total=%d new=%d skip_target=%d cache=%d skip_on=%s",
            len([s for s in srcs if s]),
            len(todo_text),
            skipped,
            cache_size,
            skip_target,
        )
        return items, "\n".join(parts)

    def translate_one_shot(self, source: str, target_language: str) -> str:
        """单次文本直译。"""
        if not source or not source.strip():
            return ""
        src = source.strip()
        if self.translator is not None and hasattr(self.translator, "translate"):
            return str(self.translator.translate(src, target_language))
        sub, _ = self.subtitle_translator.translate_subtitle_lines([src], target_language)
        return sub
