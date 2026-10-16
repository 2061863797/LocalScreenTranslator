# -*- coding: utf-8 -*-
"""原位替换的控制条：持续翻译与截屏翻译共用同一外观和按钮顺序。

持续翻译（watch）：⠿ 拖动 · 固定 · 目标语言 · 字幕条 · 对比 · 暂停 · 关闭
截屏翻译（screenshot）：⠿ 拖动 · 目标语言 · 结果窗口 · 对比 · 复制 · 保存 · 关闭
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ..i18n import t as _t
from .language_popup import LanguagePairButton
from .overlay_base import (
    _FLAGS_TOP,
    _STATUS_BAR_HEIGHT,
    _CaptureAllowedMixin,
    _DragHandle,
    _FloatingCtrlMixin,
    _allow_capture,
    _move_if_changed,
    _show_once,
)
from .theme import CTRL_STYLE


class _RegionDragHandle(_DragHandle):
    """控制条中的识别区拖动手柄。"""

    def __init__(self, owner):
        super().__init__(owner, height=24)
        self._owner = owner

    def _can_drag(self) -> bool:
        frame = self._owner._region_frame
        return frame is not None and frame.isVisible() and not frame.pinned

    def _drag_origin(self) -> QPoint:
        x, y, _, _ = self._owner._region_frame.content_rect()
        return QPoint(x, y)

    def _drag_to(self, point: QPoint) -> None:
        self._owner._region_frame.move_content_to(point.x(), point.y())


class _OverlayDragHandle(_DragHandle):
    """截屏控制条中的拖动手柄：整体拖动截屏覆盖层，控制条跟随。"""

    def __init__(self, owner):
        super().__init__(owner, height=24)
        self._owner = owner

    def _can_drag(self) -> bool:
        target = self._owner._drag_target
        return target is not None and target.isVisible()

    def _drag_origin(self) -> QPoint:
        return self._owner._drag_target.pos()

    def _drag_to(self, point: QPoint) -> None:
        target = self._owner._drag_target
        target.move(point)
        self._owner.place_above((point.x(), point.y(), target.width(), target.height()))

    def _drag_finished(self) -> None:
        target = self._owner._drag_target
        if target is not None:
            self._owner.target_moved.emit(target.x(), target.y(), target.width(), target.height())


class OverlayCtrl(_FloatingCtrlMixin, _CaptureAllowedMixin, QWidget):
    """贴在目标区域左上方外侧的控制条；区域顶到屏幕上沿时改贴在区域内侧。"""

    stop_requested = Signal()
    # 持续翻译：切到字幕条；截屏：切到结果窗口
    switch_requested = Signal()
    pause_changed = Signal(bool)
    language_changed = Signal(str, str)
    compare_changed = Signal(bool)
    copy_requested = Signal()
    save_requested = Signal()
    # 截屏：拖动手柄移动了覆盖层（新的 x, y, w, h）
    target_moved = Signal(int, int, int, int)

    def __init__(self, kind: str = "watch"):
        super().__init__()
        assert kind in ("watch", "screenshot")
        self._kind = kind
        self.setWindowFlags(_FLAGS_TOP)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._region_frame = None
        self._region_controls_visible = False
        self._drag_target: QWidget | None = None
        container = QWidget()
        container.setObjectName("overlayCtrl")
        container.setFixedHeight(_STATUS_BAR_HEIGHT)
        lay = QHBoxLayout(container)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)

        self._handle = _RegionDragHandle(self)
        self._overlay_handle = _OverlayDragHandle(self)
        self._btn_region_pin = QPushButton()
        self._btn_region_pin.setCheckable(True)
        self._btn_region_pin.clicked.connect(self._toggle_region_pin)
        self._language_button = LanguagePairButton(self)
        self._language_button.changed.connect(self.language_changed.emit)
        self._btn_switch = QPushButton()
        self._btn_switch.clicked.connect(self.switch_requested.emit)
        self._btn_compare = QPushButton()
        self._btn_compare.setCheckable(True)
        self._btn_compare.toggled.connect(self.compare_changed.emit)
        self._btn_copy = QPushButton()
        self._btn_copy.clicked.connect(self.copy_requested.emit)
        self._btn_save = QPushButton()
        self._btn_save.clicked.connect(self.save_requested.emit)
        self._btn_pause = QPushButton()
        self._btn_pause.setCheckable(True)
        self._btn_pause.toggled.connect(self.pause_changed.emit)
        self._btn_close = QPushButton()
        self._btn_close.clicked.connect(self.stop_requested.emit)

        if kind == "watch":
            widgets = (
                self._handle, self._btn_region_pin, self._language_button,
                self._btn_switch, self._btn_compare, self._btn_pause, self._btn_close,
            )
        else:
            widgets = (
                self._overlay_handle, self._language_button, self._btn_switch,
                self._btn_compare, self._btn_copy, self._btn_save, self._btn_close,
            )
        for widget in widgets:
            lay.addWidget(widget)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self.setStyleSheet(CTRL_STYLE)
        self.apply_ui_language()
        self.set_region_controls_visible(False)

    @property
    def kind(self) -> str:
        return self._kind

    def apply_ui_language(self) -> None:
        self._handle.setToolTip(_t("frame_drag"))
        self._overlay_handle.setToolTip(_t("shot_drag_tip"))
        self._sync_region_pin()
        if self._kind == "watch":
            self._btn_switch.setText(_t("ctrl_to_subtitle"))
            self._btn_switch.setToolTip(_t("ctrl_to_subtitle_tip"))
            self._btn_compare.setToolTip(_t("ann_compare_tip"))
            self._btn_close.setToolTip(_t("ann_close_tip"))
        else:
            self._btn_switch.setText(_t("ctrl_to_window"))
            self._btn_switch.setToolTip(_t("ctrl_to_window_tip"))
            self._btn_compare.setToolTip(_t("shot_compare_tip"))
            self._btn_close.setToolTip(_t("shot_close_tip"))
        self._btn_compare.setText(_t("shot_compare"))
        self._btn_copy.setText(_t("shot_copy"))
        self._btn_copy.setToolTip(_t("shot_copy_tip"))
        self._btn_save.setText(_t("shot_save"))
        self._btn_save.setToolTip(_t("shot_save_tip"))
        self._btn_pause.setText(
            _t("sub_resume") if self._btn_pause.isChecked() else _t("sub_pause")
        )
        self._btn_close.setText(_t("sub_close"))
        for btn in (
            self._btn_region_pin, self._btn_switch, self._btn_compare, self._btn_copy,
            self._btn_save, self._btn_pause, self._btn_close,
        ):
            btn.setFixedHeight(26)
            btn.setStyleSheet("padding:2px 8px;font-size:12px;")
        self.adjustSize()

    def set_drag_target(self, widget: QWidget | None) -> None:
        """截屏控制条的拖动手柄所移动的覆盖层。"""
        self._drag_target = widget

    # ---------- 区域识别框 ----------

    def set_region_frame(self, frame) -> None:
        self._region_frame = frame
        self.set_region_controls_visible(self._region_controls_visible)
        self.apply_ui_language()

    def set_region_controls_visible(self, visible: bool) -> None:
        """拖动、固定只在区域持续翻译显示。"""
        self._region_controls_visible = bool(
            visible and self._kind == "watch" and self._region_frame is not None
        )
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
        self._btn_region_pin.setToolTip(_t("frame_pinned") if pinned else _t("frame_drag"))

    # ---------- 状态同步（不重复发出信号） ----------

    def set_paused(self, paused: bool) -> None:
        self._btn_pause.blockSignals(True)
        self._btn_pause.setChecked(bool(paused))
        self._btn_pause.blockSignals(False)
        self.apply_ui_language()

    def set_comparing(self, comparing: bool) -> None:
        self._btn_compare.blockSignals(True)
        self._btn_compare.setChecked(bool(comparing))
        self._btn_compare.blockSignals(False)

    def set_switch_visible(self, visible: bool) -> None:
        """窗口持续翻译没有字幕条，隐藏切换按钮。"""
        self._btn_switch.setVisible(bool(visible))
        self.adjustSize()

    def sync_languages(self, source: str, target: str) -> None:
        self._language_button.sync_languages(source, target)
        self.adjustSize()

    def place_above(self, rect: tuple[int, int, int, int]) -> None:
        x, y, w, h = rect
        self.adjustSize()
        top = y - self.height() - 4
        screen = QGuiApplication.screenAt(QPoint(x + w // 2, y + h // 2))
        if screen is not None and top < screen.availableGeometry().top():
            top = y + 4
        first = not self.isVisible()
        _move_if_changed(self, x, top)
        _show_once(self)
        if first:
            _allow_capture(self)
        self.restack_layer()


__all__ = ["OverlayCtrl"]
