# -*- coding: utf-8 -*-
"""选择要持续翻译的窗口的对话框。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import capture
from ..i18n import t as _t
from .theme import CornerSizeGrip, FLOAT_PANEL_STYLE, apply_frameless_float
from .topmost import center_on_cursor_screen, raise_to_front


class WindowPicker(QDialog):
    """列出可见顶层窗口，返回选中的 hwnd（selected_hwnd，未选为 None）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        apply_frameless_float(self, tool=False)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(460, 420)
        self.setMinimumSize(340, 280)
        self.selected_hwnd: int | None = None

        self._search = QLineEdit()
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter_list)

        self._list = QListWidget()
        self._list.setSpacing(2)
        self._refresh()

        self._btn_refresh = QPushButton()
        self._btn_refresh.setObjectName("ghostBtn")
        self._btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_ok = QPushButton()
        self._btn_ok.setObjectName("primaryBtn")
        self._btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_cancel = QPushButton()
        self._btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_refresh.clicked.connect(self._refresh)
        self._btn_ok.clicked.connect(self._confirm)
        self._btn_cancel.clicked.connect(self.reject)
        self._list.itemDoubleClicked.connect(lambda _: self._confirm())

        title_bar = QHBoxLayout()
        title_bar.setContentsMargins(4, 2, 2, 2)
        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet("font-size:14px;font-weight:600;")
        btn_close = QPushButton("×")
        btn_close.setObjectName("closeBtn")
        btn_close.setFixedSize(28, 26)
        btn_close.clicked.connect(self.reject)
        title_bar.addWidget(self._title_lbl)
        title_bar.addStretch()
        title_bar.addWidget(btn_close)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(self._btn_refresh)
        bar.addStretch()
        bar.addWidget(self._btn_ok)
        bar.addWidget(self._btn_cancel)

        container = QWidget()
        container.setObjectName("panel")
        inner = QVBoxLayout(container)
        inner.setContentsMargins(14, 12, 14, 10)
        inner.setSpacing(8)
        inner.addLayout(title_bar)
        self._hint = QLabel()
        self._hint.setStyleSheet("color:rgba(255,255,255,140);font-size:11px;")
        inner.addWidget(self._hint)
        inner.addWidget(self._search)
        inner.addWidget(self._list, stretch=1)
        inner.addLayout(bar)
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch()
        grip_row.addWidget(CornerSizeGrip(container))
        inner.addLayout(grip_row)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self.setStyleSheet(FLOAT_PANEL_STYLE)
        self.apply_ui_language()

    def apply_ui_language(self):
        self.setWindowTitle(_t("pick_title"))
        self._title_lbl.setText(_t("pick_title"))
        self._hint.setText(_t("pick_hint"))
        self._search.setPlaceholderText(_t("pick_search"))
        self._btn_refresh.setText(_t("pick_refresh"))
        self._btn_ok.setText(_t("pick_ok"))
        self._btn_cancel.setText(_t("pick_cancel"))

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        self.apply_ui_language()
        center_on_cursor_screen(self)
        raise_to_front(self, activate=True)

    def _refresh(self):
        self._list.clear()
        for hwnd, title in capture.list_windows():
            text = title or f"hwnd={hwnd}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, hwnd)
            item.setToolTip(f"{text} (HWND: {hwnd})")
            self._list.addItem(item)
        self._filter_list(self._search.text())

    def _filter_list(self, query: str):
        needle = (query or "").strip().casefold()
        for i in range(self._list.count()):
            item = self._list.item(i)
            self._list.setItemHidden(item, bool(needle and needle not in item.text().casefold()))

    def _confirm(self):
        item = self._list.currentItem()
        if item is None:
            return
        self.selected_hwnd = int(item.data(Qt.ItemDataRole.UserRole))
        self.accept()
