# -*- coding: utf-8 -*-
"""统一 UI 样式（对齐截图/划词翻译结果窗的视觉，但不改动该窗本身）。

InputTranslateWindow 保持独立样式表；其它窗口、控制条、滚动条、缩放把手
应引用本模块，避免各处颜色/圆角不一致。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizeGrip, QWidget

# —— 现代 Fluent 半透明暗色设计令牌 ——
PANEL_BG = "rgba(18, 22, 30, 235)"
PANEL_BORDER = "rgba(255, 255, 255, 28)"
PANEL_RADIUS = 10
TEXT = "#ffffff"
TEXT_MUTED = "rgba(255, 255, 255, 170)"
FIELD_BG = "rgba(255, 255, 255, 20)"
FIELD_BORDER = "rgba(255, 255, 255, 45)"
FIELD_BORDER_FOCUS = "rgba(0, 150, 255, 180)"
BTN_BG = "rgba(255, 255, 255, 30)"
BTN_HOVER = "rgba(255, 255, 255, 52)"
BTN_PRESSED = "rgba(255, 255, 255, 18)"
BTN_CHECKED = "rgba(0, 140, 255, 180)"
DROPDOWN_BG = "#191d26"
DROPDOWN_FG = "#ffffff"

# QPainter 用
PANEL_QCOLOR = QColor(18, 22, 30, 230)
TEXT_QCOLOR = QColor(255, 255, 255)
MUTED_QCOLOR = QColor(255, 255, 255, 175)
FIELD_QCOLOR = QColor(255, 255, 255, 25)
ACCENT_QCOLOR = QColor(0, 140, 255, 180)
BORDER_QCOLOR = QColor(255, 255, 255, 55)


class CornerSizeGrip(QSizeGrip):
    """右下角缩放：自绘三道斜线，深色面板上高对比抗锯齿。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("拖动调整大小")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        for i, alpha in enumerate((230, 170, 110)):
            off = 4 + i * 4
            p.setPen(QPen(QColor(255, 255, 255, alpha), 1.6))
            p.drawLine(w - 3, h - off, w - off, h - 3)


# 现代深色右键菜单
MENU_STYLE = """
QMenu {
  background: rgba(22, 26, 36, 248);
  border: 1px solid rgba(255, 255, 255, 36);
  border-radius: 8px;
  padding: 5px;
}
QMenu::item {
  color: #fff;
  padding: 6px 22px 6px 14px;
  border-radius: 5px;
  font-size: 12px;
}
QMenu::item:selected {
  background: rgba(0, 140, 255, 160);
  color: #fff;
}
QMenu::item:disabled {
  color: rgba(255, 255, 255, 90);
}
QMenu::separator {
  height: 1px;
  background: rgba(255, 255, 255, 25);
  margin: 4px 6px;
}
"""

# 现代气泡工具提示
TOOLTIP_STYLE = """
QToolTip {
  background: rgba(18, 22, 30, 240);
  color: #ffffff;
  border: 1px solid rgba(255, 255, 255, 45);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 12px;
}
"""

# 浮层控制条（字幕 / 备注 / 识别框）
CTRL_STYLE = f"""
#ctrl,#annCtrl,#regCtrl {{
  background: rgba(15, 18, 26, 235);
  border: 1px solid rgba(255, 255, 255, 25);
  border-radius: 6px;
}}
QLabel {{
  color: {TEXT};
  background: transparent;
  font-size: 12px;
}}
QLabel#dragHandle {{
  background: transparent;
  border: none;
}}
QPushButton {{
  background: {BTN_BG};
  color: {TEXT};
  border: 1px solid rgba(255, 255, 255, 18);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
}}
QPushButton:hover {{
  background: {BTN_HOVER};
  border-color: rgba(255, 255, 255, 40);
}}
QPushButton:pressed {{
  background: {BTN_PRESSED};
}}
QPushButton:checked {{
  background: {BTN_CHECKED};
  border-color: rgba(0, 180, 255, 220);
  font-weight: 600;
}}
{TOOLTIP_STYLE}
"""

