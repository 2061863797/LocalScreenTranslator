# -*- coding: utf-8 -*-
"""区域翻译识别框（带外侧拖动与固定控制条）。"""

from __future__ import annotations

import ctypes

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QPainter, QPen, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import t as _t
from .overlay_base import (
    _FLAGS_TOP,
    _STATUS_BAR_HEIGHT,
    _allow_capture,
    _CaptureAllowedMixin,
    _move_if_changed,
    _set_geo_if_changed,
    _show_once,
    dispatch_set_overlay_layer as set_overlay_layer,
)
from .theme import BORDER_QCOLOR, CTRL_STYLE, paint_size_grip
from .topmost import restack_above_owner


class _RegionFrameDragHandle(QLabel):
    """识别框最左侧的六点拖动手柄。"""

    def __init__(self, frame: RegionWatchFrame, parent=None):
        super().__init__("⠿", parent)
        self._frame = frame
        self._drag_offset = None
        self.setObjectName("dragHandle")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(20, 26)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setStyleSheet(
            "QLabel#dragHandle{background:transparent;border:none;color:rgba(255,255,255,180);font-size:15px;padding:0;}"
            "QLabel#dragHandle:hover{color:#ffffff;}"
        )
        self.setToolTip(_t("frame_drag"))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._frame.pinned:
            cr = self._frame.content_rect()
            self._drag_offset = event.globalPosition().toPoint() - QPoint(cr[0], cr[1])
            self.grabMouse()
            event.accept()

    def mouseMoveEvent(self, event):
        if (
            self._drag_offset is not None
            and not self._frame.pinned
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            p = event.globalPosition().toPoint() - self._drag_offset
            self._frame.move_content_to(p.x(), p.y())
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            try:
                self.releaseMouse()
            except Exception:
                pass
            self._frame._emit_moved()
            event.accept()


class _RegionCtrl(_CaptureAllowedMixin, QWidget):
    """区域识别框控制条：拖动手柄 + 固定/取消固定。"""

    def __init__(self, frame: RegionWatchFrame):
        super().__init__()
        self.setWindowFlags(_FLAGS_TOP)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._frame = frame
        self._drag_offset = None
        container = QWidget()
        container.setObjectName("regCtrl")
        lay = QHBoxLayout(container)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        container.setFixedHeight(_STATUS_BAR_HEIGHT)
        self._handle = _RegionFrameDragHandle(frame, self)
        lay.addWidget(self._handle)
        self._btn_pin = QPushButton()
        self._btn_pin.setCheckable(True)
        self._btn_pin.setFixedHeight(26)
        self._btn_pin.setStyleSheet("padding:2px 8px;font-size:12px;")
        self._btn_pin.clicked.connect(self._on_pin)
        lay.addWidget(self._btn_pin)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self.setStyleSheet(CTRL_STYLE)
        self.apply_ui_language()
        self._layer_owner: int | None = None

    def apply_ui_language(self):
        self._handle.setToolTip(_t("frame_drag"))
        self._sync_pin_text()
        self.adjustSize()

    def set_layer_owner(self, owner_hwnd: int | None) -> None:
        self._layer_owner = int(owner_hwnd) if owner_hwnd else None
        set_overlay_layer(self, self._layer_owner)
        if self.isVisible():
            _allow_capture(self)

    def restack_layer(self) -> None:
        if not self.isVisible():
            return
        if QApplication.activePopupWidget() is not None:
            return
        if self._layer_owner:
            restack_above_owner(self, self._layer_owner)
        else:
            set_overlay_layer(self, None)

    def _sync_pin_text(self):
        on = self._frame.pinned
        self._btn_pin.blockSignals(True)
        self._btn_pin.setChecked(on)
        self._btn_pin.blockSignals(False)
        self._btn_pin.setText(_t("frame_pin_on") if on else _t("frame_pin_off"))
        self._btn_pin.setToolTip(_t("frame_pinned") if on else _t("frame_drag"))

    def _on_pin(self):
        self._frame.set_pinned(self._btn_pin.isChecked())
        self._sync_pin_text()

    def place_above(self, rect: tuple[int, int, int, int]):
        """贴在识别区上方左侧（右侧留给备注控制条，与窗口翻译一致不抢位）。"""
        x, y, w, h = rect
        self.adjustSize()
        nx = x
        ny = y - self.height() - 4
        first = not self.isVisible()
        _move_if_changed(self, nx, ny)
        _show_once(self)
        if first:
            _allow_capture(self)
        self.restack_layer()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._frame.pinned:
            cr = self._frame.content_rect()
            self._drag_offset = event.globalPosition().toPoint() - QPoint(cr[0], cr[1])
            event.accept()

    def mouseMoveEvent(self, event):
        if (
            self._drag_offset is not None
            and not self._frame.pinned
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            p = event.globalPosition().toPoint() - self._drag_offset
            self._frame.move_content_to(p.x(), p.y())
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            self._frame._emit_moved()
            event.accept()


class RegionWatchFrame(_CaptureAllowedMixin, QWidget):
    """区域翻译识别框：仅描边识别区 + 右下角缩放。

    顶栏控制（拖动 / 固定）与窗口翻译共用同一套浮层控制条样式（CTRL_STYLE），
    不再使用自绘深色顶栏。

    中心区域通过 setMask 镂空，鼠标点击穿透到下层窗口；仅边框可点用于缩放。
    """

    region_moved = Signal(int, int, int, int)

    _EDGE = 10
    _MIN_W = 100
    _MIN_H = 60

    def __init__(self):
        super().__init__()
        self.setWindowFlags(_FLAGS_TOP)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._pinned = False
        self._resize_edge: str | None = None
        self._resize_origin = None
        self._external_control_bar = False
        self._ctrl = _RegionCtrl(self)

    def set_control_bar_external(self, enabled: bool) -> None:
        """区域模式把识别框操作并入当前字幕/备注状态栏。"""
        self._external_control_bar = bool(enabled)
        if self._external_control_bar:
            self._ctrl.hide()
        elif self.isVisible():
            self._place_ctrl()
        self.restack_layer()

    def apply_ui_language(self):
        self._ctrl.apply_ui_language()
        self._place_ctrl()
        self.update()

    def restack_layer(self) -> None:
        """识别框和控制条始终处在全局 TOPMOST 层最前，且不抢焦点。"""
        if QApplication.activePopupWidget() is not None:
            return
        widgets = (self,) if self._external_control_bar else (self, self._ctrl)
        for widget in widgets:
            if widget.isVisible():
                set_overlay_layer(widget, None)
                _allow_capture(widget)

    def show_region(self, rect: tuple[int, int, int, int], *, pinned: bool = False):
        """显示 OCR 识别区描边；控制条贴在区域上方外侧。"""
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            self.hide_frame()
            return
        self._pinned = bool(pinned)
        self._resize_edge = None
        nw = max(w, self._MIN_W)
        nh = max(h, self._MIN_H)
        first = not self.isVisible()
        _set_geo_if_changed(self, x, y, nw, nh)
        self._update_hit_mask()
        _show_once(self)
        if first:
            _allow_capture(self)
        self._ctrl._sync_pin_text()
        self._place_ctrl()
        self.restack_layer()
        self.update()

    def _place_ctrl(self):
        if self._external_control_bar:
            self._ctrl.hide()
        elif self.isVisible():
            self._ctrl.place_above(self.content_rect())

    def content_rect(self) -> tuple[int, int, int, int]:
        """当前 OCR 识别区 = 本窗几何。"""
        g = self.geometry()
        return g.x(), g.y(), g.width(), g.height()

    def move_content_to(self, x: int, y: int):
        """控制条拖动：移动识别区。"""
        if self._pinned:
            return
        g = self.geometry()
        self.move(x, y)
        self._place_ctrl()
        self.region_moved.emit(x, y, g.width(), g.height())

    def _emit_moved(self):
        self.region_moved.emit(*self.content_rect())

    @property
    def pinned(self) -> bool:
        return self._pinned

    def set_pinned(self, on: bool):
        self._pinned = bool(on)
        self._resize_edge = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._ctrl._sync_pin_text()
        self._update_hit_mask()
        self.update()

    def hide_frame(self):
        if self._resize_edge is not None:
            try:
                self.releaseMouse()
            except Exception:
                pass
        self._resize_edge = None
        self._resize_origin = None
        self._pinned = False
        try:
            self._ctrl.hide()
        except Exception:
            pass
        self.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 拖动缩放时也要刷新镂空，保证中心始终穿透
        self._update_hit_mask()

    def _update_hit_mask(self):
        """仅边框接收鼠标；中心镂空点击穿透到下层应用。"""
        e = self._EDGE
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        if w <= e * 2 or h <= e * 2:
            # 过小无法镂空，整窗可点（仍可缩放）
            self.clearMask()
            return
        outer = QRegion(0, 0, w, h)
        inner = QRegion(e, e, w - 2 * e, h - 2 * e)
        self.setMask(outer.subtracted(inner))

    def _hit_resize_edge(self, pos) -> str | None:
        e = self._EDGE
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        left = x <= e
        right = x >= w - e
        top = y <= e
        bottom = y >= h - e
        if top and left:
            return "tl"
        if top and right:
            return "tr"
        if bottom and left:
            return "bl"
        if bottom and right:
            return "br"
        if left:
            return "l"
        if right:
            return "r"
        if top:
            return "t"
        if bottom:
            return "b"
        return None

    def _cursor_for_edge(self, edge: str | None):
        m = {
            "l": Qt.CursorShape.SizeHorCursor,
            "r": Qt.CursorShape.SizeHorCursor,
            "t": Qt.CursorShape.SizeVerCursor,
            "b": Qt.CursorShape.SizeVerCursor,
            "tl": Qt.CursorShape.SizeFDiagCursor,
            "br": Qt.CursorShape.SizeFDiagCursor,
            "tr": Qt.CursorShape.SizeBDiagCursor,
            "bl": Qt.CursorShape.SizeBDiagCursor,
        }
        return m.get(edge, Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._pinned:
            return
        edge = self._hit_resize_edge(event.position().toPoint())
        if edge:
            self._resize_edge = edge
            self._resize_origin = (
                event.globalPosition().toPoint(),
                self.geometry(),
            )
            # 镂空后光标易离开边框，grab 保证拖动跟手
            self.grabMouse()
            event.accept()

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._resize_edge is None and not self._pinned:
            self.setCursor(self._cursor_for_edge(self._hit_resize_edge(pos)))

        if (
            self._resize_edge
            and self._resize_origin
            and not self._pinned
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            gpos0, geo0 = self._resize_origin
            d = event.globalPosition().toPoint() - gpos0
            x, y, w, h = geo0.x(), geo0.y(), geo0.width(), geo0.height()
            edge = self._resize_edge
            if "l" in edge:
                nw = max(self._MIN_W, w - d.x())
                x = x + (w - nw)
                w = nw
            if "r" in edge:
                w = max(self._MIN_W, w + d.x())
            if "t" in edge:
                nh = max(self._MIN_H, h - d.y())
                y = y + (h - nh)
                h = nh
            if "b" in edge:
                h = max(self._MIN_H, h + d.y())
            self.setGeometry(x, y, w, h)
            self._update_hit_mask()
            self._place_ctrl()
            self.region_moved.emit(*self.content_rect())
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._resize_edge is not None:
            self._resize_edge = None
            self._resize_origin = None
            try:
                self.releaseMouse()
            except Exception:
                pass
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._update_hit_mask()
            self._place_ctrl()
            self.region_moved.emit(*self.content_rect())
            event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        # 仅识别区描边（与窗口翻译一致：控制在外侧浮层，不自绘顶栏）
        painter.setPen(QPen(BORDER_QCOLOR, 2))
        painter.drawRect(1, 1, max(0, w - 3), max(0, h - 3))
        if not self._pinned:
            painter.save()
            painter.translate(w - 18, h - 18)
            paint_size_grip(painter, 18, 18)
            painter.restore()
