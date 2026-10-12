# -*- coding: utf-8 -*-
"""浮层与窗口基础混入类、Win32 辅助函数及通用几何更新工具。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QApplication, QLabel, QWidget

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
    try:
        hwnd = int(widget.winId())
        if hwnd:
            set_window_capture_excluded(hwnd, True)
    except Exception:
        pass


def _allow_capture(widget: QWidget) -> None:
    """恢复普通显示亲和性，让系统截图和录屏能捕获翻译浮层。"""
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


class _FloatingCtrlMixin:
    """独立浮动控制条：层级跟随目标窗口，并贴在目标区域左上方（不遮挡内容）。"""

    _layer_owner: int | None = None

    def set_layer_owner(self, owner_hwnd: int | None) -> None:
        self._layer_owner = int(owner_hwnd) if owner_hwnd else None
        dispatch_set_overlay_layer(self, self._layer_owner)
        if self.isVisible():
            _allow_capture(self)

    def restack_layer(self) -> None:
        if not self.isVisible():
            return
        if QApplication.activePopupWidget() is not None:
            return
        if self._layer_owner:
            from .topmost import restack_above_owner

            restack_above_owner(self, self._layer_owner)
        else:
            dispatch_set_overlay_layer(self, None)

    def place_above(self, rect: tuple[int, int, int, int]) -> None:
        x, y, _, _ = rect
        self.adjustSize()
        first = not self.isVisible()
        _move_if_changed(self, x, y - self.height() - 4)
        _show_once(self)
        if first:
            _allow_capture(self)
        self.restack_layer()


class _DragHandle(QLabel):
    """“⠿”拖动手柄：按住左键拖动目标，子类决定拖谁、何时可拖、松开后做什么。"""

    def __init__(self, parent=None, height: int = 26):
        super().__init__("⠿", parent)
        self._drag_offset: QPoint | None = None
        self.setObjectName("dragHandle")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(20, height)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setStyleSheet(
            "QLabel#dragHandle{background:transparent;border:none;color:rgba(255,255,255,180);font-size:15px;padding:0;}"
            "QLabel#dragHandle:hover{color:#ffffff;}"
        )

    # 子类覆盖的钩子
    def _can_drag(self) -> bool:
        """拖动过程中是否跟随移动。"""
        return True

    def _can_start_drag(self) -> bool:
        """按下时能否开始拖动；默认与 _can_drag 相同。"""
        return self._can_drag()

    def _drag_origin(self) -> QPoint:
        """被拖目标当前左上角（全局坐标）。"""
        raise NotImplementedError

    def _drag_to(self, point: QPoint) -> None:
        raise NotImplementedError

    def _drag_started(self) -> None:
        pass

    def _drag_finished(self) -> None:
        pass

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._can_start_drag():
            self._drag_started()
            self._drag_offset = event.globalPosition().toPoint() - self._drag_origin()
            self.grabMouse()
            event.accept()

    def mouseMoveEvent(self, event):
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            if self._can_drag():
                self._drag_to(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            try:
                self.releaseMouse()
            except Exception:
                pass
            self._drag_finished()
            event.accept()


def dispatch_set_overlay_layer(widget: QWidget, owner_hwnd: int | None = None) -> None:
    """设置浮层置顶层级（延迟导入 topmost，避免循环导入）。"""
    from . import topmost
    return topmost.set_overlay_layer(widget, owner_hwnd)
