# -*- coding: utf-8 -*-
"""文本变化与状态机检测器 (PR 9).

观察 OCR 识别行，执行 SequenceMatcher 相似度比对，并追踪稳定、变化与清空状态。
"""

from __future__ import annotations

from difflib import SequenceMatcher
import threading
from typing import Any, List, Optional, Tuple


class TextChangeDetector:
    """OCR 识别文本变化观察与状态检测器。

    职责：
    1. 观察当前 OCR 行，提取并规范化文本；
    2. 连续两轮（默认阈值）无文字时判定为 "clear"；
    3. 利用数学理论上限短路加速 SequenceMatcher 相似度比对；
    4. 相似度低于阈值时判定为 "change"，否则判定为 "none"；
    5. 维护逐行翻译缓存并支持定期修剪。
    """

    def __init__(self, empty_clear_threshold: int = 2) -> None:
        self.empty_clear_threshold = max(1, int(empty_clear_threshold))
        self._lock = threading.RLock()
        self._last_text: str = ""
        self._empty_frames: int = 0
        self._line_cache: dict[str, str] = {}

    @property
    def last_text(self) -> str:
        with self._lock:
            return self._last_text

    @last_text.setter
    def last_text(self, value: str) -> None:
        with self._lock:
            self._last_text = str(value)

    @property
    def empty_frames(self) -> int:
        with self._lock:
            return self._empty_frames

    @empty_frames.setter
    def empty_frames(self, value: int) -> None:
        with self._lock:
            self._empty_frames = int(value)

    @property
    def line_cache(self) -> dict[str, str]:
        with self._lock:
            return self._line_cache

    @line_cache.setter
    def line_cache(self, value: dict[str, str]) -> None:
        with self._lock:
            self._line_cache = dict(value)

    @property
    def is_stable(self) -> bool:
        """当前是否处于有稳定文本且无连续空帧的状态。"""
        with self._lock:
            return bool(self._last_text) and self._empty_frames == 0

    def reset(self, *, clear_cache: bool = True) -> None:
        """重置状态机。若 clear_cache 为 True 则同时清空逐行缓存。"""
        with self._lock:
            self._last_text = ""
            self._empty_frames = 0
            if clear_cache:
                self._line_cache.clear()

    def observe(self, lines: list[Any] | str, threshold: float = 0.5) -> tuple[str, str]:
        """观察新一轮 OCR 结果并判断状态事件。

        Args:
            lines: OCR 识别行列表（各项包含 .text 属性或为 str），或单一多行文本。
            threshold: 相似度判定阈值（0.0 ~ 1.0）。若相似度 >= threshold 则判定无实质变化。

        Returns:
            (event, text):
            event 为 "none" | "change" | "clear"；
            text 为当前帧有效原文或清空后的空串。
        """
        if isinstance(lines, str):
            text = lines.strip()
        elif isinstance(lines, list):
            parts = []
            for item in lines:
                if hasattr(item, "text"):
                    parts.append(str(item.text))
                else:
                    parts.append(str(item))
            text = "\n".join(parts).strip()
        else:
            text = str(lines).strip()

        with self._lock:
            if not text:
                self._empty_frames += 1
                if self._empty_frames >= self.empty_clear_threshold and self._last_text:
                    self._last_text = ""
                    return "clear", ""
                return "none", ""

            self._empty_frames = 0
            if text == self._last_text:
                return "none", text

            l1, l2 = len(self._last_text), len(text)
            # 数学理论上限短路：ratio <= 2 * min(l1, l2) / (l1 + l2)。
            # 当理论上限小于阈值时，必定不满足匹配，跳过昂贵的 LCS 动态规划
            if (l1 + l2 > 0) and (2.0 * min(l1, l2) / (l1 + l2) < threshold):
                self._last_text = text
                return "change", text

            ratio = SequenceMatcher(None, self._last_text, text).ratio()
            if ratio >= threshold:
                return "none", text

            self._last_text = text
            return "change", text

    def prune_cache(self, active_lines: list[str], limit: int = 400) -> None:
        """修剪逐行译文缓存，保留活跃行，限制最大条目数。"""
        with self._lock:
            if len(self._line_cache) > limit:
                active = set(active_lines)
                self._line_cache = {
                    k: v for k, v in self._line_cache.items() if k in active
                }
