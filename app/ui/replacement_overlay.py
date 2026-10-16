# -*- coding: utf-8 -*-
"""原位替换层：在原文位置铺背景色并按段落重新排版译文。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QShowEvent,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QWidget

from ..replacement import ReplacementBlock, ReplacementPayload, _is_cjk
from .overlay_base import (
    _FLAGS_TOP,
    _exclude_from_capture,
    _set_geo_if_changed,
    _show_once,
    dispatch_set_overlay_layer,
)
from .topmost import restack_above_owner

_FONT_FAMILIES = ["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Noto Sans CJK SC", "Arial"]
# 译文放不下时最多缩到原字号的比例，再放不下才在段尾省略
_MIN_SCALE = 0.7


@dataclass
class _Fill:
    rect: QRectF
    color: QColor
    patch: QImage | None
    # 倾斜/竖排行只盖这个四边形；None 时盖整个 rect
    polygon: QPolygonF | None = None


@dataclass
class _RenderedBlock:
    fills: list[_Fill]
    color: QColor
    # 横排（含倾斜）：layout 画在以 center 为原点、旋转 angle 度后的 position 处
    layout: QTextLayout | None = None
    position: QPointF = field(default_factory=QPointF)
    center: QPointF | None = None
    angle: float = 0.0
    # 竖排：逐字摆放的 (字, 基线位置)
    glyphs: list[tuple[str, QPointF]] | None = None
    font: QFont | None = None


def _bgr_image(array: np.ndarray | None) -> QImage | None:
    if not isinstance(array, np.ndarray) or array.ndim != 3 or array.size == 0:
        return None
    rgb = np.ascontiguousarray(array[:, :, 2::-1])
    return QImage(
        rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888
    ).copy()


def _make_font(size: int) -> QFont:
    font = QFont()
    font.setFamilies(_FONT_FAMILIES)
    font.setPixelSize(max(1, int(size)))
    return font


# 一行字形本身约占字号的 1.15 倍高（不含字体自带的大行距）
_GLYPH_HEIGHT = 1.15
# 译文比原文行数多时，允许把行距/列距压到字号的这个倍数，再不行才缩字号
_TIGHT_STEP = 1.25


def _layout_lines(
    text: str, font: QFont, width: float, step: float, top: float = 0.0
) -> tuple[QTextLayout, int]:
    """在给定宽度内按固定行距折行，第一行从 top 开始。"""
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    count = 0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        line.setPosition(QPointF(0.0, top + count * step))
        count += 1
    layout.endLayout()
    return layout, count


def _centered_layout(text: str, font: QFont, rect: QRectF, step: float) -> QTextLayout:
    """排好后整体在 rect 内垂直居中（行框可略超出 rect，字形仍在其中）。"""
    _, count = _layout_lines(text, font, rect.width(), step)
    total = (count - 1) * step + QFontMetricsF(font).height()
    return _layout_lines(text, font, rect.width(), step, (rect.height() - total) / 2.0)[0]


def fit_block(
    text: str, rect: QRectF, font_size: float, pitch: float
) -> tuple[QTextLayout, float]:
    """返回排好的 layout 与实际字号。

    依次尝试：原字号 + 原行距 → 原字号 + 收紧行距 → 逐级缩小字号（最多到 70%）→ 末尾省略。
    """
    text = " ".join(str(text or "").split())
    base = max(8.0, float(font_size))
    pitch = max(float(pitch), 1.0)
    minimum = max(8.0, base * _MIN_SCALE)
    size = base
    while True:
        font = _make_font(round(size))
        loose = max(pitch * size / base, size * _GLYPH_HEIGHT)
        for step in dict.fromkeys((loose, min(loose, size * _TIGHT_STEP))):
            _, count = _layout_lines(text, font, rect.width(), step)
            if (count - 1) * step + size * _GLYPH_HEIGHT <= rect.height() + 2.0:
                return _centered_layout(text, font, rect, step), size
        if size <= minimum:
            break
        size = max(minimum, size - 1.0)
    # 最小字号仍放不下：只保留能放下的行，最后一行末尾省略
    step = min(loose, size * _TIGHT_STEP)
    layout, _ = _layout_lines(text, font, rect.width(), step)
    keep = max(1, int((rect.height() + 2.0 - size * _GLYPH_HEIGHT) // step) + 1)
    keep = min(keep, layout.lineCount())
    shown = "".join(
        text[layout.lineAt(i).textStart(): layout.lineAt(i).textStart() + layout.lineAt(i).textLength()]
        for i in range(keep)
    )
    rest = text[len(shown):]
    if rest:
        last = layout.lineAt(keep - 1)
        last_text = text[last.textStart(): last.textStart() + last.textLength()]
        elided = QFontMetricsF(font).elidedText(
            last_text + rest, Qt.TextElideMode.ElideRight, rect.width()
        )
        shown = shown[: len(shown) - len(last_text)] + elided
    return _centered_layout(shown, font, rect, step), size


def _mostly_cjk(text: str) -> bool:
    chars = [ch for ch in str(text or "") if not ch.isspace()]
    return bool(chars) and sum(_is_cjk(ch) for ch in chars) * 2 >= len(chars)


def fit_vertical(
    text: str, rect: QRectF, font_size: float, pitch: float
) -> tuple[QFont, list[tuple[str, QPointF]], float]:
    """竖排：字头朝上从上到下排，列从右到左。

    依次尝试：原字号 + 原列距 → 原字号 + 收紧列距 → 逐级缩小字号 → 截断加省略号。
    """
    chars = [ch for ch in str(text or "") if not ch.isspace()]
    base = max(8.0, float(font_size))
    minimum = max(8.0, base * _MIN_SCALE)
    size = base
    fitted = False
    while not fitted:
        font = _make_font(round(size))
        metrics = QFontMetricsF(font)
        step_y = size * 1.05
        per_col = max(1, int((rect.height() + 2.0) // step_y))
        columns = max(1, math.ceil(len(chars) / per_col))
        loose = max(pitch * size / base, size * 1.15)
        for col_step in dict.fromkeys((loose, min(loose, size * _TIGHT_STEP))):
            if (columns - 1) * col_step + size <= rect.width() + 2.0:
                fitted = True
                break
        if fitted or size <= minimum:
            break
        size = max(minimum, size - 1.0)
    max_columns = max(1, 1 + int((rect.width() + 2.0 - size) // col_step))
    if len(chars) > max_columns * per_col:
        chars = chars[: max_columns * per_col - 1] + ["…"]
    glyphs = []
    for index, ch in enumerate(chars):
        column, row = divmod(index, per_col)
        center_x = rect.right() - size / 2.0 - column * col_step
        top = rect.top() + row * step_y
        baseline = top + (step_y - metrics.height()) / 2.0 + metrics.ascent()
        glyphs.append((ch, QPointF(center_x - metrics.horizontalAdvance(ch) / 2.0, baseline)))
    return font, glyphs, size


class ReplacementOverlay(QWidget):
    """鼠标穿透的透明覆盖层；截屏模式额外画冻结底图。"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(_FLAGS_TOP | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._payload = ReplacementPayload((0, 0), (0, 0))
        self._layer_owner: int | None = None
        self._comparing = False
        self._base: QImage | None = None
        self._blocks: list[_RenderedBlock] = []

    @property
    def payload(self) -> ReplacementPayload:
        return self._payload

    @property
    def comparing(self) -> bool:
        return self._comparing

    def set_comparing(self, comparing: bool) -> None:
        """对比：有冻结底图时只显示原图，否则整层隐藏、透出真实画面。"""
        self._comparing = bool(comparing)
        self._refresh_visibility()
        self.update()

    def showEvent(self, event: QShowEvent):
        QWidget.showEvent(self, event)
        # 排除截图捕获，持续翻译不会把自己的译文再识别一遍
        _exclude_from_capture(self)

    def set_layer_owner(self, owner_hwnd: int | None) -> None:
        self._layer_owner = int(owner_hwnd) if owner_hwnd else None
        dispatch_set_overlay_layer(self, self._layer_owner)

    def restack_layer(self) -> None:
        if not self.isVisible():
            return
        if self._layer_owner:
            restack_above_owner(self, self._layer_owner)
        else:
            dispatch_set_overlay_layer(self, None)

    def update_geometry(self, rect: tuple[int, int, int, int]) -> None:
        x, y, width, height = rect
        if _set_geo_if_changed(self, int(x), int(y), int(width), int(height)):
            self._relayout()

    def set_payload(self, payload: ReplacementPayload | None) -> None:
        self._payload = payload or ReplacementPayload((0, 0), (0, 0))
        self._base = _bgr_image(self._payload.base_image)
        self._relayout()

    def clear(self) -> None:
        self._payload = ReplacementPayload((0, 0), (0, 0))
        self._base = None
        self._blocks = []
        self._comparing = False
        self.hide()

    def snapshot(self) -> QImage | None:
        """截屏模式下的合成结果（用于保存图片）；无冻结底图时返回 None。

        按截图原始像素尺寸输出：高分屏缩放下窗口尺寸比截图小，直接抓窗口会变糊。
        """
        if self._base is None:
            return None
        image = QImage(self._base.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        if self.width() and self.height():
            painter.scale(image.width() / self.width(), image.height() / self.height())
        self._paint(painter, comparing=False)
        painter.end()
        return image

    def _scale(self) -> tuple[float, float]:
        pw, ph = self._payload.size
        sx = self.width() / pw if pw and self.width() else 1.0
        sy = self.height() / ph if ph and self.height() else 1.0
        return sx, sy

    def _map(self, box, sx: float, sy: float) -> QRectF:
        ox, oy = self._payload.origin
        x1, y1, x2, y2 = box
        return QRectF((x1 - ox) * sx, (y1 - oy) * sy, (x2 - x1) * sx, (y2 - y1) * sy)

    def _map_point(self, x: float, y: float, sx: float, sy: float) -> QPointF:
        ox, oy = self._payload.origin
        return QPointF((x - ox) * sx, (y - oy) * sy)

    def _render_block(self, block: ReplacementBlock, sx: float, sy: float) -> _RenderedBlock:
        fills = [
            _Fill(
                self._map(line.box, sx, sy),
                QColor(*line.fill),
                _bgr_image(line.patch),
                QPolygonF([self._map_point(x, y, sx, sy) for x, y in line.polygon])
                if line.polygon else None,
            )
            for line in block.lines
        ]
        color = QColor(*block.text_color)
        scale = (sx + sy) / 2.0
        if block.vertical and _mostly_cjk(block.text):
            # 竖排原文、中日韩译文：照原样竖排
            font, glyphs, _ = fit_vertical(
                block.text, self._map(block.rect, sx, sy),
                block.font_size * scale, block.line_pitch * scale,
            )
            return _RenderedBlock(fills, color, glyphs=glyphs, font=font)
        if block.angle and block.center is not None:
            # 倾斜（或竖排但译文是拉丁文）：在顺着文字方向的坐标系里横排，再整体旋转
            local = QRectF(
                -block.length * scale / 2.0, -block.thickness * scale / 2.0,
                block.length * scale, block.thickness * scale,
            )
            layout, _ = fit_block(block.text, local, block.font_size * scale, block.line_pitch * scale)
            return _RenderedBlock(
                fills, color, layout=layout, position=local.topLeft(),
                center=self._map_point(*block.center, sx, sy), angle=block.angle,
            )
        rect = self._map(block.rect, sx, sy)
        layout, _ = fit_block(block.text, rect, block.font_size * sy, block.line_pitch * sy)
        return _RenderedBlock(fills, color, layout=layout, position=rect.topLeft())

    def _relayout(self) -> None:
        sx, sy = self._scale()
        self._blocks = [self._render_block(block, sx, sy) for block in self._payload.blocks]
        self._refresh_visibility()
        self.update()

    def _refresh_visibility(self) -> None:
        has_content = bool(self._blocks) or self._base is not None
        if not has_content or (self._comparing and self._base is None):
            self.hide()
            return
        _show_once(self)
        self.restack_layer()

    def _paint(self, painter: QPainter, *, comparing: bool) -> None:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if self._base is not None:
            painter.drawImage(QRectF(self.rect()), self._base)
        if comparing:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for block in self._blocks:
            for fill in block.fills:
                if fill.polygon is not None:
                    path = QPainterPath()
                    path.addPolygon(fill.polygon)
                    path.closeSubpath()
                    if fill.patch is not None:
                        painter.save()
                        painter.setClipPath(path)
                        painter.drawImage(fill.rect, fill.patch)
                        painter.restore()
                    else:
                        painter.fillPath(path, fill.color)
                elif fill.patch is not None:
                    painter.drawImage(fill.rect, fill.patch)
                else:
                    painter.fillRect(fill.rect, fill.color)
            painter.setPen(block.color)
            if block.glyphs is not None:
                painter.setFont(block.font)
                for ch, point in block.glyphs:
                    painter.drawText(point, ch)
            elif block.center is not None:
                painter.save()
                painter.translate(block.center)
                painter.rotate(block.angle)
                block.layout.draw(painter, block.position)
                painter.restore()
            else:
                block.layout.draw(painter, block.position)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        self._paint(painter, comparing=self._comparing)
        if self._base is not None:
            # 截屏覆盖用白色虚线框标出范围（保存图片时不画）
            pen = QPen(QColor(255, 255, 255, 220), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()


__all__ = ["ReplacementOverlay", "fit_block", "fit_vertical"]
