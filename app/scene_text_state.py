# -*- coding: utf-8 -*-
"""场景全局文本状态融合器 (PR 13 / SceneTextState).

维护全局画面文本行拓扑，支持局部 ROI OCR 识别行与视野外历史行的无缝增量融合，
杜绝局部 ROI OCR 导致的视野外注释丢失与文字闪烁。
"""

from __future__ import annotations

import threading
from typing import Any, List, Optional, Tuple


def _boxes_intersect(
    box_a: list[list[int | float]] | tuple | Any,
    roi_box: tuple[int, int, int, int],
    margin: int = 4,
) -> bool:
    """检查多边形或矩形文本框与 ROI 矩形 (rx1, ry1, rx2, ry2) 是否相交。"""
    rx1, ry1, rx2, ry2 = roi_box
    # 扩展少量 margin 覆盖边界吸附误差
    rx1 -= margin
    ry1 -= margin
    rx2 += margin
    ry2 += margin

    if hasattr(box_a, "__len__") and len(box_a) == 4:
        # PP-OCR 4点多边形 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]] 或 [x, y, w, h]
        first = box_a[0]
        if isinstance(first, (list, tuple)) and len(first) >= 2:
            xs = [pt[0] for pt in box_a]
            ys = [pt[1] for pt in box_a]
            bx1, bx2 = min(xs), max(xs)
            by1, by2 = min(ys), max(ys)
        elif isinstance(first, (int, float)):
            bx1, by1, bw, bh = box_a[0], box_a[1], box_a[2], box_a[3]
            bx2, by2 = bx1 + bw, by1 + bh
        else:
            return False
    else:
        return False

    # 矩形无相交判定：若分离则必有一方在另一方外侧
    if bx2 < rx1 or bx1 > rx2 or by2 < ry1 or by1 > ry2:
        return False
    return True


class SceneTextState:
    """场景全局文字状态维护器。

    职责：
    1. 缓存当前画面全局识别行；
    2. 当传入局部 ROI 识别结果时，剔除与 ROI 区域重叠的旧行，合并新行；
    3. 支持按纵向坐标自动重排，保持视觉阅读顺序；
    4. 追踪连续 ROI 轮次，达到周期（如 30 次）时提示需要全量校准。
    """

    def __init__(self, full_refresh_interval: int = 30) -> None:
        self._lock = threading.RLock()
        self._lines: list[Any] = []
        self._roi_update_count: int = 0
        self.full_refresh_interval = max(5, int(full_refresh_interval))

    @property
    def lines(self) -> list[Any]:
        with self._lock:
            return list(self._lines)

    @property
    def roi_update_count(self) -> int:
        with self._lock:
            return self._roi_update_count

    def should_force_full_refresh(self) -> bool:
        """是否已达到全量重识别周期。"""
        with self._lock:
            return self._roi_update_count >= self.full_refresh_interval

    def update_full(self, lines: list[Any]) -> list[Any]:
        """全量刷新场景文本，重置周期计数器。"""
        with self._lock:
            self._lines = list(lines)
            self._roi_update_count = 0
            return list(self._lines)

    def update_roi(
        self,
        roi_lines: list[Any],
        roi_box: tuple[int, int, int, int],
    ) -> list[Any]:
        """增量融合局部 ROI 结果：剔除落在 ROI 范围内的旧行，合入新识别行。"""
        with self._lock:
            self._roi_update_count += 1
            if not self._lines:
                self._lines = list(roi_lines)
                return list(self._lines)

            # 保留未与本次变动 ROI 相交的旧行
            retained: list[Any] = []
            for item in self._lines:
                box = getattr(item, "box", None)
                if box is None and hasattr(item, "__getitem__"):
                    try:
                        box = item[1]  # 兼容 (text, box) 元组
                    except Exception:
                        pass
                if box is not None and _boxes_intersect(box, roi_box):
                    continue
                retained.append(item)

            # 注入新 ROI 识别行
            retained.extend(roi_lines)

            # 按 Y 坐标从上至下、X 坐标从左至右排序
            def _sort_key(line: Any) -> tuple[int, int]:
                box = getattr(line, "box", None)
                if box is not None and len(box) >= 1:
                    first = box[0]
                    if isinstance(first, (list, tuple)) and len(first) >= 2:
                        return (int(first[1]), int(first[0]))
                    elif isinstance(first, (int, float)) and len(box) >= 2:
                        return (int(box[1]), int(box[0]))
                return (0, 0)

            retained.sort(key=_sort_key)
            self._lines = retained
            return list(self._lines)

    def clear(self) -> None:
        """清空所有场景状态。"""
        with self._lock:
            self._lines.clear()
            self._roi_update_count = 0
