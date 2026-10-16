# -*- coding: utf-8 -*-
"""结果派发与世代防覆盖管理器 (PR 2 & PR 9).

管理 UI 信号通知，通过 generation_id 严格校验异步任务的时效性，
丢弃并记录任何晚到的旧世代译文，避免异步乱序覆盖新结果。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, Signal

from .pipelines import GenerationTracker

_log = logging.getLogger("st.result_mgr")


class ResultManager(QObject):
    """管线结果派发管理器。

    管理并派发以下信号：
    - subtitle_ready(translation[, gen_id])
    - replacement_ready(payload[, gen_id])
    - history_ready(source, translation, mode[, gen_id])
    - window_moved(x, y, w, h)
    - content_cleared()
    - stopped(reason)

    译文类派发只在 gen_id 仍是当前世代时才 emit，过期结果计数后丢弃。
    """

    subtitle_ready = Signal((str,), (str, int))
    replacement_ready = Signal((object,), (object, int))
    history_ready = Signal((str, str, str), (str, str, str, int))
    window_moved = Signal(int, int, int, int)
    content_cleared = Signal()
    stopped = Signal(str)

    def __init__(self, generation_tracker: GenerationTracker | None = None, parent: Any = None) -> None:
        super().__init__(parent)
        self.generation_tracker = generation_tracker or GenerationTracker()
        self.dropped_stale_count: int = 0
        self._lock = threading.Lock()

    def _accept(self, gen_id: int, kind: str) -> bool:
        """gen_id 仍为当前世代时返回 True，否则计数并丢弃。"""
        with self._lock:
            if self.generation_tracker.is_active(gen_id):
                return True
            self.dropped_stale_count += 1
        _log.debug("Dropped stale %s: gen=%d active=%d", kind, gen_id,
                   self.generation_tracker.current_generation)
        return False

    def dispatch_subtitle(self, text: str, gen_id: int) -> bool:
        """派发字幕译文。若 gen_id 非当前活跃世代则丢弃。"""
        if not self._accept(gen_id, "subtitle"):
            return False
        self.subtitle_ready[str, int].emit(text, gen_id)
        self.subtitle_ready.emit(text)
        return True


    def dispatch_replacement(self, payload: Any, gen_id: int) -> bool:
        """派发原位替换 payload。若 gen_id 非当前活跃世代则丢弃。"""
        if not self._accept(gen_id, "replacement"):
            return False
        self.replacement_ready[object, int].emit(payload, gen_id)
        self.replacement_ready.emit(payload)
        return True

    def dispatch_history(
        self,
        source: str,
        translation: str,
        mode: str,
        gen_id: int | None = None,
    ) -> bool:
        """派发历史记录。若指定了 gen_id 且已过期则丢弃（不计入过期译文数）。"""
        if gen_id is not None and not self.generation_tracker.is_active(gen_id):
            _log.debug("Dropped stale history: gen=%d", gen_id)
            return False
        self.history_ready[str, str, str, int].emit(source, translation, mode, gen_id or 0)
        self.history_ready.emit(source, translation, mode)
        return True

    def dispatch_cleared(self, gen_id: int | None = None) -> bool:
        """派发画面内容清空通知。"""
        if gen_id is not None and not self.generation_tracker.is_active(gen_id):
            return False
        self.content_cleared.emit()
        return True

    def dispatch_moved(self, x: int, y: int, w: int, h: int) -> None:
        """派发目标窗口或区域位移通知。"""
        self.window_moved.emit(x, y, w, h)

    def dispatch_stopped(self, reason: str) -> None:
        """派发监视停止通知。"""
        self.stopped.emit(reason)
