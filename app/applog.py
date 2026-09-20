# -*- coding: utf-8 -*-
"""应用日志：写入 app.log + 可选推送到设置页实时面板。"""

from __future__ import annotations

from collections import deque
import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, Signal

from .paths import LOG_PATH

_configured = False
_emitter: "LogEmitter | None" = None
_qt_handler: "QtLogHandler | None" = None

# 内存环形缓冲，设置页打开时能看到最近日志
_RING_MAX = 800
_ring: deque = deque(maxlen=_RING_MAX)


class LogEmitter(QObject):
    """线程安全：在任意线程 emit，UI 用 QueuedConnection 接收。"""

    line = Signal(str)


class QtLogHandler(logging.Handler):
    """把 logging 记录推给 Qt 信号 + 内存环。"""

    def __init__(self, emitter: LogEmitter):
        super().__init__()
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # 单行过长（异常栈/超长 OCR）截断，避免设置页/环缓冲爆炸
            if len(msg) > 2000:
                msg = msg[:2000] + "…"
            _ring.append(msg)
            self._emitter.line.emit(msg)
        except Exception:
            self.handleError(record)


def get_log_emitter() -> LogEmitter:
    """供设置页连接实时日志。须在 QApplication 创建后调用 setup。"""
    global _emitter
    if _emitter is None:
        _emitter = LogEmitter()
    return _emitter


def recent_lines(max_lines: int = 400) -> list[str]:
    """内存中最近日志；不足时再从文件尾部补。"""
    if _ring:
        return list(_ring)[-max_lines:]
    return tail_file(max_lines)


def tail_file(max_lines: int = 400) -> list[str]:
    """读 app.log 末尾若干行。"""
    if not LOG_PATH.exists():
        return []
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """初始化根日志（进程内只配一次）。应在 QApplication 之后调用。"""
    global _configured, _qt_handler
    root = logging.getLogger("st")
    if _configured:
        return root

    root.setLevel(level)
    root.handlers.clear()
    root.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        LOG_PATH,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if sys.stderr is not None:
        try:
            sh = logging.StreamHandler(sys.stderr)
            sh.setLevel(logging.WARNING)
            sh.setFormatter(fmt)
            root.addHandler(sh)
        except Exception:
            pass

    # 实时面板
    emitter = get_log_emitter()
    _qt_handler = QtLogHandler(emitter)
    _qt_handler.setLevel(level)
    _qt_handler.setFormatter(fmt)
    root.addHandler(_qt_handler)

    _configured = True
    root.info("======== 日志启动 path=%s ========", LOG_PATH)
    return root


def get_logger(name: str = "app") -> logging.Logger:
    """取子 logger，如 st.app / st.llama / st.ocr。"""
    if not _configured:
        setup_logging()
    return logging.getLogger(f"st.{name}")
