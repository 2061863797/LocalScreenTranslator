# -*- coding: utf-8 -*-
"""翻译结果显示组件：

- _DraggableMixin：可拖动窗口的通用混入（翻译窗口使用）
- SubtitleBar：持续翻译的悬浮字幕条（文字层鼠标穿透 + 独立控制小条）
- AnnotationOverlay：持续翻译的备注模式（译文贴在原文旁，鼠标穿透）
- RegionWatchFrame：区域翻译识别框（可拖动 / 固定）
"""

import ctypes
from ctypes import wintypes
import numpy as np

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QPainter,
    QPen,
    QRegion,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..capture import set_window_capture_excluded
from ..i18n import t as _t
from .theme import (
    ACCENT_QCOLOR,
    BORDER_QCOLOR,
    CTRL_STYLE,
    FIELD_QCOLOR,
    MUTED_QCOLOR,
    PANEL_QCOLOR,
    SCROLLBAR_STYLE,
    TEXT_QCOLOR,
    paint_size_grip,
)
from .topmost import restack_above_owner, set_overlay_layer

_FLAGS_TOP = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
)


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
    widget.setGeometry(x, y, w, h)
    return True


def _move_if_changed(widget: QWidget, x: int, y: int) -> bool:
    if widget.x() == x and widget.y() == y:
        return False
    widget.move(x, y)
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


