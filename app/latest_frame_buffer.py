# -*- coding: utf-8 -*-
"""Latest-Frame-Wins buffer for real-time screen translation pipeline (PR 11).

Single-capacity thread-safe producer-consumer buffer guarded by threading.Condition.
When screen capture rate exceeds inference rate, stale intermediate frames
are automatically dropped in favour of the most recent frame.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Optional, Tuple
import numpy as np


@dataclass
class FramePacket:
    """封装抓屏数据、内容版本与变动区域的帧数据包。
    
    兼容性保证：支持迭代与索引解包 (img, epoch = packet)，保证与既有代码和测试 100% 兼容。
    """

    frame: np.ndarray
    epoch: int
    roi_box: tuple[int, int, int, int] | None = None
    timestamp: float = 0.0
    has_changed: bool = True
    changed_ratio: float = 0.0

    def __iter__(self):
        yield self.frame
        yield self.epoch

    def __getitem__(self, index: int):
        if index == 0:
            return self.frame
        elif index == 1:
            return self.epoch
        raise IndexError(f"FramePacket index {index} out of range (0..1)")


class LatestFrameBuffer:
    """Thread-safe single-slot producer-consumer buffer guarded by threading.Condition.

    Guarantees:
    1. Single-slot capacity via unified single source of truth (_item).
    2. Overwrites unconsumed frame with newer frame.
    3. Increments dropped_count when an unconsumed frame is overwritten.
    4. Thread-safe condition synchronization for get(timeout).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._item: FramePacket | tuple[np.ndarray, int] | None = None
        self._dropped_count: int = 0

    @property
    def _frame(self) -> np.ndarray | None:
        """兼容性属性：返回内部待消费帧图像，无帧时为 None。"""
        if self._item is None:
            return None
        if isinstance(self._item, FramePacket):
            return self._item.frame
        return self._item[0]

    @_frame.setter
    def _frame(self, val: Any) -> None:
        if val is None:
            self._item = None

    @property
    def _packet(self) -> FramePacket | None:
        """兼容性属性：返回内部待消费 FramePacket。"""
        if isinstance(self._item, FramePacket):
            return self._item
        return None

    @_packet.setter
    def _packet(self, val: Any) -> None:
        if val is None:
            self._item = None
        elif isinstance(val, FramePacket):
            self._item = val

    @property
    def _gen_id(self) -> int | None:
        """兼容性属性：返回内部待消费 generation ID。"""
        if self._item is None:
            return None
        if isinstance(self._item, FramePacket):
            return self._item.epoch
        return self._item[1]

    def put(
        self,
        frame: np.ndarray | FramePacket,
        generation_id: int | None = None,
        *,
        gen_id: int | None = None,
    ) -> None:
        """Store the newest frame or FramePacket into the buffer, overwriting any unconsumed frame."""
        if isinstance(frame, FramePacket):
            new_item: FramePacket | tuple[np.ndarray, int] = frame
        else:
            gid = generation_id if generation_id is not None else (gen_id if gen_id is not None else 0)
            new_item = (frame, gid)

        with self._lock:
            if self._item is not None:
                self._dropped_count += 1
            self._item = new_item
            self._cond.notify_all()

    def get(self, timeout: float | None = None) -> tuple[np.ndarray, int] | FramePacket | None:
        """Consume and return the latest frame or FramePacket from the buffer."""
        with self._lock:
            if self._item is None:
                if not self._cond.wait(timeout):
                    return None
            if self._item is None:
                return None

            result = self._item
            self._item = None
            return result

    def clear(self) -> None:
        """Clear any pending frame from the buffer without incrementing dropped_count."""
        with self._lock:
            self._item = None
            self._cond.notify_all()

    @property
    def dropped_count(self) -> int:
        """Number of unconsumed frames dropped due to newer frames arriving."""
        with self._lock:
            return self._dropped_count

    def reset_dropped_count(self) -> None:
        """Reset the dropped frame counter to zero."""
        with self._lock:
            self._dropped_count = 0

    @property
    def is_empty(self) -> bool:
        """True if no unconsumed frame is waiting in the buffer."""
        with self._lock:
            return self._item is None

    def peek(self) -> tuple[np.ndarray, int] | None:
        """Return the current frame and generation ID without removing it from the buffer."""
        with self._lock:
            if self._item is None:
                return None
            if isinstance(self._item, FramePacket):
                return (self._item.frame, self._item.epoch)
            return self._item