# 独立滚动条（字幕条右侧）
SCROLLBAR_STYLE = f"""
QScrollBar:vertical {{
  background: rgba(0, 0, 0, 110);
  width: 10px;
  margin: 2px;
  border-radius: 4px;
}}
QScrollBar::handle:vertical {{
  background: rgba(255, 255, 255, 75);
  min-height: 28px;
  border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
  background: rgba(255, 255, 255, 130);
}}
QScrollBar::handle:vertical:disabled {{
  background: rgba(255, 255, 255, 28);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
  height: 0;
  width: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
  background: transparent;
}}
"""

# 设置 / 历史 / 选窗等完整面板
FLOAT_PANEL_STYLE = f"""
#panel {{
  background: {PANEL_BG};
  border: 1px solid {PANEL_BORDER};
  border-radius: {PANEL_RADIUS}px;
}}
QLabel {{
  color: {TEXT};
  background: transparent;
}}
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox, QListWidget, QTableWidget {{
  background: {FIELD_BG};
  color: {TEXT};
  border: 1px solid {FIELD_BORDER};
  border-radius: 6px;
  selection-background-color: rgba(0, 150, 255, 140);
  selection-color: #fff;
  padding: 3px 6px;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
  border: 1px solid {FIELD_BORDER_FOCUS};
  background: rgba(0, 0, 0, 60);
}}
QPlainTextEdit {{
  font-family: Consolas, 'Cascadia Mono', 'Microsoft YaHei UI', monospace;
  font-size: 12px;
}}
QHeaderView::section {{
  background: rgba(255, 255, 255, 18);
  color: {TEXT};
  border: none;
  padding: 6px 8px;
  font-weight: 600;
  border-bottom: 1px solid rgba(255, 255, 255, 28);
}}
QTableWidget {{
  gridline-color: transparent;
  alternate-background-color: rgba(255, 255, 255, 8);
}}
QTableWidget::item {{
  padding: 4px;
  border-bottom: 1px solid rgba(255, 255, 255, 12);
}}
QTableWidget::item:selected {{
  background: rgba(0, 140, 255, 120);
  color: #fff;
}}
QListWidget::item {{
  padding: 4px 8px;
  border-radius: 4px;
}}
QListWidget::item:hover {{
  background: rgba(255, 255, 255, 15);
}}
QListWidget::item:selected {{
  background: rgba(0, 140, 255, 140);
  color: #fff;
}}
QComboBox {{
  padding: 3px 8px;
}}
QComboBox::drop-down {{
  border: none;
  width: 20px;
}}
QComboBox QAbstractItemView {{
  background: {DROPDOWN_BG};
  color: {DROPDOWN_FG};
  selection-background-color: {BTN_CHECKED};
  selection-color: #fff;
  border: 1px solid {FIELD_BORDER};
  border-radius: 6px;
  padding: 4px;
}}
QPushButton {{
  background: {BTN_BG};
  color: {TEXT};
  border: 1px solid rgba(255, 255, 255, 20);
  border-radius: 6px;
  padding: 5px 12px;
  font-size: 12px;
}}
QPushButton:hover {{
  background: {BTN_HOVER};
  border-color: rgba(255, 255, 255, 40);
}}
QPushButton:pressed {{
  background: {BTN_PRESSED};
}}
QPushButton:checked {{
  background: {BTN_CHECKED};
  border-color: rgba(0, 180, 255, 200);
}}
QPushButton:disabled {{
  color: rgba(255, 255, 255, 80);
  background: rgba(255, 255, 255, 14);
  border-color: transparent;
}}
QCheckBox {{
  color: {TEXT};
  spacing: 8px;
  background: transparent;
}}
QCheckBox::indicator {{
  width: 15px;
  height: 15px;
  border: 1px solid {FIELD_BORDER};
  border-radius: 4px;
  background: {FIELD_BG};
}}
QCheckBox::indicator:hover {{
  border-color: rgba(0, 150, 255, 160);
}}
QCheckBox::indicator:checked {{
  background: {BTN_CHECKED};
  border-color: rgba(0, 180, 255, 220);
}}
QSpinBox::up-button, QSpinBox::down-button {{
  background: {BTN_BG};
  border: none;
  width: 16px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
  background: {BTN_HOVER};
}}
QScrollBar:vertical {{
  background: rgba(0, 0, 0, 70);
  width: 10px;
  margin: 2px;
  border-radius: 4px;
}}
QScrollBar::handle:vertical {{
  background: rgba(255, 255, 255, 70);
  min-height: 28px;
  border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
  background: rgba(255, 255, 255, 120);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
  height: 0;
  width: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
  background: transparent;
}}
QScrollBar:horizontal {{
  background: rgba(0, 0, 0, 70);
  height: 10px;
  margin: 2px;
  border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
  background: rgba(255, 255, 255, 70);
  min-width: 28px;
  border-radius: 4px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
  width: 0;
  height: 0;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
  background: transparent;
}}
{MENU_STYLE}
{TOOLTIP_STYLE}
"""

