# -*- coding: utf-8 -*-
"""Adaptive polling controller for screen capture & translation pipeline.

PR 4: Adaptive Polling.
Replaces static sleep intervals with an adaptive state machine transitioning
through ACTIVE (120ms), WARM (250ms), IDLE (500ms), and DEEP_IDLE (800ms).
Immediately wakes to ACTIVE upon frame change.
"""

from __future__ import annotations

from enum import Enum
import threading


class PollingState(str, Enum):
    """Adaptive polling states."""

    ACTIVE = "ACTIVE"
    WARM = "WARM"
    IDLE = "IDLE"
    DEEP_IDLE = "DEEP_IDLE"


class AdaptivePollingController:
    """Dynamic polling interval controller responding to visual activity."""

    def __init__(
        self,
        active_interval: float = 0.12,     # 120ms
        warm_interval: float = 0.25,       # 250ms
        idle_interval: float = 0.50,       # 500ms
        deep_idle_interval: float = 0.80,  # 800ms
        warm_threshold: int = 2,
        idle_threshold: int = 5,
        deep_idle_threshold: int = 20,
    ) -> None:
        self.active_interval = float(active_interval)
        self.warm_interval = float(warm_interval)
        self.idle_interval = float(idle_interval)
        self.deep_idle_interval = float(deep_idle_interval)

        self.warm_threshold = int(warm_threshold)
        self.idle_threshold = int(idle_threshold)
        self.deep_idle_threshold = int(deep_idle_threshold)

        self._lock = threading.Lock()
        self._state = PollingState.ACTIVE
        self._static_count = 0

    def on_frame(self, has_changed: bool) -> float:
        """Update controller state based on whether visual frame changed.

        Args:
            has_changed: True if current frame differs substantively from previous.

        Returns:
            Sleep interval in seconds for the next cycle.
        """
        with self._lock:
            if has_changed:
                self._static_count = 0
                self._state = PollingState.ACTIVE
                return self.active_interval

            self._static_count += 1
            if self._static_count >= self.deep_idle_threshold:
                self._state = PollingState.DEEP_IDLE
                return self.deep_idle_interval
            elif self._static_count >= self.idle_threshold:
                self._state = PollingState.IDLE
                return self.idle_interval
            elif self._static_count >= self.warm_threshold:
                self._state = PollingState.WARM
                return self.warm_interval
            else:
                self._state = PollingState.ACTIVE
                return self.active_interval

    @property
    def current_interval(self) -> float:
        """Current sleep interval according to active polling state."""
        with self._lock:
            if self._state == PollingState.DEEP_IDLE:
                return self.deep_idle_interval
            elif self._state == PollingState.IDLE:
                return self.idle_interval
            elif self._state == PollingState.WARM:
                return self.warm_interval
            return self.active_interval

    @property
    def state(self) -> PollingState:
        """Current polling state."""
        with self._lock:
            return self._state

    @property
    def static_count(self) -> int:
        """Number of consecutive static frames observed."""
        with self._lock:
            return self._static_count

    def reset(self) -> None:
        """Reset polling controller back to initial active state."""
        with self._lock:
            self._state = PollingState.ACTIVE
            self._static_count = 0
