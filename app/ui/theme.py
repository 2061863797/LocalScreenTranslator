# -*- coding: utf-8 -*-
"""统一 UI 样式（对齐截图/划词翻译结果窗的视觉，但不改动该窗本身）。

InputTranslateWindow 保持独立样式表；其它窗口、控制条、滚动条、缩放把手
应引用本模块，避免各处颜色/圆角不一致。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizeGrip, QWidget

# —— 与 InputTranslateWindow 对齐的色值 ——
PANEL_BG = "rgba(0,0,0,175)"
PANEL_RADIUS = 8
TEXT = "#fff"
FIELD_BG = "rgba(255,255,255,28)"
FIELD_BORDER = "rgba(255,255,255,60)"
BTN_BG = "rgba(255,255,255,40)"
BTN_HOVER = "rgba(255,255,255,60)"
BTN_CHECKED = "rgba(0,150,255,150)"
DROPDOWN_BG = "#2b2b2b"
DROPDOWN_FG = "#eee"

# QPainter 用
PANEL_QCOLOR = QColor(0, 0, 0, 175)
TEXT_QCOLOR = QColor(255, 255, 255)
MUTED_QCOLOR = QColor(255, 255, 255, 180)
FIELD_QCOLOR = QColor(255, 255, 255, 40)
ACCENT_QCOLOR = QColor(0, 150, 255, 150)
BORDER_QCOLOR = QColor(255, 255, 255, 140)


class CornerSizeGrip(QSizeGrip):
    """右下角缩放：自绘三道斜线，深色面板上也能看清（对齐翻译窗把手形态）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("拖动调整大小")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        # 三道短斜线，由外到内略变淡，贴近系统 SizeGrip
        for i, alpha in enumerate((220, 180, 130)):
            off = 4 + i * 4
            p.setPen(QPen(QColor(255, 255, 255, alpha), 1.6))
            p.drawLine(w - 3, h - off, w - off, h - 3)

# 浮层控制条（字幕 / 备注）
CTRL_STYLE = f"""
#ctrl,#annCtrl,#regCtrl{{background:{PANEL_BG};border-radius:{PANEL_RADIUS}px;}}
QLabel{{color:{TEXT};background:transparent;font-size:12px;}}
QPushButton{{background:{BTN_BG};color:{TEXT};border:none;
border-radius:3px;padding:1px 5px;font-size:11px;}}
QPushButton:hover{{background:{BTN_HOVER};}}
QPushButton:checked{{background:{BTN_CHECKED};}}
"""

# 独立滚动条（字幕条右侧）
SCROLLBAR_STYLE = f"""
QScrollBar:vertical{{
  background:rgba(0,0,0,120);width:12px;margin:2px;border-radius:4px;
}}
QScrollBar::handle:vertical{{
  background:rgba(255,255,255,70);min-height:28px;border-radius:4px;
}}
QScrollBar::handle:vertical:hover{{background:rgba(255,255,255,110);}}
QScrollBar::handle:vertical:disabled{{background:rgba(255,255,255,28);}}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;width:0;}}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent;}}
"""

# 设置 / 历史 / 选窗等完整面板
FLOAT_PANEL_STYLE = f"""
#panel{{background:{PANEL_BG};border-radius:{PANEL_RADIUS}px;}}
QLabel{{color:{TEXT};background:transparent;}}
QLineEdit,QTextEdit,QPlainTextEdit,QSpinBox,QComboBox,QListWidget,QTableWidget{{
  background:{FIELD_BG};color:{TEXT};
  border:1px solid {FIELD_BORDER};border-radius:4px;
  selection-background-color:rgba(0,150,255,120);selection-color:#fff;
  padding:2px 4px;
}}
QPlainTextEdit{{
  font-family:Consolas,'Cascadia Mono','Microsoft YaHei UI';font-size:12px;
}}
QHeaderView::section{{
  background:{BTN_BG};color:{TEXT};border:none;padding:4px 6px;
}}
QTableWidget{{gridline-color:rgba(255,255,255,40);}}
QTableWidget::item:selected{{background:rgba(0,150,255,120);color:#fff;}}
QListWidget::item:selected{{background:rgba(0,150,255,120);color:#fff;}}
QComboBox{{padding:2px 6px;}}
QComboBox::drop-down{{border:none;width:18px;}}
QComboBox QAbstractItemView{{
  background:{DROPDOWN_BG};color:{DROPDOWN_FG};
  selection-background-color:{BTN_CHECKED};selection-color:#fff;
  border:1px solid {FIELD_BORDER};
}}
QPushButton{{
  background:{BTN_BG};color:{TEXT};border:none;
  border-radius:4px;padding:4px 10px;
}}
QPushButton:hover{{background:{BTN_HOVER};}}
QPushButton:checked{{background:{BTN_CHECKED};}}
QPushButton:disabled{{color:rgba(255,255,255,90);background:rgba(255,255,255,18);}}
QCheckBox{{color:{TEXT};spacing:6px;background:transparent;}}
QCheckBox::indicator{{
  width:14px;height:14px;border:1px solid {FIELD_BORDER};
  border-radius:3px;background:{FIELD_BG};
}}
QCheckBox::indicator:checked{{background:{BTN_CHECKED};border-color:rgba(0,150,255,180);}}
QSpinBox::up-button,QSpinBox::down-button{{
  background:{BTN_BG};border:none;width:16px;
}}
QScrollBar:vertical{{
  background:rgba(0,0,0,80);width:12px;margin:2px;border-radius:4px;
}}
QScrollBar::handle:vertical{{
  background:rgba(255,255,255,70);min-height:28px;border-radius:4px;
}}
QScrollBar::handle:vertical:hover{{background:rgba(255,255,255,110);}}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;width:0;}}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent;}}
QScrollBar:horizontal{{
  background:rgba(0,0,0,80);height:12px;margin:2px;border-radius:4px;
}}
QScrollBar::handle:horizontal{{
  background:rgba(255,255,255,70);min-width:28px;border-radius:4px;
}}
QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{{width:0;height:0;}}
QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{{background:transparent;}}
"""

