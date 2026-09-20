# -*- coding: utf-8 -*-
"""结果派发与世代防覆盖管理器 (PR 2 & PR 9).

管理 UI 信号通知，通过 generation_id 严格校验异步任务的时效性，
丢弃并记录任何晚到的旧世代译文，避免异步乱序覆盖新结果。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, List, Optional, Tuple

try:
    from PySide6.QtCore import QObject, Signal
    _HAS_QT = True
except ImportError:
    _HAS_QT = False

    class QObject:  # type: ignore
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class Signal:  # type: ignore
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._slots: list[Callable[..., Any]] = []

        def connect(self, slot: Callable[..., Any]) -> None:
            self._slots.append(slot)

        def emit(self, *args: Any, **kwargs: Any) -> None:
            for s in list(self._slots):
                try:
                    s(*args, **kwargs)
                except Exception:
                    pass

from .pipelines import GenerationTracker, WatchCycleContext

_log = logging.getLogger("st.result_mgr")


class ResultManager(QObject):
    """管线结果派发管理器。

    管理并派发以下信号：
    - subtitle_ready(translation)
    - annotations_ready(items)
    - history_ready(source, translation, mode)
    - window_moved(x, y, w, h)
    - content_cleared()
    - stopped(reason)

    所有派发方法在 gen_id 与 generation_tracker 的 active_generation 匹配时才真正 emit，
    对于过期结果自动记录 debug 日志并安全丢弃。
    """

    subtitle_ready = Signal(str)
    annotations_ready = Signal(list)
    history_ready = Signal(str, str, str)
    window_moved = Signal(int, int, int, int)
    content_cleared = Signal()
    stopped = Signal(str)

    def __init__(
        self,
        generation_tracker: GenerationTracker | None = None,
        parent: Any = None,
        *,
        on_subtitle: Callable[[str], None] | None = None,
        on_annotations: Callable[[list], None] | None = None,
        on_history: Callable[[str, str, str], None] | None = None,
        on_cleared: Callable[[], None] | None = None,
        on_moved: Callable[[int, int, int, int], None] | None = None,
        on_stopped: Callable[[str], None] | None = None,
    ) -> None:
        if _HAS_QT and parent is not None:
            super().__init__(parent)
        else:
            super().__init__()

        self.generation_tracker = generation_tracker or GenerationTracker()
        self.latest_dispatched: str | None = None
        self.dropped_stale_count: int = 0
        self.dispatched_history: list[tuple[int, str]] = []

        self._on_subtitle = on_subtitle
        self._on_annotations = on_annotations
        self._on_history = on_history
        self._on_cleared = on_cleared
        self._on_moved = on_moved
        self._on_stopped = on_stopped
        self._lock = threading.Lock()

    @property
    def tracker(self) -> GenerationTracker:
        """Alias for generation_tracker."""
        return self.generation_tracker

    @tracker.setter
    def tracker(self, value: GenerationTracker) -> None:
        self.generation_tracker = value

    def dispatch_subtitle(self, text: str, gen_id: int) -> bool:
        """派发字幕译文。若 gen_id 非当前活跃世代则丢弃。"""
        with self._lock:
            if not self.generation_tracker.is_active(gen_id):
                self.dropped_stale_count += 1
                _log.debug(
                    "Dropped stale subtitle translation: gen=%d active=%d",
                    gen_id,
                    self.generation_tracker.current_generation,
                )
                return False

            self.latest_dispatched = text
            self.dispatched_history.append((gen_id, text))

        self.subtitle_ready.emit(text)
        if self._on_subtitle is not None:
            try:
                self._on_subtitle(text)
            except Exception:
                pass
        return True

    def dispatch_annotations(self, items: list[Any], gen_id: int) -> bool:
        """派发逐行标注译文。若 gen_id 非当前活跃世代则丢弃。"""
        with self._lock:
            if not self.generation_tracker.is_active(gen_id):
                self.dropped_stale_count += 1
                _log.debug(
                    "Dropped stale annotation translation: gen=%d active=%d",
                    gen_id,
                    self.generation_tracker.current_generation,
                )
                return False

        self.annotations_ready.emit(items)
        if self._on_annotations is not None:
            try:
                self._on_annotations(items)
            except Exception:
                pass
        return True

    def dispatch_history(
        self, source: str, translation: str, mode: str, gen_id: int | None = None
    ) -> bool:
        """派发历史记录。若指定了 gen_id 且已过期则丢弃。"""
        if gen_id is not None:
            with self._lock:
                if not self.generation_tracker.is_active(gen_id):
                    _log.debug(
                        "Dropped stale history: gen=%d active=%d",
                        gen_id,
                        self.generation_tracker.current_generation,
                    )
                    return False

        self.history_ready.emit(source, translation, mode)
        if self._on_history is not None:
            try:
                self._on_history(source, translation, mode)
            except Exception:
                pass
        return True

    def dispatch_cleared(self, gen_id: int | None = None) -> bool:
        """派发画面内容清空通知。"""
        if gen_id is not None:
            with self._lock:
                if not self.generation_tracker.is_active(gen_id):
                    return False

        self.content_cleared.emit()
        if self._on_cleared is not None:
            try:
                self._on_cleared()
            except Exception:
                pass
        return True

    def dispatch_moved(self, x: int, y: int, w: int, h: int) -> None:
        """派发目标窗口或区域位移通知。"""
        self.window_moved.emit(x, y, w, h)
        if self._on_moved is not None:
            try:
                self._on_moved(x, y, w, h)
            except Exception:
                pass

    def dispatch_stopped(self, reason: str) -> None:
        """派发监视停止通知。"""
        self.stopped.emit(reason)
        if self._on_stopped is not None:
            try:
                self._on_stopped(reason)
            except Exception:
                pass