# 高级设置面板（侧栏 + 卡片）
SETTINGS_STYLE = f"""
#panel {{
  background: rgba(16, 20, 28, 246);
  border: 1px solid rgba(255, 255, 255, 32);
  border-radius: 12px;
}}
#titleBar {{
  background: transparent;
}}
#titleLabel {{
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.5px;
  background: transparent;
}}
#subtitle {{
  color: rgba(255, 255, 255, 120);
  font-size: 11px;
  background: transparent;
}}
#sideNav {{
  background: rgba(255, 255, 255, 6);
  border: 1px solid rgba(255, 255, 255, 14);
  border-radius: 10px;
  padding: 6px;
}}
#sideNav QPushButton {{
  text-align: left;
  padding: 10px 14px;
  border-radius: 8px;
  background: transparent;
  color: rgba(255, 255, 255, 180);
  border: none;
  font-size: 13px;
}}
#sideNav QPushButton:hover {{
  background: rgba(255, 255, 255, 16);
  color: #fff;
}}
#sideNav QPushButton:checked {{
  background: rgba(0, 140, 255, 55);
  color: #fff;
  border-left: 3px solid rgba(0, 180, 255, 240);
  border-top: 1px solid rgba(0, 150, 255, 70);
  border-right: 1px solid rgba(0, 150, 255, 70);
  border-bottom: 1px solid rgba(0, 150, 255, 70);
  font-weight: 600;
}}
#card {{
  background: rgba(255, 255, 255, 8);
  border: 1px solid rgba(255, 255, 255, 16);
  border-radius: 10px;
}}
#cardTitle {{
  color: rgba(255, 255, 255, 230);
  font-size: 13px;
  font-weight: 600;
  background: transparent;
  padding: 2px 0 6px 0;
}}
#cardHint {{
  color: rgba(255, 255, 255, 120);
  font-size: 11px;
  background: transparent;
}}
#footer {{
  background: transparent;
  border-top: 1px solid rgba(255, 255, 255, 16);
}}
QLabel {{
  color: {TEXT};
  background: transparent;
}}
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
  background: rgba(0, 0, 0, 85);
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 42);
  border-radius: 6px;
  selection-background-color: rgba(0, 150, 255, 140);
  selection-color: #fff;
  padding: 5px 8px;
  min-height: 22px;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
  border: 1px solid rgba(0, 160, 255, 200);
}}
QPlainTextEdit {{
  font-family: Consolas, 'Cascadia Mono', 'Microsoft YaHei UI', monospace;
  font-size: 12px;
  border-radius: 8px;
}}
QComboBox {{
  padding: 4px 8px;
}}
QComboBox::drop-down {{
  border: none;
  width: 20px;
}}
QComboBox QAbstractItemView {{
  background: #191d26;
  color: #eee;
  selection-background-color: {BTN_CHECKED};
  selection-color: #fff;
  border: 1px solid rgba(255, 255, 255, 40);
  border-radius: 6px;
  padding: 4px;
}}
QPushButton {{
  background: rgba(255, 255, 255, 30);
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 20);
  border-radius: 6px;
  padding: 6px 14px;
  font-size: 12px;
}}
QPushButton:hover {{
  background: rgba(255, 255, 255, 48);
  border-color: rgba(255, 255, 255, 36);
}}
QPushButton:pressed {{
  background: rgba(255, 255, 255, 20);
}}
QPushButton:checked {{
  background: {BTN_CHECKED};
  border-color: rgba(0, 180, 255, 220);
}}
QPushButton#primaryBtn {{
  background: rgba(0, 130, 255, 210);
  border: 1px solid rgba(0, 180, 255, 240);
  color: #fff;
  font-weight: 600;
  padding: 8px 22px;
  border-radius: 8px;
}}
QPushButton#primaryBtn:hover {{
  background: rgba(20, 150, 255, 235);
}}
QPushButton#primaryBtn:pressed {{
  background: rgba(0, 110, 220, 220);
}}
QPushButton#ghostBtn {{
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 40);
  color: rgba(255, 255, 255, 210);
}}
QPushButton#ghostBtn:hover {{
  background: rgba(255, 255, 255, 18);
  border-color: rgba(255, 255, 255, 60);
}}
QPushButton#closeBtn {{
  background: transparent;
  color: rgba(255, 255, 255, 180);
  border: none;
  border-radius: 6px;
  font-size: 16px;
  padding: 2px 8px;
}}
QPushButton#closeBtn:hover {{
  background: rgba(255, 75, 75, 180);
  color: #fff;
}}
QCheckBox {{
  color: {TEXT};
  spacing: 8px;
  background: transparent;
}}
QCheckBox::indicator {{
  width: 16px;
  height: 16px;
  border: 1px solid rgba(255, 255, 255, 50);
  border-radius: 4px;
  background: rgba(0, 0, 0, 80);
}}
QCheckBox::indicator:hover {{
  border-color: rgba(0, 150, 255, 180);
}}
QCheckBox::indicator:checked {{
  background: rgba(0, 150, 255, 190);
  border-color: rgba(0, 180, 255, 230);
}}
QSpinBox::up-button, QSpinBox::down-button {{
  background: rgba(255, 255, 255, 28);
  border: none;
  width: 16px;
}}
QScrollBar:vertical {{
  background: rgba(0, 0, 0, 60);
  width: 10px;
  margin: 2px;
  border-radius: 5px;
}}
QScrollBar::handle:vertical {{
  background: rgba(255, 255, 255, 70);
  min-height: 28px;
  border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
  background: rgba(255, 255, 255, 120);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
  height: 0;
  width: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
  background: transparent;
}}
{MENU_STYLE}
{TOOLTIP_STYLE}
"""


def apply_frameless_float(widget: QWidget, *, tool: bool = True) -> None:
    """无边框、置顶、透明底（与翻译结果窗一致）。"""
    flags = (
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
    )
    if tool:
        flags |= Qt.WindowType.Tool
    widget.setWindowFlags(flags)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def paint_size_grip(painter: QPainter, width: int, height: int) -> None:
    """在任意控件上画与翻译窗一致的右下角三道斜线。"""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    for i, alpha in enumerate((230, 170, 110)):
        off = 4 + i * 4
        painter.setPen(QPen(QColor(255, 255, 255, alpha), 1.6))
        painter.drawLine(width - 3, height - off, width - off, height - 3)

