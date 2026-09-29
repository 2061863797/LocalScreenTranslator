# -*- coding: utf-8 -*-
"""现代化 Fluent 风格目标语言切换弹窗与胶囊按钮组件。"""

from __future__ import annotations

import ctypes

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import t as _t
from ..translation_runtime.languages import LANGUAGES

_LANG_POPUP_CONTAINER_STYLE = """
QWidget#langPopupContainer {
    background-color: rgba(18, 22, 32, 248);
    border: 1px solid rgba(255, 255, 255, 38);
    border-radius: 12px;
}
QLabel#popupTitle {
    color: rgba(255, 255, 255, 175);
    font-size: 11px;
    font-weight: 600;
    padding-left: 2px;
}
"""

_LANG_ITEM_STYLE = """
QPushButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    color: #d8e2ea;
    font-size: 12px;
    text-align: left;
    padding: 4px 10px;
}
QPushButton:hover {
    background: rgba(255, 255, 255, 25);
    color: #ffffff;
}
"""

_LANG_ITEM_ACTIVE_STYLE = """
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(0, 180, 255, 210), stop:1 rgba(0, 120, 230, 230));
    border: 1px solid rgba(0, 220, 255, 140);
    border-radius: 6px;
    color: #ffffff;
    font-size: 12px;
    font-weight: bold;
    text-align: left;
    padding: 4px 10px;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(0, 195, 255, 235), stop:1 rgba(0, 135, 245, 250));
}
"""


class LanguageSelectPopup(QWidget):
    """现代化双列紧凑 Fluent 风格目标语言选择弹窗。"""

    language_selected = Signal(str)

    def __init__(self, current_target: str, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._current_target = current_target

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(0)

        container = QWidget(self)
        container.setObjectName("langPopupContainer")
        container.setStyleSheet(_LANG_POPUP_CONTAINER_STYLE)

        shadow = QGraphicsDropShadowEffect(container)
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 160))
        container.setGraphicsEffect(shadow)

        box_lay = QVBoxLayout(container)
        box_lay.setContentsMargins(12, 10, 12, 12)
        box_lay.setSpacing(8)

        # 顶部标题栏
        header_lay = QHBoxLayout()
        header_lay.setContentsMargins(0, 0, 0, 0)
        title_lbl = QLabel(_t("target_lang"), container)
        title_lbl.setObjectName("popupTitle")
        header_lay.addWidget(title_lbl)
        header_lay.addStretch()
        box_lay.addLayout(header_lay)

        # 双列网格 (7 行 2 列)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        for i, name in enumerate(LANGUAGES):
            row = i % 7
            col = i // 7
            btn = QPushButton(container)
            is_active = (name == self._current_target)
            btn.setStyleSheet(_LANG_ITEM_ACTIVE_STYLE if is_active else _LANG_ITEM_STYLE)
            btn.setText(f"✔  {name}" if is_active else f"   {name}")
            btn.setFixedHeight(28)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, val=name: self._on_selected(val))
            grid.addWidget(btn, row, col)

        box_lay.addLayout(grid)
        root_layout.addWidget(container)
        self.setFixedSize(330, 286)

    def _on_selected(self, lang: str):
        self.language_selected.emit(lang)
        self.close()

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        try:
            hwnd = int(self.winId())
            if hwnd:
                ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    -1,  # HWND_TOPMOST
                    0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0040,  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
                )
        except Exception:
            pass


class LanguagePairButton(QPushButton):
    """紧凑的目标语言菜单，适合窄的持续翻译控制条。"""

    changed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._target = "简体中文"
        self._compact = False
        self._popup = None
        self.setFixedHeight(26)
        self.setStyleSheet("padding:3px 8px;font-size:12px;")
        self.clicked.connect(self._open_menu)
        self.sync_languages("自动", self._target)

    def sync_languages(self, source: str, target: str) -> None:
        del source
        self._target = target
        self.setText("译" if self._compact else target)
        self.setToolTip(f"{_t('target_lang')}：{target}；点击选择目标语言")

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self.setFixedWidth(26) if compact else self.setMinimumWidth(0)
        self.setFixedHeight(26)
        self.setStyleSheet(
            "padding:2px 3px;font-size:12px;"
            if compact
            else "padding:2px 8px;font-size:12px;"
        )
        if not compact:
            self.setMaximumWidth(16777215)
        self.sync_languages("自动", self._target)

    def _open_menu(self) -> None:
        if self._popup is not None:
            try:
                self._popup.close()
            except Exception:
                pass
            self._popup = None

        popup = LanguageSelectPopup(self._target)
        self._popup = popup

        def _cleanup():
            if getattr(self, "_popup", None) is popup:
                self._popup = None

        popup.destroyed.connect(_cleanup)
        popup.language_selected.connect(
            lambda val: self.changed.emit("自动", val)
        )

        # 智能计算弹出位置
        btn_pos = self.mapToGlobal(QPoint(0, 0))
        btn_w, btn_h = self.width(), self.height()
        pop_w, pop_h = popup.width(), popup.height()

        screen = QGuiApplication.screenAt(btn_pos)
        if not screen:
            screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

        # 识别是否处于识别区上方控制栏（如 AnnotateCtrl / _RegionCtrl）
        is_above_ctrl = False
        p = self.parent()
        while p is not None:
            if type(p).__name__ in ("AnnotateCtrl", "_RegionCtrl") or p.objectName() in ("annCtrl", "regCtrl"):
                is_above_ctrl = True
                break
            p = p.parent()

        space_below = avail.bottom() - (btn_pos.y() + btn_h)
        space_above = btn_pos.y() - avail.top()

        y_up = btn_pos.y() - pop_h + 10
        y_down = btn_pos.y() + btn_h - 10

        if is_above_ctrl:
            # 备注控制栏贴在识别框正上方，优先向上弹以完全避开识别框；若上方空间不足则向下弹
            if space_above >= (pop_h - 20) or space_above >= space_below:
                y = y_up
            else:
                y = y_down
        else:
            # 字幕模式等贴在目标下方的浮层，优先向下弹；若下方放不下则向上翻转
            if space_below >= (pop_h - 20) or space_below >= space_above:
                y = y_down
            else:
                y = y_up

        # 水平对齐：左对齐按钮，且保证落在可用屏幕工作区内
        x = btn_pos.x() - 10
        if x + pop_w > avail.right():
            x = avail.right() - pop_w
        if x < avail.left():
            x = avail.left()

        # 上下安全夹紧，防止在极端小分辨率下出界
        y = max(avail.top(), min(y, avail.bottom() - pop_h))

        popup.move(x, y)
        popup.show()
