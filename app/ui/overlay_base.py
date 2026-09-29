# -*- coding: utf-8 -*-
"""浮层与窗口基础混入类、Win32 辅助函数及通用几何更新工具。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QWidget

from ..capture import set_window_capture_excluded

_FLAGS_TOP = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
)
_STATUS_BAR_HEIGHT = 30


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


def _exclude_from_capture(widget: QWidget) -> None:
    """让仅用于框选的窗口不参与屏幕捕获。"""
    import sys
    mod = sys.modules.get("app.ui.overlays")
    if mod is not None:
        target = getattr(mod, "_exclude_from_capture", None)
        if target is not None and target is not _exclude_from_capture:
            return target(widget)
    try:
        hwnd = int(widget.winId())
        if hwnd:
            set_window_capture_excluded(hwnd, True)
    except Exception:
        pass


def _allow_capture(widget: QWidget) -> None:
    """恢复普通显示亲和性，让系统截图和录屏能捕获翻译浮层。"""
    import sys
    mod = sys.modules.get("app.ui.overlays")
    if mod is not None:
        target = getattr(mod, "_allow_capture", None)
        if target is not None and target is not _allow_capture:
            return target(widget)
    try:
        hwnd = int(widget.winId())
        if hwnd:
            set_window_capture_excluded(hwnd, False)
    except Exception:
        pass


def _show_once(widget: QWidget) -> None:
    """仅在不可见时 show，避免反复 show/raise 导致闪烁。"""
    if widget is not None and not widget.isVisible():
        widget.show()


def _set_geo_if_changed(widget: QWidget, x: int, y: int, w: int, h: int) -> bool:
    """几何未变则跳过 setGeometry（减少 DWM 重布局闪）。返回是否改过。"""
    g = widget.geometry()
    if g.x() == x and g.y() == y and g.width() == w and g.height() == h:
        return False
    parent = widget.parentWidget()
    old_rect = QRect(g)
    widget.setGeometry(x, y, w, h)
    if parent is not None and parent.isVisible():
        parent.update(old_rect)
        parent.update(widget.geometry())
    return True


def _move_if_changed(widget: QWidget, x: int, y: int) -> bool:
    if widget.x() == x and widget.y() == y:
        return False
    parent = widget.parentWidget()
    old_rect = QRect(widget.geometry())
    widget.move(x, y)
    if parent is not None and parent.isVisible():
        parent.update(old_rect)
        parent.update(widget.geometry())
    return True


class _CaptureAllowedMixin:
    """翻译浮层显示时保持可被系统截图和录屏捕获。"""

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        _allow_capture(self)


class _DraggableMixin:
    """按住窗口空白处拖动。"""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and getattr(self, "_drag_offset", None) is not None
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


def dispatch_set_overlay_layer(widget: QWidget, owner_hwnd: int | None = None) -> None:
    """分发置顶层级设置，优先遵循 app.ui.overlays 上可能存在的 mock 补丁。"""
    import sys
    mod = sys.modules.get("app.ui.overlays")
    if mod is not None:
        target = getattr(mod, "set_overlay_layer", None)
        if target is not None and target is not dispatch_set_overlay_layer:
            return target(widget, owner_hwnd)
    from .topmost import set_overlay_layer
    return set_overlay_layer(widget, owner_hwnd)
