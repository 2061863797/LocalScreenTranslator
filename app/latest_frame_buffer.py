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
    1. Single-slot capacity.
    2. Overwrites unconsumed frame with newer frame.
    3. Increments dropped_count when an unconsumed frame is overwritten.
    4. Thread-safe condition synchronization for get(timeout).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frame: np.ndarray | None = None
        self._packet: FramePacket | None = None
        self._gen_id: int | None = None
        self._dropped_count: int = 0

    def put(
        self,
        frame: np.ndarray | FramePacket,
        generation_id: int | None = None,
        *,
        gen_id: int | None = None,
    ) -> None:
        """Store the newest frame or FramePacket into the buffer, overwriting any unconsumed frame.

        Args:
            frame: Captured image frame or FramePacket.
            generation_id: Generation identifier (positional or keyword).
            gen_id: Generation identifier (alias keyword).
        """
        if isinstance(frame, FramePacket):
            with self._lock:
                if self._frame is not None or self._packet is not None:
                    self._dropped_count += 1
                self._packet = frame
                self._frame = frame.frame
                self._gen_id = frame.epoch
                self._cond.notify_all()
            return

        gid = generation_id if generation_id is not None else (gen_id if gen_id is not None else 0)
        with self._lock:
            if self._frame is not None or self._packet is not None:
                self._dropped_count += 1
            self._packet = None
            self._frame = frame
            self._gen_id = gid
            self._cond.notify_all()

    def get(self, timeout: float | None = None) -> tuple[np.ndarray, int] | FramePacket | None:
        """Consume and return the latest frame or FramePacket from the buffer.

        Blocks until a frame is available or until timeout expires.

        Args:
            timeout: Optional wait timeout in seconds. None blocks indefinitely.

        Returns:
            FramePacket or (frame, generation_id) tuple, or None if timed out without a frame.
        """
        with self._lock:
            if self._frame is None and self._packet is None:
                if not self._cond.wait(timeout):
                    return None
            if self._frame is None and self._packet is None:
                return None

            if self._packet is not None:
                result = self._packet
                self._packet = None
                self._frame = None
                self._gen_id = None
                return result

            result = (self._frame, self._gen_id if self._gen_id is not None else 0)
            self._frame = None
            self._gen_id = None
            return result

    def clear(self) -> None:
        """Clear any pending frame from the buffer without incrementing dropped_count."""
        with self._lock:
            self._frame = None
            self._gen_id = None
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
            return self._frame is None

    def peek(self) -> tuple[np.ndarray, int] | None:
        """Return the current frame and generation ID without removing it from the buffer."""
        with self._lock:
            if self._frame is None:
                return None
            return (self._frame, self._gen_id if self._gen_id is not None else 0)
