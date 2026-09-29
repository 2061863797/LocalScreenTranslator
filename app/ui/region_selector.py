# -*- coding: utf-8 -*-
"""屏幕框选组件：先静默截全屏，再以静态图为底框选（避免半透明首帧闪屏）。"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QShowEvent,
)
from PySide6.QtWidgets import QApplication, QWidget

from .overlays import _exclude_from_capture
from .topmost import raise_to_front


def _virtual_geometry() -> QRect:
    geo = QRect()
    for screen in QGuiApplication.screens():
        geo = geo.united(screen.geometry())
    return geo


def _bgr_to_qimage(img: np.ndarray) -> QImage:
    """BGR uint8 → QImage（深拷贝，避免底层缓冲被回收）。"""
    h, w = img.shape[:2]
    if img.ndim == 2:
        rgb = np.ascontiguousarray(img)
        qimg = QImage(rgb.data, w, h, w, QImage.Format.Format_Grayscale8)
        return qimg.copy()
    # BGR → RGB
    rgb = np.ascontiguousarray(img[:, :, ::-1])
    bytes_per_line = rgb.strides[0]
    qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
    return qimg.copy()


class RegionSelector(QWidget):
    """选区完成后发出 region_selected(x, y, w, h)（虚拟屏幕坐标）。

    防闪策略：
    - 不显示「真·半透明挖洞」遮罩（DWM 首帧常先不透明再合成，会闪）
    - 先 mss 静默截全屏，把截图当底图 + 压暗，在图上框选（不透明整窗绘制）
    - 选区图从底图裁切，无需再 hide 后截屏
    - 启动时 prepare：预热 mss + 透明度为 0 预建 HWND（人眼看不见）
    - 关闭时先 opacity=0 再 hide，避免空白一帧
    """

    region_selected = Signal(int, int, int, int)
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._origin = None
        self._current = None
        self._prepared = False
        self._dormant = False  # prepare 预热时不绘制
        # 全屏截图（虚拟桌面）
        self._bg_bgr: np.ndarray | None = None
        self._bg_pix: QPixmap | None = None
        self._bg_origin = (0, 0)  # 虚拟桌面左上角
        self._last_crop: np.ndarray | None = None
        self._use_solid_bg = True  # 有底图时不透明绘制，杜绝 DWM 半透明闪

    def _set_paint_mode(self, solid: bool):
        """solid=True：整窗不透明绘制（推荐）；False：半透明挖洞回退。"""
        self._use_solid_bg = solid
        # 切换属性时尽量在 hide 状态下做，避免闪
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, not solid)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, solid)

    def prepare(self):
        """启动时预热：mss 首次初始化 + 全屏 HWND，避免首次 Alt+Q 才创建。"""
        if self._prepared:
            return
        # 1) 预热 mss（第一次 import/初始化可能顿一下）
        try:
            import mss

            with mss.MSS() as sct:
                mon = sct.monitors[0]  # 全部监视器合并
                sct.grab(mon)
        except Exception:
            pass
        # 2) 透明度为 0 建原生窗口，人眼看不见，避免启动时闪一下
        self._dormant = True
        self._set_paint_mode(True)
        geo = _virtual_geometry()
        if geo.width() <= 0 or geo.height() <= 0:
            self.setGeometry(-32000, -32000, 16, 16)
        else:
            self.setGeometry(geo)
        self.setWindowOpacity(0.0)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.show()
        _exclude_from_capture(self)
        QApplication.processEvents()
        self.hide()
        self.setWindowOpacity(1.0)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self._dormant = False
        self._prepared = True

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        _exclude_from_capture(self)

    def take_crop(self) -> np.ndarray | None:
        """取出最近一次框选的图像（BGR）；没有则返回 None。"""
        img = self._last_crop
        self._last_crop = None
        return img

    def _capture_desktop(self) -> bool:
        """静默截取虚拟桌面到 _bg_*。"""
        from .. import capture

        geo = _virtual_geometry()
        if geo.width() <= 0 or geo.height() <= 0:
            return False
        try:
            img = capture.grab_region(geo.x(), geo.y(), geo.width(), geo.height())
        except Exception:
            return False
        if img is None or img.size == 0:
            return False
        self._bg_bgr = img
        self._bg_origin = (geo.x(), geo.y())
        self._bg_pix = QPixmap.fromImage(_bgr_to_qimage(img))
        return True

    def _clear_bg(self):
        self._bg_bgr = None
        self._bg_pix = None
        self._bg_origin = (0, 0)

    def _dismiss(self):
        """无闪关闭：先全透明再 hide，避免空白帧闪一下。"""
        if self.isVisible():
            try:
                self.setWindowOpacity(0.0)
            except Exception:
                pass
            self.hide()
            try:
                self.setWindowOpacity(1.0)
            except Exception:
                pass
        else:
            self.hide()

    def start(self):
        """开始框选：先截全屏，再显示静态底图遮罩。"""
        self._origin = None
        self._current = None
        self._last_crop = None
        self._dormant = False

        # 隐藏态完成截屏与几何，避免「空窗一帧」
        if self.isVisible():
            self._dismiss()

        # 关闭前先截屏（遮罩尚未显示，不会进画面）
        has_bg = self._capture_desktop()
        if not has_bg:
            self._clear_bg()
            self._set_paint_mode(False)
        else:
            self._set_paint_mode(True)

        geo = _virtual_geometry()
        self.setGeometry(geo)
        self.setWindowOpacity(1.0)
        # 先 show 再 TOPMOST；不反复 processEvents 减少合成闪
        raise_to_front(self, activate=True)
        _exclude_from_capture(self)
        # 强制立刻画满整窗（不透明路径下无空帧）
        self.repaint()

    def cancel(self):
        """程序侧取消框选（不发出 region_selected）。"""
        self._origin = None
        self._current = None
        self._clear_bg()
        self._last_crop = None
        was = self.isVisible()
        self._dismiss()
        if was:
            self.cancelled.emit()

    def _draw_selection_chrome(self, painter: QPainter, rect: QRect):
        """绘制现代化 HUD 框选视效：主边框、四角取景标与尺寸提示胶囊。"""
        # 1. 亮蓝色发光边框
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QPen(QColor(0, 150, 255, 220), 2))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

        # 2. 四角 HUD 取景 L 形角标
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        corner_len = min(12, max(4, min(rect.width(), rect.height()) // 4))
        p_pen = QPen(QColor(255, 255, 255, 240), 3)
        painter.setPen(p_pen)
        x1, y1, x2, y2 = rect.left(), rect.top(), rect.right(), rect.bottom()
        # 左上
        painter.drawLine(x1, y1, x1 + corner_len, y1)
        painter.drawLine(x1, y1, x1, y1 + corner_len)
        # 右上
        painter.drawLine(x2, y1, x2 - corner_len, y1)
        painter.drawLine(x2, y1, x2, y1 + corner_len)
        # 左下
        painter.drawLine(x1, y2, x1 + corner_len, y2)
        painter.drawLine(x1, y2, x1, y2 - corner_len)
        # 右下
        painter.drawLine(x2, y2, x2 - corner_len, y2)
        painter.drawLine(x2, y2, x2, y2 - corner_len)

        # 3. 尺寸胶囊浮标 (Size Badge)
        if rect.width() >= 60 and rect.height() >= 30:
            badge_text = f"{rect.width()} × {rect.height()}"
            badge_w = max(70, len(badge_text) * 7 + 16)
            badge_h = 20
            bx = rect.left() + (rect.width() - badge_w) // 2
            by = rect.top() - badge_h - 4 if rect.top() >= badge_h + 6 else rect.top() + 6
            badge_rect = QRect(bx, by, badge_w, badge_h)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(18, 22, 30, 220))
            painter.drawRoundedRect(badge_rect, 4, 4)
            painter.setPen(QColor(255, 255, 255, 230))
            font = painter.font()
            font.setPixelSize(11)
            painter.setFont(font)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)

    def paintEvent(self, event):
        if self._dormant:
            return
        painter = QPainter(self)

        if self._bg_pix is not None and not self._bg_pix.isNull():
            # 整窗铺底图 + 压暗（不透明路径，无 DWM 半透明首帧）
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.drawPixmap(0, 0, self._bg_pix)
            painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
            if self._origin and self._current:
                rect = QRect(self._origin, self._current).normalized()
                painter.drawPixmap(rect, self._bg_pix, rect)
                self._draw_selection_chrome(painter, rect)
            return

        # 回退：半透明蒙版挖洞（仅截屏失败时）
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))
        if self._origin and self._current:
            rect = QRect(self._origin, self._current).normalized()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rect, Qt.GlobalColor.transparent)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver
            )
            self._draw_selection_chrome(painter, rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event):
        if self._origin:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._origin:
            rect = QRect(self._origin, self._current).normalized()
            if rect.width() > 5 and rect.height() > 5:
                # 组件坐标 → 虚拟屏幕全局坐标
                ox, oy = self._bg_origin
                gx = ox + rect.x()
                gy = oy + rect.y()
                # 优先从静态底图裁切（无第二次截屏）
                if self._bg_bgr is not None:
                    h, w = self._bg_bgr.shape[:2]
                    x1 = max(0, min(rect.x(), w))
                    y1 = max(0, min(rect.y(), h))
                    x2 = max(0, min(rect.x() + rect.width(), w))
                    y2 = max(0, min(rect.y() + rect.height(), h))
                    if x2 > x1 and y2 > y1:
                        self._last_crop = np.ascontiguousarray(
                            self._bg_bgr[y1:y2, x1:x2].copy()
                        )
                # 先发信号再关窗：主线程可立刻用 crop；关窗无闪
                self.region_selected.emit(gx, gy, rect.width(), rect.height())
                self._clear_bg()
                self._dismiss()
            else:
                self._clear_bg()
                self._dismiss()
                self.cancelled.emit()
            self._origin = None
            self._current = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._origin = None
            self._current = None
            self._clear_bg()
            self._last_crop = None
            self._dismiss()
            self.cancelled.emit()
