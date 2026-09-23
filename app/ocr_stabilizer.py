# -*- coding: utf-8 -*-
"""OCR Jitter Stabilizer for suppressing optical character flipping.

PR 6: OCR Stabilizer.
Employs a sliding 2-frame debounce window and Levenshtein edit-distance check
(max_jitter_dist=2) to suppress single-frame character flipping optical jitter
from triggering expensive downstream LLM translation calls.
"""

from __future__ import annotations

import threading
from typing import Any


def levenshtein_distance(s1: str, s2: str, max_dist: int | None = None) -> int:
    """Compute Levenshtein edit distance between two strings with optional early exit.

    Optimized with fast identity check, fast length bound, common prefix/suffix
    stripping, and banded DP to eliminate quadratic O(N*M) stalls on long text blocks.
    If max_dist is provided and the distance exceeds max_dist, returns max_dist + 1.
    """
    if s1 == s2:
        return 0

    if max_dist is not None and abs(len(s1) - len(s2)) > max_dist:
        return max_dist + 1

    # Strip common prefix
    start = 0
    l1, l2 = len(s1), len(s2)
    min_len = min(l1, l2)
    while start < min_len and s1[start] == s2[start]:
        start += 1

    # Strip common suffix
    s1_end = l1 - 1
    s2_end = l2 - 1
    while s1_end >= start and s2_end >= start and s1[s1_end] == s2[s2_end]:
        s1_end -= 1
        s2_end -= 1

    s1_trimmed = s1[start : s1_end + 1]
    s2_trimmed = s2[start : s2_end + 1]

    if not s1_trimmed:
        return len(s2_trimmed)
    if not s2_trimmed:
        return len(s1_trimmed)

    m = len(s1_trimmed)
    n = len(s2_trimmed)
    if max_dist is not None and abs(m - n) > max_dist:
        return max_dist + 1

    if max_dist is None:
        if m < n:
            s1_trimmed, s2_trimmed = s2_trimmed, s1_trimmed
            m, n = n, m
        prev = list(range(n + 1))
        for c1 in s1_trimmed:
            curr = [prev[0] + 1]
            for j, c2 in enumerate(s2_trimmed):
                ins = prev[j + 1] + 1
                delete = curr[j] + 1
                sub = prev[j] + (c1 != c2)
                curr.append(min(ins, delete, sub))
            prev = curr
        return prev[-1]

    # Banded DP with threshold max_dist
    k = max_dist
    inf = k + 1
    prev = [j if j <= k else inf for j in range(n + 1)]
    for i in range(1, m + 1):
        c1 = s1_trimmed[i - 1]
        curr = [inf] * (n + 1)
        j_min = max(1, i - k)
        j_max = min(n, i + k)
        if i <= k:
            curr[0] = i
        row_min = curr[0]
        for j in range(j_min, j_max + 1):
            c2 = s2_trimmed[j - 1]
            cost = 0 if c1 == c2 else 1
            val = min(
                prev[j] + 1,        # deletion
                curr[j - 1] + 1,    # insertion
                prev[j - 1] + cost, # substitution/match
            )
            curr[j] = val
            if val < row_min:
                row_min = val
        if row_min > k:
            return k + 1
        prev = curr

    res = prev[n]
    return res if res <= k else k + 1


class OcrStabilizer:
    """Temporal debounce and edit-distance jitter filter for OCR lines."""

    def __init__(self, debounce_frames: int = 2, max_jitter_dist: int = 2) -> None:
        self.debounce_frames = max(1, int(debounce_frames))
        self.max_jitter_dist = int(max_jitter_dist)

        self._lock = threading.Lock()
        self._stable_lines: list[Any] | None = None
        self._candidate_lines: list[Any] = []
        self._candidate_repeat_count: int = 0
        self._last_change_was_debounced: bool = False

    @property
    def has_pending_candidate(self) -> bool:
        """是否有正在等待防抖确认的候选文本。"""
        with self._lock:
            return self._candidate_repeat_count > 0

    @property
    def last_change_was_debounced(self) -> bool:
        """最近一次 process 是否晋升了连续两帧相同的细小文字变化。"""
        with self._lock:
            return self._last_change_was_debounced

    def _extract_text(self, lines: list[Any]) -> str:
        """Extract joined text from list of OCR lines or strings."""
        parts: list[str] = []
        for line in lines:
            if hasattr(line, "text"):
                parts.append(str(line.text))
            else:
                parts.append(str(line))
        return " ".join(parts)

    def process(self, lines: list[Any]) -> tuple[bool, list[Any]]:
        """Process new OCR lines through temporal debounce and jitter filter.

        Args:
            lines: Current cycle OCR recognition lines.

        Returns:
            Tuple of (is_substantive_change, stabilized_lines).
        """
        with self._lock:
            self._last_change_was_debounced = False
            if self._stable_lines is None:
                # Initial state: first frame establishes stable baseline
                self._stable_lines = list(lines)
                return True, self._stable_lines

            # Handle empty input lines
            if not lines:
                if not self._stable_lines:
                    # Consecutive empty frames: remain quiet, no substantive change
                    return False, self._stable_lines
                # Transitioning from non-empty text to empty: signal cleared content
                self._stable_lines = []
                self._candidate_lines = []
                self._candidate_repeat_count = 0
                return True, []

            # If previous stable state was empty and current has content: substantive change
            if not self._stable_lines:
                self._stable_lines = list(lines)
                self._candidate_lines = []
                self._candidate_repeat_count = 0
                return True, self._stable_lines

            curr_text = self._extract_text(lines)
            prev_text = self._extract_text(self._stable_lines)

            # Immediate fast-path: identical text has zero jitter and requires no distance computation
            if curr_text == prev_text:
                self._candidate_repeat_count = 0
                return False, self._stable_lines

            # Early length difference exit: difference exceeding jitter threshold is substantive
            if abs(len(curr_text) - len(prev_text)) > self.max_jitter_dist:
                self._stable_lines = list(lines)
                self._candidate_lines = []
                self._candidate_repeat_count = 0
                return True, self._stable_lines

            dist = levenshtein_distance(curr_text, prev_text, max_dist=self.max_jitter_dist)

            # Minor character jitter / flip under threshold
            if dist <= self.max_jitter_dist:
                # Candidate jitter: check if candidate repeats stably across debounce_frames
                cand_text = self._extract_text(self._candidate_lines)
                if curr_text == cand_text:
                    self._candidate_repeat_count += 1
                    if self._candidate_repeat_count >= self.debounce_frames:
                        self._last_change_was_debounced = True
                        self._stable_lines = list(lines)
                        self._candidate_repeat_count = 0
                        return True, self._stable_lines
                    return False, self._stable_lines
                else:
                    self._candidate_lines = list(lines)
                    self._candidate_repeat_count = 1
                    return False, self._stable_lines

            # Substantive text change (distance exceeds jitter threshold)
            self._stable_lines = list(lines)
            self._candidate_lines = []
            self._candidate_repeat_count = 0
            return True, self._stable_lines

    def reset(self) -> None:
        """Reset stabilizer state."""
        with self._lock:
            self._stable_lines = None
            self._candidate_lines = []
            self._candidate_repeat_count = 0
            self._last_change_was_debounced = False
