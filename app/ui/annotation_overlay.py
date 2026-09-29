# -*- coding: utf-8 -*-
"""备注模式浮层与控制条组件（原文就近标注显示）。"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import t as _t
from .language_popup import LanguagePairButton
from .overlay_base import (
    _FLAGS_TOP,
    _STATUS_BAR_HEIGHT,
    _allow_capture,
    _CaptureAllowedMixin,
    _exclude_from_capture,
    _move_if_changed,
    _set_geo_if_changed,
    _show_once,
    dispatch_set_overlay_layer as set_overlay_layer,
)
from .theme import CTRL_STYLE
from .topmost import restack_above_owner


class _RegionDragHandle(QLabel):
    """备注状态栏中的识别区拖动手柄。"""

    def __init__(self, owner):
        super().__init__("⠿", owner)
        self._owner = owner
        self._drag_offset = None
        self.setObjectName("dragHandle")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(20, 24)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setStyleSheet(
            "QLabel#dragHandle{background:transparent;border:none;color:rgba(255,255,255,180);font-size:15px;padding:0;}"
            "QLabel#dragHandle:hover{color:#ffffff;}"
        )

    def mousePressEvent(self, event):
        frame = self._owner._region_frame
        if (
            event.button() == Qt.MouseButton.LeftButton
            and frame is not None
            and frame.isVisible()
            and not frame.pinned
        ):
            x, y, _, _ = frame.content_rect()
            self._drag_offset = event.globalPosition().toPoint() - QPoint(x, y)
            self.grabMouse()
            event.accept()

    def mouseMoveEvent(self, event):
        frame = self._owner._region_frame
        if (
            self._drag_offset is not None
            and frame is not None
            and not frame.pinned
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            point = event.globalPosition().toPoint() - self._drag_offset
            frame.move_content_to(point.x(), point.y())
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            try:
                self.releaseMouse()
            except Exception:
                pass
            event.accept()


class AnnotateCtrl(_CaptureAllowedMixin, QWidget):
    """备注模式控制条：区域拖动/固定、目标语言、切换字幕、暂停与关闭。"""

    stop_requested = Signal()
    switch_to_subtitle = Signal()  # 运行中切换到字幕条模式
    pause_changed = Signal(bool)
    language_changed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(_FLAGS_TOP)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._region_frame = None
        self._region_controls_visible = False
        container = QWidget()
        container.setObjectName("annCtrl")
        lay = QHBoxLayout(container)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        container.setFixedHeight(_STATUS_BAR_HEIGHT)
        self._handle = _RegionDragHandle(self)
        lay.addWidget(self._handle)
        self._btn_region_pin = QPushButton()
        self._btn_region_pin.setCheckable(True)
        self._btn_region_pin.setFixedHeight(26)
        self._btn_region_pin.clicked.connect(self._toggle_region_pin)
        lay.addWidget(self._btn_region_pin)
        self._language_button = LanguagePairButton(self)
        self._language_button.changed.connect(self.language_changed.emit)
        lay.addWidget(self._language_button)
        self._btn_sub = QPushButton()
        self._btn_sub.setFixedHeight(26)
        self._btn_sub.clicked.connect(self.switch_to_subtitle.emit)
        lay.addWidget(self._btn_sub)
        self._btn_pause = QPushButton()
        self._btn_pause.setCheckable(True)
        self._btn_pause.setFixedHeight(26)
        self._btn_pause.toggled.connect(self.pause_changed.emit)
        lay.addWidget(self._btn_pause)
        self._btn_close = QPushButton()
        self._btn_close.setFixedHeight(26)
        self._btn_close.clicked.connect(self.stop_requested.emit)
        lay.addWidget(self._btn_close)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self.setStyleSheet(CTRL_STYLE)
        self.apply_ui_language()
        self.set_region_controls_visible(False)
        self._layer_owner: int | None = None

    def apply_ui_language(self):
        self._handle.setToolTip(_t("frame_drag"))
        self._sync_region_pin()
        self._btn_sub.setText(_t("ann_subtitle"))
        self._btn_sub.setToolTip(_t("ann_subtitle_tip"))
        self._btn_pause.setText(
            _t("sub_resume") if self._btn_pause.isChecked() else _t("sub_pause")
        )
        self._btn_close.setText(_t("sub_close"))
        self._btn_close.setToolTip(_t("ann_close_tip"))
        for btn in (self._btn_sub, self._btn_pause, self._btn_close):
            btn.setFixedHeight(26)
            btn.setStyleSheet("padding:2px 8px;font-size:12px;")
        self.adjustSize()

    def set_region_frame(self, frame) -> None:
        self._region_frame = frame
        self._handle.setVisible(self._region_controls_visible)
        self._btn_region_pin.setVisible(self._region_controls_visible)
        self.apply_ui_language()

    def set_region_controls_visible(self, visible: bool) -> None:
        self._region_controls_visible = bool(visible and self._region_frame is not None)
        self._handle.setVisible(self._region_controls_visible)
        self._btn_region_pin.setVisible(self._region_controls_visible)
        self.adjustSize()

    def _toggle_region_pin(self) -> None:
        if self._region_frame is None:
            return
        self._region_frame.set_pinned(not self._region_frame.pinned)
        self._sync_region_pin()

    def _sync_region_pin(self) -> None:
        pinned = bool(self._region_frame and self._region_frame.pinned)
        self._btn_region_pin.blockSignals(True)
        self._btn_region_pin.setChecked(pinned)
        self._btn_region_pin.blockSignals(False)
        self._btn_region_pin.setText(_t("frame_pin_off"))
        self._btn_region_pin.setToolTip(
            _t("frame_pinned") if pinned else _t("frame_drag")
        )

    def set_paused(self, paused: bool):
        self._btn_pause.blockSignals(True)
        self._btn_pause.setChecked(bool(paused))
        self._btn_pause.blockSignals(False)
        self.apply_ui_language()

    def sync_languages(self, source: str, target: str) -> None:
        self._language_button.sync_languages(source, target)
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

    def set_subtitle_button_visible(self, visible: bool) -> None:
        """窗口模式不提供字幕切换；区域备注模式保留该按钮。"""
        self._btn_sub.setVisible(bool(visible))
        self.adjustSize()

    def place_above(self, win_rect: tuple[int, int, int, int]):
        """贴在目标左上角外侧，不挡内容。"""
        x, y, w, h = win_rect
        self.adjustSize()
        nx = x
        ny = y - self.height() - 4
        first = not self.isVisible()
        _move_if_changed(self, nx, ny)
        _show_once(self)
        if first:
            _allow_capture(self)
        self.restack_layer()


class AnnotationOverlay(_CaptureAllowedMixin, QWidget):
    """备注模式：每行译文贴在对应原文正下方（越界则贴上方），
    覆盖整个目标区域，鼠标完全穿透，不挡操作。

    默认布局以「对得上是哪一行」为优先，不做复杂空白搜索。
    """

    _DEFAULT_FONT_SIZE = 13

    def __init__(self):
        super().__init__()
        self.setWindowFlags(_FLAGS_TOP | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # [(box(x1,y1,x2,y2) 相对区域, 译文), ...]
        self._items: list[tuple[tuple[int, int, int, int], str]] = []
        self._text_color = QColor("#00F0FF")
        self._font_size = self._DEFAULT_FONT_SIZE
        self._layer_owner: int | None = None

    def _apply_capture_affinity(self) -> None:
        _exclude_from_capture(self)

    def showEvent(self, event: QShowEvent):
        # 备注层始终排除捕获，避免译文被下一轮 OCR 识别。
        QWidget.showEvent(self, event)
        self._apply_capture_affinity()

    def set_layer_owner(self, owner_hwnd: int | None) -> None:
        self._layer_owner = int(owner_hwnd) if owner_hwnd else None
        set_overlay_layer(self, self._layer_owner)
        if self.isVisible():
            self._apply_capture_affinity()

    def restack_layer(self) -> None:
        if not self.isVisible():
            return
        if self._layer_owner:
            restack_above_owner(self, self._layer_owner)
        else:
            set_overlay_layer(self, None)

    def set_text_color(self, color) -> None:
        """备注译文颜色：#RRGGBB 字符串或 QColor。"""
        c = QColor(color) if not isinstance(color, QColor) else QColor(color)
        if not c.isValid():
            c = QColor("#00F0FF")
        if c.rgb() == self._text_color.rgb() and c.alpha() == self._text_color.alpha():
            return
        self._text_color = c
        if self.isVisible():
            self.update()

    def set_font_size(self, size) -> None:
        """设置备注译文字号；0 或无效值恢复原有默认字号。"""
        try:
            requested = int(size)
        except (TypeError, ValueError):
            requested = 0
        resolved = (
            requested if 12 <= requested <= 20 else self._DEFAULT_FONT_SIZE
        )
        if self._font_size == resolved:
            return
        self._font_size = resolved
        if self.isVisible():
            self.update()

    def update_geometry(self, win_rect: tuple[int, int, int, int]):
        x, y, w, h = win_rect
        if _set_geo_if_changed(self, x, y, w, h):
            self.update()

    def set_items(self, items: list[tuple[tuple[int, int, int, int], str]]):
        self._items = items or []
        if not any(text for _, text in self._items):
            # 尚未显示时不创建空浮层；已显示时只清空画面，不反复隐藏窗口。
            if self.isVisible():
                self.update()
            return
        first = not self.isVisible()
        _show_once(self)
        if first:
            self._apply_capture_affinity()
        self.restack_layer()
        self.update()

    def clear(self):
        self._items = []
        self.hide()

    def _layout_items(
        self,
    ) -> tuple[QFont, list[tuple[tuple[int, int, int, int], str]]]:
        """统一计算屏幕绘制与 OCR 遮罩位置，避免两者坐标漂移。"""
        font = QFont()
        font.setPixelSize(self._font_size)
        fm = QFontMetrics(font)
        width, height = self.width(), self.height()
        layout = []
        for (x1, y1, x2, y2), text in self._items:
            if not text:
                continue
            text_width = fm.horizontalAdvance(text) + 10
            text_height = fm.height() + 4
            # 默认：贴原文正下方；下方越界则贴上方；水平对齐原文左缘
            draw_x = min(x1, max(0, width - text_width)) + 5
            draw_y = y2 + 2
            if draw_y + text_height > height:
                draw_y = y1 - text_height - 2
            if draw_y < 0:
                draw_y = 0
            draw_width = max(1, text_width - 10)
            layout.append((
                (draw_x, draw_y, draw_x + draw_width, draw_y + text_height),
                text,
            ))
        return font, layout

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if not self._items:
            return
        font, layout = self._layout_items()
        painter.setFont(font)
        painter.setPen(self._text_color)
        for (x1, y1, x2, y2), text in layout:
            painter.drawText(
                x1, y1, x2 - x1, y2 - y1,
                Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine,
                text,
            )
