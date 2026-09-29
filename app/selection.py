# -*- coding: utf-8 -*-
"""获取前台选中文本：WM_COPY / 多种复制快捷键。

终端（Windows Terminal、conhost 等）往往不用 Ctrl+C 复制选区，
而用 Ctrl+Shift+C 或「选中即复制」；标准编辑框则 WM_COPY / Ctrl+C 更稳。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from PySide6.QtCore import QMimeData, QObject, QTimer, Signal

from pynput.keyboard import Controller, Key

user32 = ctypes.windll.user32

WM_COPY = 0x0301


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def get_focus_hwnd() -> int:
    """前台焦点控件句柄（失败则返回前台顶层窗）。"""
    fg = user32.GetForegroundWindow()
    if not fg:
        return 0
    tid = user32.GetWindowThreadProcessId(fg, None)
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return int(info.hwndFocus or info.hwndActive or fg)
    return int(fg)


def try_wm_copy() -> bool:
    """向焦点控件发 WM_COPY（多数编辑框有效，部分终端无效）。"""
    hwnd = get_focus_hwnd()
    if not hwnd:
        return False
    try:
        user32.SendMessageW(hwnd, WM_COPY, 0, 0)
        return True
    except Exception:
        return False


def _release_modifiers(kb: Controller) -> None:
    for mod in (
        Key.alt, Key.alt_l, Key.alt_r,
        Key.shift, Key.shift_l, Key.shift_r,
        Key.ctrl, Key.ctrl_l, Key.ctrl_r,
        Key.cmd,
    ):
        try:
            kb.release(mod)
        except Exception:
            pass


def send_copy_shortcut(kind: str = "ctrl_c") -> None:
    """模拟复制快捷键。kind: ctrl_c | ctrl_shift_c | ctrl_insert"""
    kb = Controller()
    _release_modifiers(kb)
    time.sleep(0.02)
    if kind == "ctrl_shift_c":
        # Windows Terminal / 许多终端默认
        with kb.pressed(Key.ctrl):
            with kb.pressed(Key.shift):
                kb.press("c")
                kb.release("c")
    elif kind == "ctrl_insert":
        with kb.pressed(Key.ctrl):
            kb.press(Key.insert)
            kb.release(Key.insert)
    else:
        with kb.pressed(Key.ctrl):
            kb.press("c")
            kb.release("c")


def clone_mime_data(source: QMimeData | None) -> QMimeData:
    """完整克隆剪贴板数据，保留多种数据格式以供还原。"""
    from PySide6.QtCore import QMimeData

    new_data = QMimeData()
    if source is None:
        return new_data
    for fmt in source.formats():
        try:
            new_data.setData(fmt, bytes(source.data(fmt)))
        except Exception:
            pass
    return new_data


class WordSelectionService(QObject):
    """划词剪贴板安全提取服务与多阶段降级状态机。

    执行流程：
    1. 备份原剪贴板；
    2. 写入时间戳防碰撞标记字符串；
    3. 依次尝试 wm_copy -> ctrl_c -> ctrl_shift_c -> ctrl_insert；
    4. 高频定时器轮询剪贴板变化，识别有效文本；
    5. 还原原剪贴板内容并向外发射信号。
    """

    text_ready = Signal(str, int, int)  # text, mouse_x, mouse_y
    failed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.is_busy: bool = False
        self._old_mime: QMimeData | None = None
        self._marker: str = ""
        self._phase: int = 0
        self._kinds: tuple[str, ...] = ("wm_copy", "ctrl_c", "ctrl_shift_c", "ctrl_insert")
        self._target_pos: tuple[int, int] = (0, 0)
        self._quitting: bool = False

    def set_quitting(self, quitting: bool = True) -> None:
        self._quitting = quitting
        if quitting:
            self.restore_clipboard()

    def fetch_selection(self, pos: tuple[int, int] | None = None) -> bool:
        """启动划词多阶段捕获。若已有进行中的捕获则返回 False。"""
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QApplication

        if self._quitting or self.is_busy:
            return False

        app = QApplication.instance()
        if app is None:
            return False

        if pos is None:
            cpos = QCursor.pos()
            self._target_pos = (cpos.x(), cpos.y())
        else:
            self._target_pos = pos

        self.is_busy = True
        clipboard = QApplication.clipboard()
        self._old_mime = clone_mime_data(clipboard.mimeData())
        self._marker = f"\u200bST{time.time_ns()}\u200b"
        clipboard.setText(self._marker)
        self._phase = 0
        self._fire_phase()
        self._poll_clipboard(attempts=12)
        return True

    def _fire_phase(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._phase < 0 or self._phase >= len(self._kinds):
            return
        kind = self._kinds[self._phase]
        marker = self._marker
        clipboard = QApplication.clipboard()
        try:
            if marker and clipboard:
                clipboard.setText(marker)
            if kind == "wm_copy":
                try_wm_copy()
            else:
                send_copy_shortcut(kind)
        except Exception:
            pass

    def _clipboard_text(self) -> str:
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is None:
            return ""
        text = clipboard.text()
        if not text or (self._marker and text == self._marker):
            return ""
        return text.strip()

    def _poll_clipboard(self, attempts: int) -> None:
        from PySide6.QtCore import QTimer

        if not self.is_busy or self._quitting:
            return
        text = self._clipboard_text()
        if text:
            self._finish_with_text(text)
            return
        if attempts > 0:
            QTimer.singleShot(30, lambda: self._poll_clipboard(attempts - 1))
            return
        phase = self._phase + 1
        if phase < len(self._kinds):
            self._phase = phase
            self._fire_phase()
            self._poll_clipboard(attempts=12)
            return
        self._finish_with_text("")

    def _finish_with_text(self, text: str) -> None:
        x, y = self._target_pos
        self.restore_clipboard()
        if self._quitting:
            return
        if text:
            self.text_ready.emit(text, x, y)
        else:
            self.failed.emit()

    def restore_clipboard(self) -> None:
        from PySide6.QtWidgets import QApplication

        old = self._old_mime
        self._old_mime = None
        self.is_busy = False
        try:
            clipboard = QApplication.clipboard()
            if old is not None and clipboard is not None:
                clipboard.setMimeData(old)
        except Exception:
            pass

