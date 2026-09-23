# -*- coding: utf-8 -*-
"""文本变化与状态机检测器 (PR 9).

观察 OCR 识别行，执行 SequenceMatcher 相似度比对，并追踪稳定、变化与清空状态。
"""

from __future__ import annotations

from difflib import SequenceMatcher
import threading
import time
from typing import Any, Callable, List, Optional, Tuple


class TextChangeDetector:
    """OCR 识别文本变化观察与状态检测器。

    职责：
    1. 观察当前 OCR 行，提取并规范化文本；
    2. 连续两轮（默认阈值）无文字且达到可选保留期时判定为 "clear"；
    3. 利用数学理论上限短路加速 SequenceMatcher 相似度比对；
    4. 相似度低于阈值时判定为 "change"，否则判定为 "none"；
    5. 维护逐行翻译缓存并支持定期修剪。
    """

    def __init__(
        self,
        empty_clear_threshold: int = 2,
        candidate_confirm_frames: int = 2,
        empty_clear_delay_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.empty_clear_threshold = max(1, int(empty_clear_threshold))
        self.candidate_confirm_frames = max(1, int(candidate_confirm_frames))
        self.empty_clear_delay_s = max(0.0, float(empty_clear_delay_s))
        self._clock = clock
        self._lock = threading.RLock()
        self._last_text: str = ""
        self._candidate_text: str = ""
        self._candidate_count: int = 0
        self._empty_frames: int = 0
        self._empty_started_at: float | None = None
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
    def candidate_text(self) -> str:
        with self._lock:
            return self._candidate_text

    @property
    def candidate_count(self) -> int:
        with self._lock:
            return self._candidate_count

    @property
    def empty_frames(self) -> int:
        with self._lock:
            return self._empty_frames

    @empty_frames.setter
    def empty_frames(self, value: int) -> None:
        with self._lock:
            self._empty_frames = int(value)
            self._empty_started_at = self._clock() if self._empty_frames > 0 else None

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

    @property
    def has_pending_candidate(self) -> bool:
        """是否有正在等待确认的候选文本或待确认的清空帧。"""
        with self._lock:
            return self._candidate_count > 0 or (
                bool(self._last_text) and self._empty_frames > 0
            )

    def reset(self, *, clear_cache: bool = True) -> None:
        """重置状态机。若 clear_cache 为 True 则同时清空逐行缓存。"""
        with self._lock:
            self._last_text = ""
            self._candidate_text = ""
            self._candidate_count = 0
            self._empty_frames = 0
            self._empty_started_at = None
            if clear_cache:
                self._line_cache.clear()

    def rollback(self, text: str | None = None) -> None:
        """回滚状态（派发失败或因世代过期丢弃时调用），允许后续帧重新发起翻译。"""
        with self._lock:
            if text is None or self._last_text == text:
                self._last_text = ""
                self._candidate_text = ""
                self._candidate_count = 0
                self._empty_started_at = None

    def observe(
        self,
        lines: list[Any] | str,
        threshold: float = 0.5,
        *,
        exact_change: bool = False,
    ) -> tuple[str, str]:
        """观察新一轮 OCR 结果并判断状态事件。

        Args:
            lines: OCR 识别行列表（各项包含 .text 属性或为 str），或单一多行文本。
            threshold: 相似度判定阈值（0.0 ~ 1.0）。
            exact_change: 是否启用精确文本变动（只要 text != last_text 即视为变动，不进入两帧相似度候选确认）。

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
                now = self._clock()
                if self._empty_started_at is None:
                    self._empty_started_at = now
                self._empty_frames += 1
                self._candidate_text = ""
                self._candidate_count = 0
                if (
                    self._empty_frames >= self.empty_clear_threshold
                    and self._last_text
                    and now - self._empty_started_at >= self.empty_clear_delay_s
                ):
                    self._last_text = ""
                    self._empty_started_at = None
                    return "clear", ""
                return "none", ""

            self._empty_frames = 0
            self._empty_started_at = None
            if text == self._last_text:
                self._candidate_text = ""
                self._candidate_count = 0
                return "none", text

            # 显式精确变动（如字幕模式已具备前置 OcrStabilizer）、threshold >= 1.0 或首帧建立基准：
            # 只要文本不相等，立即触发变动，不进入相似度候选确认，彻底消除首帧与字幕更新延迟！
            if exact_change or threshold >= 1.0 or not self._last_text:
                self._last_text = text
                self._candidate_text = ""
                self._candidate_count = 0
                return "change", text

            l1, l2 = len(self._last_text), len(text)
            # 1. 数学理论上限短路：ratio <= 2 * min(l1, l2) / (l1 + l2)。
            # 当理论上限小于阈值时必定为实质变化，跳过昂贵的 LCS 动态规划
            if (l1 + l2 > 0) and (2.0 * min(l1, l2) / (l1 + l2) < threshold):
                self._last_text = text
                self._candidate_text = ""
                self._candidate_count = 0
                return "change", text

            # 2. 精确比对相似度
            ratio = SequenceMatcher(None, self._last_text, text).ratio()
            if ratio < threshold:
                self._last_text = text
                self._candidate_text = ""
                self._candidate_count = 0
                return "change", text

            # 3. 相似度 >= threshold 但文本 != last_text（如数字微变、HUD）：
            # 采用候选帧累积晋升机制，连续稳定出现后晋升确认，绝不永久丢弃！
            if text == self._candidate_text:
                self._candidate_count += 1
                if self._candidate_count >= self.candidate_confirm_frames:
                    self._last_text = text
                    self._candidate_text = ""
                    self._candidate_count = 0
                    return "change", text
                return "none", self._last_text
            else:
                self._candidate_text = text
                self._candidate_count = 1
                if self.candidate_confirm_frames <= 1:
                    self._last_text = text
                    self._candidate_text = ""
                    self._candidate_count = 0
                    return "change", text
                return "none", self._last_text

    def prune_cache(self, active_lines: list[str], limit: int = 400) -> None:
        """修剪逐行译文缓存，保留活跃行，限制最大条目数。"""
        with self._lock:
            if len(self._line_cache) > limit:
                active = set(active_lines)
                self._line_cache = {
                    k: v for k, v in self._line_cache.items() if k in active
                }