class SubtitleBar(_CaptureAllowedMixin, QWidget):
    """持续翻译的悬浮字幕条：

    - 文字层：鼠标穿透，固定尺寸，长文在框内滚动（不随译文自动改大小）
    - 右侧滚动条：独立可点窗口
    - 右下角缩放把手：独立窗口，grabMouse 保证拖出后仍跟手
    - 控制小条：跟随 / 自由 / 固定 / 备注 / 关闭
    """

    mode_changed = Signal(str)  # follow / free / pinned
    stop_requested = Signal()   # 用户点关闭，停止持续翻译
    switch_to_annotate = Signal()  # 运行中切换到备注模式
    pause_changed = Signal(bool)

    _PAD = 10
    _SCROLL_W = 14
    _GRIP = 18
    _MIN_W = 100
    _MIN_H = 32
    _DEFAULT_H = 100
    _DEFAULT_FONT_SIZE = 16

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.mode = "follow"
        self._interactive = True
        self._user_size: tuple[int, int] | None = None
        self._text = ""
        self._scroll = 0
        self._content_h = 0
        self._layout_calculated_before_show = False
        self._font = QFont()
        self._font.setPixelSize(self._DEFAULT_FONT_SIZE)
        # 单一顶层 HWND：通过 WM_NCHITTEST 实现动态穿透与控件可交互 (PR 10)
        self.setWindowFlags(_FLAGS_TOP)

        self._ctrl = _SubtitleCtrl(self)
        self._vscroll = _SubtitleVScroll(self)
        self._grip = _SubtitleResizeGrip(self)
        self._ctrl.hide()
        self._vscroll.hide()
        self._grip.hide()
        self.resize(self._MIN_W, self._DEFAULT_H)
        self._layer_owner: int | None = None
        self._capture_visible = True

    def is_single_hwnd(self) -> bool:
        """Returns True if self is top-level window and all controls are child widgets."""
        return (
            self.isWindow()
            and not self._ctrl.isWindow()
            and self._ctrl.parent() is self
            and not self._vscroll.isWindow()
            and self._vscroll.parent() is self
            and not self._grip.isWindow()
            and self._grip.parent() is self
        )

    def nativeEvent(self, event_type, message):
        """处理 Windows WM_NCHITTEST (0x0084)：子控件返回 HTCLIENT(1)，背景返回 HTTRANSPARENT(-1)。"""
        try:
            msg_val = None
            lparam = 0
            if hasattr(message, "message") and hasattr(message, "lParam"):
                msg_val = message.message
                lparam = message.lParam
            elif event_type in (b"windows_generic_MSG", "windows_generic_MSG"):
                msg = MSG.from_address(int(message))
                msg_val = msg.message
                lparam = msg.lParam

            if msg_val == 0x0084:  # WM_NCHITTEST
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                local_pt = self.mapFromGlobal(QPoint(x, y))
                child = self.childAt(local_pt)
                if child is not None:
                    return True, 1  # HTCLIENT: 可交互子部件
                return True, -1  # HTTRANSPARENT: 穿透到底层应用
        except Exception:
            pass
        return super().nativeEvent(event_type, message)

    def layer_widgets(self) -> list[QWidget]:
        """单一原生 HWND：仅返回主窗口自身，消除多 HWND 竞争与 Win32 1400 报错。"""
        return [self]

    def set_capture_visible(self, visible: bool) -> None:
        """字幕浮层是否参与屏幕捕获（区域翻译覆盖时防干扰 OCR）。"""
        self._capture_visible = bool(visible)
        self._apply_capture_affinity()

    def _apply_capture_affinity(self) -> None:
        for w in self.layer_widgets():
            if self._capture_visible:
                _allow_capture(w)
            else:
                _exclude_from_capture(w)

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        self._apply_capture_affinity()

    def set_layer_owner(self, owner_hwnd: int | None) -> None:
        """窗口翻译：跟目标窗同层；None=恢复全局置顶（区域翻译）。"""
        self._layer_owner = int(owner_hwnd) if owner_hwnd else None
        for w in self.layer_widgets():
            set_overlay_layer(w, self._layer_owner)
        if self.isVisible():
            self._apply_capture_affinity()

    def restack_layer(self) -> None:
        """窗口模式贴回 owner；区域模式重新置于 TOPMOST 层最前。"""
        for w in self.layer_widgets():
            if w.isVisible():
                if self._layer_owner:
                    restack_above_owner(w, self._layer_owner)
                else:
                    set_overlay_layer(w, None)

    def apply_ui_language(self):
        self._ctrl.apply_ui_language()
        try:
            self._grip.apply_ui_language()
        except Exception:
            pass
        self._place_chrome()

    def set_paused(self, paused: bool):
        self._ctrl.set_paused(paused)
        self._place_chrome()

    def set_font_size(self, size) -> None:
        """设置字幕字号；0 或无效值恢复原有默认字号。"""
        try:
            requested = int(size)
        except (TypeError, ValueError):
            requested = 0
        resolved = (
            requested if 12 <= requested <= 20 else self._DEFAULT_FONT_SIZE
        )
        if self._font.pixelSize() == resolved:
            return
        self._font.setPixelSize(resolved)
        self._reflow_text()
        self.update()

    def set_interactive(self, on: bool):
        """字幕模式：显示右下角缩放；译文层始终穿透。"""
        on = bool(on)
        if on == self._interactive:
            return
        self._interactive = on
        if self.isVisible():
            self._place_chrome()
            self._show_chrome()
            self._apply_capture_affinity()

    def set_mode(self, mode: str, emit: bool = False):
        self.mode = mode
        self._ctrl.sync_checked(mode)
        if emit:
            self.mode_changed.emit(mode)

    def attach_below(
        self,
        win_rect: tuple[int, int, int, int],
        *,
        outside: bool = False,
        match_target_size: bool = False,
    ):
        """跟随模式下吸附到目标下缘；其他模式不动。框体大小不随译文变。

        match_target_size: 区域翻译时开启，宽高完全同步为区域选区尺寸。
        """
        if self.mode != "follow":
            return
        x, y, w, h = win_rect
        if match_target_size:
            bar_w = max(w, self._MIN_W)
            # 适度字幕高度：小选区（<=120）完全匹配，大选区限制为默认字幕高度，防止撑爆屏幕导致无法跟随
            bar_h = max(h, self._MIN_H) if h <= 120 else max(self._DEFAULT_H, self._MIN_H)
            self._user_size = None
        elif self._user_size:
            bar_w, bar_h = self._user_size
        else:
            bar_w, bar_h = max(w, 280), self._DEFAULT_H
        if outside:
            nx, ny, nw, nh = x, y + h + 4, bar_w, bar_h
        else:
            nx, ny, nw, nh = x, y + h - bar_h - 10, max(w, 200), bar_h

        # 屏幕边界安全夹紧与防出界翻转（仅在屏幕内常规窗口时生效）
        try:
            center_pt = QPoint(x + w // 2, y + h // 2)
            screen = QGuiApplication.screenAt(center_pt)
            if screen:
                avail = screen.availableGeometry()
                if avail.contains(center_pt):
                    # 若 outside=True 且底部放不下，自动翻转吸附到目标选区上方（避开区域控制条）
                    if outside and (ny + nh > avail.bottom()):
                        alt_y = y - nh - 34
                        if alt_y >= avail.top():
                            ny = alt_y
                        else:
                            ny = min(ny, avail.bottom() - nh)
                    # 限制在屏幕可见工作区内
                    nx = max(avail.left(), min(nx, avail.right() - nw))
        except Exception:
            pass

        changed = _set_geo_if_changed(self, nx, ny, nw, nh)
        if changed:
            self._reflow_text()
        self._place_chrome()
        if self.isVisible():
            self._show_chrome()

    def move_to(self, x: int, y: int):
        """自由模式下由控制条拖动调用。"""
        _move_if_changed(self, x, y)
        self._place_chrome()
        if self.isVisible():
            self._show_chrome()

    def resize_to(self, w: int, h: int):
        """右下角把手缩放：改框大小并记住，不随译文自动改。"""
        w = max(self._MIN_W, int(w))
        h = max(self._MIN_H, int(h))
        if self.width() == w and self.height() == h and self._user_size == (w, h):
            return
        self.setFixedSize(w, h)
        self._user_size = (w, h)
        self._reflow_text()
        self._place_chrome()
        if self.isVisible():
            self._show_chrome()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 不用 setFixedSize 时仍同步（attach_below 用 setGeometry）
        if self.isVisible():
            self._reflow_text()
            self._place_chrome()
            self._show_chrome()

    def setGeometry(self, *args):
        # 兼容 QRect / x,y,w,h；跟随后不锁死 fixed，方便拖目标窗改默认宽
        super().setGeometry(*args)
        if self._user_size is not None:
            # 用户缩放过：强制保持记忆尺寸（位置可随 attach 变）
            g = self.geometry()
            uw, uh = self._user_size
            if g.width() != uw or g.height() != uh:
                super().setGeometry(g.x(), g.y(), uw, uh)

    def _effective_pads(self) -> tuple[int, int]:
        pad_x = min(self._PAD, max(4, self.width() // 10))
        pad_y = min(self._PAD, max(2, self.height() // 8))
        return pad_x, pad_y

    def _pad_top(self) -> int:
        _, py = self._effective_pads()
        if self.height() > 50 and hasattr(self, "_ctrl"):
            ctrl_h = self._ctrl.height() if self._ctrl.height() > 0 else self._ctrl.sizeHint().height()
            return max(py, ctrl_h + 4)
        return py

    def _text_rect_size(self) -> QSize:
        """正文可用区域（为滚动条留出右边距，为顶部控制条留出空间）。"""
        px, py = self._effective_pads()
        pt = self._pad_top()
        return QSize(
            max(20, self.width() - px * 2 - self._SCROLL_W),
            max(10, self.height() - pt - py),
        )

    def _reflow_text(self):
        """按当前框宽计算内容高度，更新滚动范围；不改框体尺寸。"""
        tr = self._text_rect_size()
        if not self._text:
            self._content_h = 0
            self._scroll = 0
            self._vscroll.set_range(0, 0, tr.height())
            self.update()
            return
        # 用 font metrics 估算换行后高度（与 paint 同一套 flags，避免估矮导致滑条误关）
        fm = QFontMetrics(self._font)
        flags = int(
            Qt.TextFlag.TextWordWrap
            | Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignTop
        )
        br = fm.boundingRect(0, 0, max(1, tr.width()), 10_000_000, flags, self._text)
        # 略留余量：部分字体/抗锯齿下 boundingRect 会偏矮
        self._content_h = max(br.height() + 4, fm.height())
        max_scroll = max(0, self._content_h - tr.height())
        self._scroll = min(self._scroll, max_scroll)
        self._vscroll.set_range(0, max_scroll, tr.height())
        self._vscroll.set_value(self._scroll)
        self.update()

    def set_scroll(self, value: int):
        tr = self._text_rect_size()
        max_scroll = max(0, self._content_h - tr.height())
        self._scroll = max(0, min(int(value), max_scroll))
        # 外部滑块可能已同步，避免回写死循环；仅刷新画面
        self.update()

    def _place_chrome(self):
        """控制条 / 滚动条 / 缩放把手贴在文字层周围（client 相对坐标）。"""
        self._ctrl.adjustSize()
        ctrl_w = self._ctrl.width()
        ctrl_h = self._ctrl.height()
        _set_geo_if_changed(
            self._ctrl,
            max(0, self.width() - ctrl_w - 4),
            4,
            ctrl_w,
            ctrl_h,
        )
        self._ctrl.raise_()
        sw = max(self._SCROLL_W, 16)
        vscroll_y = ctrl_h + 4
        vscroll_h = max(10, self.height() - vscroll_y - self._GRIP - 4)
        _set_geo_if_changed(
            self._vscroll,
            max(0, self.width() - sw),
            vscroll_y,
            sw,
            vscroll_h,
        )
        self._vscroll.raise_()
        _move_if_changed(
            self._grip,
            max(0, self.width() - self._GRIP),
            max(0, self.height() - self._GRIP),
        )
        self._grip.raise_()

    def _show_chrome(self):
        """统一显示附属窗：仅在需要时 show，避免每轮 raise 闪烁。"""
        if not self.isVisible():
            return
        _show_once(self._ctrl)
        if self._text:
            _show_once(self._vscroll)
            need = self._content_h > self._text_rect_size().height()
            self._vscroll.set_enabled(need)
        else:
            if self._vscroll.isVisible():
                self._vscroll.hide()
        if self._interactive:
            _show_once(self._grip)
        else:
            if self._grip.isVisible():
                self._grip.hide()
        self._apply_capture_affinity()

    def prepare_layout(self, text: str = "") -> None:
        """在向 DWM 呈现前执行完整的预排版与布局计算，杜绝脏矩形与二次重排闪烁。"""
        if text:
            self._text = text.strip()
        if self._user_size:
            self.resize(*self._user_size)
        elif self.width() < self._MIN_W or self.height() < self._MIN_H:
            self.resize(max(self.width(), self._MIN_W), max(self.height(), self._MIN_H))
        self._reflow_text()
        self._place_chrome()
        self._layout_calculated_before_show = True

    def set_text(self, text: str | None):
        """更新译文：框大小不变，过长用滚动条。已显示时只重绘，不反复 raise。

        滚动位置跨轮次译文刷新保持（持续翻译改文时不把滑块打回顶部）；
        仅在 hide 结束会话或正文被清空时归零。_reflow_text 会夹紧到新范围。
        未显示前严格保持隐藏；首次显示前完成完整排版与布局计算，避免脏矩形闪烁。
        """
        valid_text = (text or "").strip()
        if not valid_text:
            self._text = ""
            if self.isVisible():
                self.hide()
            return

        self._text = valid_text
        first = not self.isVisible()
        if first:
            self.prepare_layout(valid_text)
            if self._layer_owner:
                self.set_layer_owner(self._layer_owner)
            self.show()
            self._apply_capture_affinity()
        else:
            self._reflow_text()
            self._place_chrome()

        self._show_chrome()
        self.restack_layer()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(PANEL_QCOLOR)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)

        if not self._text:
            return
        px, py = self._effective_pads()
        pt = self._pad_top()
        text_rect = QRect(
            px,
            pt,
            max(20, self.width() - px * 2 - self._SCROLL_W),
            max(10, self.height() - pt - py),
        )
        painter.setFont(self._font)
        painter.setPen(TEXT_QCOLOR)
        painter.setClipRect(text_rect)
        # 内容整体上移实现滚动
        draw_rect = text_rect.translated(0, -self._scroll)
        draw_rect.setHeight(max(self._content_h + py, text_rect.height()))
        painter.drawText(
            draw_rect,
            int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
            self._text,
        )

    def hide(self):
        self._ctrl.hide()
        self._vscroll.hide()
        self._grip.hide()
        # 结束本轮持续翻译后归零，下次会话从顶部看起
        self._scroll = 0
        super().hide()


class _SubtitleVScroll(_CaptureAllowedMixin, QWidget):
    """字幕条右侧纵向滚动条（子部件，可点）。

    显隐只由 SubtitleBar._show_chrome 控制；set_range 绝不 hide，
    避免跟随/缩放路径漏 show 导致滑条突然消失。
    """

    def __init__(self, bar: SubtitleBar):
        super().__init__(bar)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._bar = bar
        self._bar_widget = QScrollBar(Qt.Orientation.Vertical, self)
        # 与翻译结果窗同系：深底 + 浅色滑块（非高饱和蓝）
        self._bar_widget.setStyleSheet(SCROLLBAR_STYLE)
        self._bar_widget.valueChanged.connect(self._bar.set_scroll)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 2, 1, 2)
        lay.addWidget(self._bar_widget)

    def set_range(self, mn: int, mx: int, page: int):
        self._bar_widget.blockSignals(True)
        self._bar_widget.setRange(mn, max(mn, mx))
        self._bar_widget.setPageStep(max(10, page))
        self._bar_widget.setSingleStep(16)
        self._bar_widget.blockSignals(False)
        # 不在这里 hide —— 显隐交给 _show_chrome

    def set_value(self, v: int):
        self._bar_widget.blockSignals(True)
        self._bar_widget.setValue(int(v))
        self._bar_widget.blockSignals(False)

    def set_enabled(self, on: bool):
        self._bar_widget.setEnabled(bool(on))


class _SubtitleResizeGrip(_CaptureAllowedMixin, QWidget):
    """右下角缩放把手：子部件 + grabMouse，拖出按钮外仍跟手。"""

    def __init__(self, bar: SubtitleBar):
        super().__init__(bar)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(bar._GRIP, bar._GRIP)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._bar = bar
        self._origin: tuple | None = None
        self.apply_ui_language()

    def apply_ui_language(self):
        self.setToolTip(_t("sub_resize_tip"))

    def paintEvent(self, event):
        # 与翻译窗右下角 QSizeGrip 同形态：三道白斜线，深色底上可见
        p = QPainter(self)
        paint_size_grip(p, self.width(), self.height())

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._origin = (
            event.globalPosition().toPoint(),
            self._bar.width(),
            self._bar.height(),
        )
        self.grabMouse()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._origin is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        g0, w0, h0 = self._origin
        d = event.globalPosition().toPoint() - g0
        self._bar.resize_to(w0 + d.x(), h0 + d.y())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._origin is not None:
            self._origin = None
            self.releaseMouse()
            event.accept()


class _SubtitleCtrl(_CaptureAllowedMixin, QWidget):
    """字幕条控制小条：拖动把手 + 模式按钮 + 关闭（子部件，可点）。"""

    def __init__(self, bar: SubtitleBar):
        super().__init__(bar)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._bar = bar
        self._drag_offset = None

        self._btns: dict[str, QPushButton] = {}
        container = QWidget(self)
        container.setObjectName("ctrl")
        lay = QHBoxLayout(container)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(4)
        self._handle = QLabel("⠿")
        self._handle.setStyleSheet(
            "color:#fff;font-size:14px;padding:0 2px;background:transparent;"
        )
        lay.addWidget(self._handle)
        for key in ("follow", "free", "pinned"):
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.clicked.connect(
                lambda _=False, k=key: self._bar.set_mode(k, emit=True)
            )
            self._btns[key] = btn
            lay.addWidget(btn)
        self._btn_ann = QPushButton()
        self._btn_ann.setFixedHeight(20)
        self._btn_ann.clicked.connect(self._bar.switch_to_annotate.emit)
        lay.addWidget(self._btn_ann)
        self._btn_pause = QPushButton()
        self._btn_pause.setCheckable(True)
        self._btn_pause.setFixedHeight(20)
        self._btn_pause.toggled.connect(self._bar.pause_changed.emit)
        lay.addWidget(self._btn_pause)
        self._btn_close = QPushButton()
        self._btn_close.setFixedHeight(20)
        self._btn_close.clicked.connect(self._bar.stop_requested.emit)
        lay.addWidget(self._btn_close)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self.setStyleSheet(CTRL_STYLE)
        self.sync_checked(bar.mode)
        self.apply_ui_language()

    def apply_ui_language(self):
        self._handle.setToolTip(_t("sub_drag_tip"))
        self._btns["follow"].setText(_t("sub_follow"))
        self._btns["free"].setText(_t("sub_free"))
        self._btns["pinned"].setText(_t("sub_pinned"))
        self._btn_ann.setText(_t("sub_annotate"))
        self._btn_ann.setToolTip(_t("sub_annotate_tip"))
        self._btn_pause.setText(
            _t("sub_resume") if self._btn_pause.isChecked() else _t("sub_pause")
        )
        self._btn_close.setText(_t("sub_close"))
        self._btn_close.setToolTip(_t("sub_close_tip"))
        self.adjustSize()

    def set_paused(self, paused: bool):
        self._btn_pause.blockSignals(True)
        self._btn_pause.setChecked(bool(paused))
        self._btn_pause.blockSignals(False)
        self.apply_ui_language()

    def sync_checked(self, mode: str):
        for k, btn in self._btns.items():
            btn.setChecked(k == mode)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self._bar.pos()

    def mouseMoveEvent(self, event):
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            if self._bar.mode == "follow":
                self._bar.set_mode("free", emit=True)
            if self._bar.mode == "free":
                p = event.globalPosition().toPoint() - self._drag_offset
                self._bar.move_to(p.x(), p.y())

    def mouseReleaseEvent(self, event):
        self._drag_offset = None


class _RegionCtrl(_CaptureAllowedMixin, QWidget):
    """区域识别框控制条：与窗口翻译字幕/备注条同一套 CTRL_STYLE。"""

    def __init__(self, frame: "RegionWatchFrame"):
        super().__init__()
        self.setWindowFlags(_FLAGS_TOP)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._frame = frame
        self._drag_offset = None
        container = QWidget()
        container.setObjectName("regCtrl")
        lay = QHBoxLayout(container)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(6)
        self._handle = QLabel("⠿")
        self._handle.setStyleSheet(
            "color:#fff;font-size:14px;padding:0 2px;background:transparent;"
        )
        lay.addWidget(self._handle)
        self._tip = QLabel()
        lay.addWidget(self._tip)
        self._btn_pin = QPushButton()
        self._btn_pin.setCheckable(True)
        self._btn_pin.setFixedHeight(20)
        self._btn_pin.clicked.connect(self._on_pin)
        lay.addWidget(self._btn_pin)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self.setStyleSheet(CTRL_STYLE)
        self.apply_ui_language()
        self._layer_owner: int | None = None

    def apply_ui_language(self):
        self._handle.setToolTip(_t("sub_drag_tip"))
        self._tip.setText(_t("hk_region"))
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
        """贴在识别区上方左侧（右侧留给备注/字幕控制条，与窗口翻译一致不抢位）。"""
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
        self._ctrl = _RegionCtrl(self)

    def apply_ui_language(self):
        self._ctrl.apply_ui_language()
        self._place_ctrl()
        self.update()

    def restack_layer(self) -> None:
        """识别框和控制条始终处在全局 TOPMOST 层最前，且不抢焦点。"""
        for widget in (self, self._ctrl):
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
        if self.isVisible():
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


class AnnotateCtrl(_CaptureAllowedMixin, QWidget):
    """备注模式控制小条：字幕 / 跳过目标语 / 关闭（可点，贴在目标外侧）。"""

    stop_requested = Signal()
    skip_target_changed = Signal(bool)  # 不翻译已是目标语言的文字
    switch_to_subtitle = Signal()  # 运行中切换到字幕条模式
    pause_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(_FLAGS_TOP)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        container = QWidget()
        container.setObjectName("annCtrl")
        lay = QHBoxLayout(container)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(6)
        self._tip = QLabel()
        lay.addWidget(self._tip)
        self._btn_sub = QPushButton()
        self._btn_sub.setFixedHeight(20)
        self._btn_sub.clicked.connect(self.switch_to_subtitle.emit)
        lay.addWidget(self._btn_sub)
        self._btn_skip = QPushButton()
        self._btn_skip.setCheckable(True)
        self._btn_skip.setFixedHeight(20)
        self._btn_skip.clicked.connect(self._on_skip_clicked)
        lay.addWidget(self._btn_skip)
        self._btn_pause = QPushButton()
        self._btn_pause.setCheckable(True)
        self._btn_pause.setFixedHeight(20)
        self._btn_pause.toggled.connect(self.pause_changed.emit)
        lay.addWidget(self._btn_pause)
        self._btn_close = QPushButton()
        self._btn_close.setFixedHeight(20)
        self._btn_close.clicked.connect(self.stop_requested.emit)
        lay.addWidget(self._btn_close)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self.setStyleSheet(CTRL_STYLE)
        self.apply_ui_language()
        self._layer_owner: int | None = None

    def apply_ui_language(self):
        self._tip.setText(_t("ann_label"))
        self._btn_sub.setText(_t("ann_subtitle"))
        self._btn_sub.setToolTip(_t("ann_subtitle_tip"))
        self._btn_skip.setText(_t("ann_skip"))
        self._btn_skip.setToolTip(_t("ann_skip_tip"))
        self._btn_pause.setText(
            _t("sub_resume") if self._btn_pause.isChecked() else _t("sub_pause")
        )
        self._btn_close.setText(_t("sub_close"))
        self._btn_close.setToolTip(_t("ann_close_tip"))
        self.adjustSize()

    def set_paused(self, paused: bool):
        self._btn_pause.blockSignals(True)
        self._btn_pause.setChecked(bool(paused))
        self._btn_pause.blockSignals(False)
        self.apply_ui_language()

    def set_layer_owner(self, owner_hwnd: int | None) -> None:
        self._layer_owner = int(owner_hwnd) if owner_hwnd else None
        set_overlay_layer(self, self._layer_owner)
        if self.isVisible():
            _allow_capture(self)

    def restack_layer(self) -> None:
        if not self.isVisible():
            return
        if self._layer_owner:
            restack_above_owner(self, self._layer_owner)
        else:
            set_overlay_layer(self, None)

    def _on_skip_clicked(self):
        self.skip_target_changed.emit(self._btn_skip.isChecked())

    def set_skip_target(self, on: bool):
        """与配置/设置页同步，不触发 signal。"""
        on = bool(on)
        if self._btn_skip.isChecked() != on:
            self._btn_skip.blockSignals(True)
            self._btn_skip.setChecked(on)
            self._btn_skip.blockSignals(False)

    def place_above(self, win_rect: tuple[int, int, int, int]):
        """贴在目标右上角外侧，不挡内容。"""
        x, y, w, h = win_rect
        self.adjustSize()
        nx = x + max(0, w - self.width())
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
        # 是否允许系统截图/录屏拍到译文。关闭（默认配置）时浮层从屏幕
        # 捕获排除，区域备注的 OCR 抓屏天然看不到译文，无需遮罩还原。
        self._capture_visible = True

    def set_capture_visible(self, visible: bool) -> None:
        """备注译文是否参与屏幕捕获；立即应用到当前原生窗口。"""
        self._capture_visible = bool(visible)
        self._apply_capture_affinity()

    def _apply_capture_affinity(self) -> None:
        if self._capture_visible:
            _allow_capture(self)
        else:
            _exclude_from_capture(self)

    def showEvent(self, event: QShowEvent):
        # 覆盖 mixin 的「显示即允许捕获」：按开关决定显示亲和性
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

    def capture_mask(self) -> np.ndarray | None:
        """按实际字体和布局渲染译文透明度遮罩，供区域 OCR 恢复底图。"""
        if not self.isVisible() or not self._items:
            return None
        width, height = self.width(), self.height()
        if width <= 0 or height <= 0:
            return None
        image = QImage(
            width,
            height,
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        font, layout = self._layout_items()
        painter.setFont(font)
        painter.setPen(Qt.GlobalColor.white)
        for (x1, y1, x2, y2), text in layout:
            painter.drawText(
                x1,
                y1,
                x2 - x1,
                y2 - y1,
                Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine,
                text,
            )
        painter.end()
        stride = image.bytesPerLine()
        pixels = np.frombuffer(
            image.constBits(),
            dtype=np.uint8,
            count=image.sizeInBytes(),
        ).reshape(height, stride)
        bgra = pixels[:, : width * 4].reshape(height, width, 4)
        return bgra[:, :, 3].copy()

    def paintEvent(self, event):
        if not self._items:
            return
        painter = QPainter(self)
        font, layout = self._layout_items()
        painter.setFont(font)
        painter.setPen(self._text_color)
        for (x1, y1, x2, y2), text in layout:
            painter.drawText(
                x1, y1, x2 - x1, y2 - y1,
                Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine,
                text,
            )