# 高级设置面板（侧栏 + 卡片）
SETTINGS_STYLE = f"""
#panel{{
  background:rgba(18,20,26,242);
  border:1px solid rgba(255,255,255,28);
  border-radius:12px;
}}
#titleBar{{background:transparent;}}
#titleLabel{{
  color:#fff;font-size:15px;font-weight:600;letter-spacing:0.5px;
  background:transparent;
}}
#subtitle{{
  color:rgba(255,255,255,120);font-size:11px;background:transparent;
}}
#sideNav{{
  background:rgba(255,255,255,8);
  border:1px solid rgba(255,255,255,12);
  border-radius:10px;
  padding:6px;
}}
#sideNav QPushButton{{
  text-align:left;padding:10px 14px;border-radius:8px;
  background:transparent;color:rgba(255,255,255,180);
  border:none;font-size:13px;
}}
#sideNav QPushButton:hover{{
  background:rgba(255,255,255,14);color:#fff;
}}
#sideNav QPushButton:checked{{
  background:rgba(0,150,255,55);
  color:#fff;
  border:1px solid rgba(0,150,255,90);
}}
#card{{
  background:rgba(255,255,255,8);
  border:1px solid rgba(255,255,255,14);
  border-radius:10px;
}}
#cardTitle{{
  color:rgba(255,255,255,220);font-size:13px;font-weight:600;
  background:transparent;padding:2px 0 6px 0;
}}
#cardHint{{
  color:rgba(255,255,255,110);font-size:11px;background:transparent;
}}
#footer{{
  background:transparent;border-top:1px solid rgba(255,255,255,16);
}}
QLabel{{color:{TEXT};background:transparent;}}
QLineEdit,QTextEdit,QPlainTextEdit,QSpinBox,QComboBox{{
  background:rgba(0,0,0,90);color:#fff;
  border:1px solid rgba(255,255,255,40);border-radius:6px;
  selection-background-color:rgba(0,150,255,120);selection-color:#fff;
  padding:5px 8px;min-height:22px;
}}
QPlainTextEdit{{
  font-family:Consolas,'Cascadia Mono','Microsoft YaHei UI';font-size:12px;
  border-radius:8px;
}}
QComboBox{{padding:4px 8px;}}
QComboBox::drop-down{{border:none;width:20px;}}
QComboBox QAbstractItemView{{
  background:#1c1f28;color:#eee;
  selection-background-color:{BTN_CHECKED};selection-color:#fff;
  border:1px solid rgba(255,255,255,40);
}}
QPushButton{{
  background:rgba(255,255,255,32);color:#fff;border:none;
  border-radius:6px;padding:6px 14px;font-size:12px;
}}
QPushButton:hover{{background:rgba(255,255,255,48);}}
QPushButton:checked{{background:{BTN_CHECKED};}}
QPushButton#primaryBtn{{
  background:rgba(0,140,255,200);color:#fff;font-weight:600;
  padding:8px 22px;border-radius:8px;
}}
QPushButton#primaryBtn:hover{{background:rgba(30,160,255,230);}}
QPushButton#ghostBtn{{
  background:transparent;border:1px solid rgba(255,255,255,40);
  color:rgba(255,255,255,200);
}}
QPushButton#ghostBtn:hover{{background:rgba(255,255,255,16);}}
QPushButton#closeBtn{{
  background:transparent;color:rgba(255,255,255,160);
  border-radius:6px;font-size:16px;padding:2px 8px;
}}
QPushButton#closeBtn:hover{{background:rgba(255,80,80,160);color:#fff;}}
QCheckBox{{color:{TEXT};spacing:8px;background:transparent;}}
QCheckBox::indicator{{
  width:16px;height:16px;border:1px solid rgba(255,255,255,50);
  border-radius:4px;background:rgba(0,0,0,80);
}}
QCheckBox::indicator:checked{{
  background:rgba(0,150,255,180);border-color:rgba(0,150,255,200);
}}
QSpinBox::up-button,QSpinBox::down-button{{
  background:rgba(255,255,255,28);border:none;width:16px;
}}
QScrollBar:vertical{{
  background:rgba(0,0,0,60);width:10px;margin:2px;border-radius:5px;
}}
QScrollBar::handle:vertical{{
  background:rgba(255,255,255,70);min-height:28px;border-radius:5px;
}}
QScrollBar::handle:vertical:hover{{background:rgba(255,255,255,110);}}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;width:0;}}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent;}}
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
    for i, alpha in enumerate((220, 180, 130)):
        off = 4 + i * 4
        painter.setPen(QPen(QColor(255, 255, 255, alpha), 1.6))
        painter.drawLine(width - 3, height - off, width - off, height - 3)
