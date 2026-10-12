# -*- coding: utf-8 -*-
"""Latest-Frame-Wins buffer for real-time screen translation pipeline (PR 11).

Single-capacity thread-safe producer-consumer buffer guarded by threading.Condition.
When screen capture rate exceeds inference rate, stale intermediate frames
are automatically dropped in favour of the most recent frame.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import numpy as np


@dataclass
class FramePacket:
    """封装抓屏数据、工作版本、内容版本与变动区域的帧数据包。

    双版本号设计 (PR 1)：
    - work_revision: 画面只要发生视觉变动即自增，保证 LatestFrameBuffer 覆盖后消费端知晓有新工作需处理；
    - content_revision: 仅在场景突变或变动 ROI 命中已有字幕行时自增，用于淘汰旧在途翻译结果；
    - epoch: 与 content_revision 同义，只给一个时另一个自动补齐。
    """

    frame: np.ndarray
    epoch: int = 0
    roi_box: tuple[int, int, int, int] | None = None
    timestamp: float = 0.0
    has_changed: bool = True
    changed_ratio: float = 0.0
    work_revision: int = 0
    content_revision: int = 0

    def __post_init__(self):
        if self.content_revision == 0 and self.epoch != 0:
            object.__setattr__(self, "content_revision", self.epoch)
        elif self.epoch == 0 and self.content_revision != 0:
            object.__setattr__(self, "epoch", self.content_revision)


class LatestFrameBuffer:
    """单槽缓冲：新帧覆盖未消费的旧帧，并统计被覆盖的帧数。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._item: FramePacket | None = None
        self._dropped_count: int = 0

    def put(self, packet: FramePacket) -> None:
        """放入最新帧，覆盖尚未消费的旧帧。"""
        with self._lock:
            if self._item is not None:
                self._dropped_count += 1
            self._item = packet
            self._cond.notify_all()

    def get(self, timeout: float | None = None) -> FramePacket | None:
        """取出最新帧；超时仍无帧时返回 None。"""
        with self._lock:
            if self._item is None:
                self._cond.wait(timeout)
            result, self._item = self._item, None
            return result

    def clear(self) -> None:
        """丢弃待消费帧（不计入覆盖数），并唤醒等待中的消费者。"""
        with self._lock:
            self._item = None
            self._cond.notify_all()

    @property
    def dropped_count(self) -> int:
        """被新帧覆盖而未消费的帧数。"""
        with self._lock:
            return self._dropped_count

    @property
    def is_empty(self) -> bool:
        """当前没有待消费帧时为 True。"""
        with self._lock:
            return self._item is None
