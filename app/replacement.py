# -*- coding: utf-8 -*-
"""原位替换翻译的纯算法部分（不依赖 Qt，可在工作线程调用）。

做法对标微软电脑管家 / Google 镜头：
1. 把 OCR 行合成段落，整段翻译后在原段落范围内重新折行；
2. 每行原文位置铺上从周围取样的背景色（复杂背景才用 OpenCV 修补）；
3. 译文沿用原文的文字颜色、字号和粗细。
覆盖层只负责把 payload 画出来。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np

try:  # OpenCV 随运行环境提供；缺失时退回插值底图
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

Box = tuple[int, int, int, int]
Color = tuple[int, int, int, int]  # RGBA
Point = tuple[float, float]

# 环带颜色离散度超过该值视为复杂背景，改用修补而非纯色
_COMPLEX_BG_SPREAD = 28.0
# 倾斜小于该角度按水平行处理
_STRAIGHT_DEGREES = 3.0
# 两行方向相差小于该角度才可能属于同一段
_SAME_DIRECTION_DEGREES = 6.0
# 两行文字色（RGB）相差超过该距离视为不同段，例如蓝色标题链接与白色正文
_SAME_COLOR_DISTANCE = 60.0


@dataclass(frozen=True)
class ReplacementLine:
    """一行原文要被盖住的区域。"""

    box: Box
    fill: Color
    # 复杂背景时为修补后的 BGR 小图（与 box 同尺寸），否则为 None 并铺纯色
    patch: np.ndarray | None = None
    # 倾斜/竖排行实际要盖住的四边形（帧内像素）；水平行为 None，直接盖 box
    polygon: tuple[Point, Point, Point, Point] | None = None


@dataclass(frozen=True)
class ReplacementBlock:
    """一个段落：若干原文行 + 一段译文。坐标均为帧内像素。"""

    rect: Box
    lines: tuple[ReplacementLine, ...]
    text: str
    source: str
    font_size: int
    text_color: Color
    # 原文相邻两行顶边的距离；单行段落为行高
    line_pitch: float = 0.0
    complex_background: bool = False
    # 阅读方向相对水平向右的角度（度，顺时针为正，范围 (-90, 90]）；0 为水平
    angle: float = 0.0
    # 倾斜段落在“顺着文字方向”的坐标系里的中心、长度（沿阅读方向）和厚度
    center: Point | None = None
    length: float = 0.0
    thickness: float = 0.0
    # 中日韩竖排（字头朝上、从上到下、列从右到左）
    vertical: bool = False


@dataclass(frozen=True)
class ReplacementPayload:
    origin: tuple[int, int]
    size: tuple[int, int]
    blocks: tuple[ReplacementBlock, ...] = ()
    # 截屏模式的冻结底图（BGR）；持续翻译为 None，透出实时画面
    base_image: np.ndarray | None = None


# ---------- 分段 ----------

def _box_height(box: Sequence[int]) -> int:
    return max(1, int(box[3]) - int(box[1]))


def _same_paragraph(prev: Box, cur: Box, gap_ratio: float = 0.5) -> bool:
    """判断 cur 行是否紧接 prev 行、属于同一段落（合段后整段翻译、重新折行）。

    prev / cur 都是 (左, 上, 右, 下) 像素坐标，cur 在 prev 下方。
    三条都满足才合段：左边对齐、行高接近（区分标题与正文）、
    行间距小于 gap_ratio 行（区分段落与紧挨着的下一行信息）；竖排列距本来就宽，放宽到 1.5 列。
    """
    height = _box_height(prev)
    aligned = abs(cur[0] - prev[0]) <= 0.6 * height
    # 识别框高度会因有无下伸笔画（g、p、逗号）差两三像素，留出余量
    similar = 0.8 <= _box_height(cur) / height <= 1.25
    close = cur[1] - prev[3] < gap_ratio * height
    return aligned and similar and close


@dataclass(frozen=True)
class LineOrientation:
    """一行文字的朝向：中心、沿阅读方向的长度、厚度和角度（度）。"""

    cx: float
    cy: float
    length: float
    thickness: float
    angle: float


def _line_points(line: Any) -> np.ndarray:
    quad = getattr(line, "quad", None)
    if quad:
        return np.asarray(quad, dtype=np.float32).reshape(-1, 2)
    x1, y1, x2, y2 = (float(v) for v in line.box)
    return np.asarray([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], dtype=np.float32)


def line_orientation(line: Any) -> LineOrientation:
    """由 OCR 四边形求文字朝向；近乎水平或近乎方块（单字）时按水平处理。"""
    x1, y1, x2, y2 = (float(v) for v in line.box)
    straight = LineOrientation((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1, 0.0)
    if not getattr(line, "quad", None) or cv2 is None:
        return straight
    rect = cv2.minAreaRect(_line_points(line))
    corners = cv2.boxPoints(rect)
    edge_a, edge_b = corners[1] - corners[0], corners[2] - corners[1]
    len_a, len_b = float(np.hypot(*edge_a)), float(np.hypot(*edge_b))
    along, length, thickness = (edge_a, len_a, len_b) if len_a >= len_b else (edge_b, len_b, len_a)
    if thickness <= 0 or length < 1.3 * thickness:
        return straight
    angle = math.degrees(math.atan2(float(along[1]), float(along[0])))
    while angle <= -90.0:
        angle += 180.0
    while angle > 90.0:
        angle -= 180.0
    if angle < -80.0:
        # 接近竖直时统一按“从上往下读”（与 OCR 把高窄行旋转后识别的方向一致）
        angle += 180.0
    if abs(angle) < _STRAIGHT_DEGREES:
        return straight
    (cx, cy), _, _ = rect
    return LineOrientation(float(cx), float(cy), length, thickness, angle)


def _to_frame(points: np.ndarray, angle: float) -> np.ndarray:
    """把帧内坐标转到“阅读方向朝右”的坐标系（绕原点旋转 -angle）。"""
    t = math.radians(angle)
    c, s = math.cos(t), math.sin(t)
    p = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    return np.stack([p[:, 0] * c + p[:, 1] * s, -p[:, 0] * s + p[:, 1] * c], axis=1)


def _from_frame(x: float, y: float, angle: float) -> Point:
    t = math.radians(angle)
    c, s = math.cos(t), math.sin(t)
    return x * c - y * s, x * s + y * c


def _frame_box(line: Any, angle: float) -> tuple[float, float, float, float]:
    if angle == 0.0:
        return tuple(float(v) for v in line.box)  # type: ignore[return-value]
    p = _to_frame(_line_points(line), angle)
    return float(p[:, 0].min()), float(p[:, 1].min()), float(p[:, 0].max()), float(p[:, 1].max())


def _same_direction(a: float, b: float) -> bool:
    diff = abs(a - b) % 180.0
    return min(diff, 180.0 - diff) < _SAME_DIRECTION_DEGREES


def group_blocks(lines: Sequence[Any], frame: np.ndarray | None = None) -> list[list[Any]]:
    """按阅读顺序把 OCR 行合成段落；每个元素需有 ``box`` 与 ``text``。

    方向相同的行才可能合段。倾斜或竖排的行先转到“顺着文字方向”的坐标系，
    再用与水平行相同的规则判断；竖排时下一列在左边，转换后正好成了“下一行”。
    给出截图 frame 时还要求文字颜色相近：粗体标题与正文的识别框高度往往差不多，
    只靠位置分不开，颜色不同（如蓝色链接标题、白色描述）就不合段。
    """
    colors: dict[int, np.ndarray] = {}

    def same_color(prev: Any, cur: Any) -> bool:
        if frame is None:
            return True
        for line in (prev, cur):
            if id(line) not in colors:
                colors[id(line)] = _line_color(frame, line)
        return float(np.linalg.norm(colors[id(prev)] - colors[id(cur)])) <= _SAME_COLOR_DISTANCE

    clusters: list[tuple[float, list[tuple[int, Any]]]] = []
    for index, line in enumerate(lines):
        if not str(getattr(line, "text", "") or "").strip():
            continue
        angle = line_orientation(line).angle
        for cluster_angle, members in clusters:
            if _same_direction(cluster_angle, angle):
                members.append((index, line))
                break
        else:
            clusters.append((angle, [(index, line)]))

    blocks: list[tuple[tuple[int, int], list[Any]]] = []
    for angle, members in clusters:
        first_index = members[0][0]
        if angle != 0.0:
            # 水平行沿用 OCR 的阅读顺序；其它方向按转换后的“行”从上到下排序
            members = sorted(members, key=lambda m: (_frame_box(m[1], angle)[1], _frame_box(m[1], angle)[0]))
        vertical_cjk = abs(angle - 90.0) < 10.0 and any(
            _is_cjk(ch) for _, line in members for ch in str(line.text)
        )
        gap_ratio = 1.5 if vertical_cjk else 0.5
        current: list[Any] | None = None
        for seq, (index, line) in enumerate(members):
            if (
                current
                and _same_paragraph(_frame_box(current[-1], angle), _frame_box(line, angle), gap_ratio)
                and same_color(current[-1], line)
            ):
                current.append(line)
            else:
                current = [line]
                # 水平段落按 OCR 顺序；同方向的倾斜/竖排段落排在一起，内部按阅读顺序
                key = (index, 0) if angle == 0.0 else (first_index, seq)
                blocks.append((key, current))
    blocks.sort(key=lambda item: item[0])
    return [block for _, block in blocks]


def _line_color(frame: np.ndarray, line: Any) -> np.ndarray:
    """一行的文字色（RGB），用于判断相邻两行是否同一段。"""
    height, width = frame.shape[:2]
    box = _normalize_box(line.box, width, height)
    if box is None:
        return np.zeros(3, dtype=np.float32)
    return np.asarray(analyze_line(frame, box).text_color[:3], dtype=np.float32)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF      # 日文假名
        or 0x3400 <= code <= 0x9FFF   # 中日韩统一表意文字
        or 0xAC00 <= code <= 0xD7AF   # 韩文
        or 0xFF00 <= code <= 0xFFEF   # 全角符号
        or 0x3000 <= code <= 0x303F   # 中文标点
    )


def join_block_text(texts: Iterable[str]) -> str:
    """段内各行拼成一句：拉丁文用空格、中日韩直接相连、行尾连字符去掉。"""
    out = ""
    for raw in texts:
        part = str(raw or "").strip()
        if not part:
            continue
        if not out:
            out = part
        elif out.endswith("-") and part[:1].islower():
            out = out[:-1] + part
        elif _is_cjk(out[-1]) or _is_cjk(part[0]):
            out += part
        else:
            out += " " + part
    return out


# ---------- 取色与字号 ----------

def _ring_pixels(frame: np.ndarray, box: Box) -> np.ndarray:
    """行框外 2～4px 环带像素（BGR，N×3）。"""
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = box
    pad = max(2, min(4, _box_height(box) // 6))
    px1, py1 = max(0, x1 - pad), max(0, y1 - pad)
    px2, py2 = min(width, x2 + pad), min(height, y2 + pad)
    crop = frame[py1:py2, px1:px2, :3]
    outside = np.ones(crop.shape[:2], dtype=bool)
    outside[y1 - py1:y2 - py1, x1 - px1:x2 - px1] = False
    pixels = crop[outside].reshape(-1, 3)
    if len(pixels) >= 8:
        return pixels
    # 行框贴边时用框内边缘代替
    inner = frame[y1:y2, x1:x2, :3]
    return np.concatenate((inner[0], inner[-1], inner[:, 0], inner[:, -1])).reshape(-1, 3)


def _parse_color(value: Any) -> Color | None:
    if not isinstance(value, str) or not value.startswith("#") or len(value) != 7:
        return None
    try:
        r, g, b = (int(value[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return None
    return r, g, b, 255


def _contrast_color(background: np.ndarray) -> Color:
    b, g, r = (int(item) for item in background[:3])
    value = 24 if (r * 299 + g * 587 + b * 114) / 1000 > 145 else 245
    return value, value, value, 255


def estimate_text_color(crop: np.ndarray | None, background: np.ndarray, override: Any = "auto") -> Color:
    """取与背景差别最大的那批像素的中位色作为文字色；差别太小则用黑/白。"""
    parsed = _parse_color(override)
    if parsed is not None:
        return parsed
    if crop is not None and crop.size:
        pixels = np.asarray(crop)[..., :3].reshape(-1, 3).astype(np.float32)
        bg = np.asarray(background[:3], dtype=np.float32)
        distances = np.linalg.norm(pixels - bg, axis=1)
        # 细笔画大多是抗锯齿边缘，只取与背景差别最大的 5% 才是笔画本色
        threshold = max(24.0, float(np.percentile(distances, 95)))
        foreground = pixels[distances >= threshold]
        if len(foreground):
            color = np.median(foreground, axis=0)
            bg_light = float(np.dot(bg, [0.114, 0.587, 0.299]))
            fg_light = float(np.dot(color, [0.114, 0.587, 0.299]))
            if abs(bg_light - fg_light) >= 42 or float(np.linalg.norm(color - bg)) >= 80:
                b, g, r = (int(item) for item in color)
                return r, g, b, 255
    return _contrast_color(np.asarray(background[:3], dtype=np.uint8))


def _ink_height(crop: np.ndarray, background: np.ndarray) -> int:
    """与背景差别明显的像素在垂直方向上的跨度。"""
    distances = np.linalg.norm(
        crop[..., :3].astype(np.float32) - background[:3].astype(np.float32), axis=2
    )
    rows = np.where((distances >= max(40.0, float(distances.max()) * 0.35)).any(axis=1))[0]
    return int(rows[-1] - rows[0] + 1) if len(rows) else 0


_ASCENDERS = set("bdfhiklt0123456789/()[]{}|!?'\"#$%&@")
_DESCENDERS = set("gjpqy/()[]{}|,;")


def _ink_span(text: str) -> float:
    """按字符估计墨迹高度占字号的比例（拉丁小写字母没有上伸/下伸时墨迹很矮）。"""
    chars = [ch for ch in str(text or "") if not ch.isspace()]
    if not chars:
        return 1.0
    if any(_is_cjk(ch) for ch in chars):
        return 1.0
    ascender = any(ch in _ASCENDERS or ch.isupper() for ch in chars)
    descender = any(ch in _DESCENDERS for ch in chars)
    if ascender and descender:
        return 1.0
    if ascender or descender:
        return 0.74
    return 0.53


def fit_font_size(box: Box, ink_height: int = 0, source: str = "") -> int:
    """由墨迹高度估计像素字号；拿不到墨迹时按行框高度估计。"""
    box_height = _box_height(box)
    if ink_height > 0:
        size = round(ink_height / _ink_span(source))
    else:
        size = round(box_height * 0.72)
    return max(8, min(size, box_height))


def _box_polygon(box: Box) -> tuple[Point, Point, Point, Point]:
    """水平行框的四个角，与倾斜行共用 _inpaint_polygon。"""
    x1, y1, x2, y2 = (float(v) for v in box)
    return (x1, y1), (x2, y1), (x2, y2), (x1, y2)


@dataclass(frozen=True)
class LineAnalysis:
    fill: Color
    text_color: Color
    ink_height: int
    complex_background: bool


def analyze_line(frame: np.ndarray, box: Box) -> LineAnalysis:
    """从行框周围取背景色，从框内取文字色和墨迹高度。"""
    ring = _ring_pixels(frame, box).astype(np.float32)
    background = np.median(ring, axis=0)
    spread = float(np.percentile(np.linalg.norm(ring - background, axis=1), 80))
    x1, y1, x2, y2 = box
    crop = frame[y1:y2, x1:x2, :3]
    b, g, r = (int(v) for v in background[:3])
    return LineAnalysis(
        fill=(r, g, b, 255),
        text_color=estimate_text_color(crop, background),
        ink_height=_ink_height(crop, background),
        complex_background=spread > _COMPLEX_BG_SPREAD,
    )


def _straighten(frame: np.ndarray, orient: LineOrientation, pad: int) -> tuple[np.ndarray, Box]:
    """把倾斜的一行旋转摆正后裁出来，返回 (裁图, 文字所在的内框)。"""
    length, thickness = max(2, int(round(orient.length))), max(2, int(round(orient.thickness)))
    width, height = length + 2 * pad, thickness + 2 * pad
    matrix = cv2.getRotationMatrix2D((orient.cx, orient.cy), orient.angle, 1.0)
    matrix[0, 2] += width / 2.0 - orient.cx
    matrix[1, 2] += height / 2.0 - orient.cy
    crop = cv2.warpAffine(
        np.ascontiguousarray(frame[..., :3]), matrix, (width, height),
        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE,
    )
    return crop, (pad, pad, pad + length, pad + thickness)


def _oriented_polygon(orient: LineOrientation, pad: float) -> tuple[Point, Point, Point, Point]:
    """行的旋转矩形向外扩 pad 像素后的四个角（帧内坐标）。"""
    half_l, half_t = orient.length / 2.0 + pad, orient.thickness / 2.0 + pad
    corners = []
    for dx, dy in ((-half_l, -half_t), (half_l, -half_t), (half_l, half_t), (-half_l, half_t)):
        ox, oy = _from_frame(dx, dy, orient.angle)
        corners.append((orient.cx + ox, orient.cy + oy))
    return tuple(corners)  # type: ignore[return-value]


def _polygon_box(frame: np.ndarray, polygon: Sequence[Point]) -> Box:
    """四边形在帧内的外接框。"""
    height, width = frame.shape[:2]
    pts = np.asarray(polygon, dtype=np.float32)
    x1, y1 = max(0, int(math.floor(pts[:, 0].min()))), max(0, int(math.floor(pts[:, 1].min())))
    x2, y2 = min(width, int(math.ceil(pts[:, 0].max()))), min(height, int(math.ceil(pts[:, 1].max())))
    return x1, y1, max(x1 + 1, x2), max(y1 + 1, y2)


def _inpaint_polygon(frame: np.ndarray, polygon: Sequence[Point]) -> np.ndarray | None:
    """修补四边形区域，返回与外接框同尺寸的修补图。"""
    height, width = frame.shape[:2]
    pts = np.asarray(polygon, dtype=np.float32)
    box = _polygon_box(frame, polygon)
    x1, y1 = box[0], box[1]
    if cv2 is None:
        return None
    margin = max(4, min(16, int(min(box[2] - x1, box[3] - y1) // 2)))
    px1, py1 = max(0, x1 - margin), max(0, y1 - margin)
    px2, py2 = min(width, box[2] + margin), min(height, box[3] + margin)
    crop = np.ascontiguousarray(frame[py1:py2, px1:px2, :3])
    mask = np.zeros(crop.shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [np.round(pts - (px1, py1)).astype(np.int32)], 255)
    try:
        repaired = cv2.inpaint(crop, mask, 3.0, cv2.INPAINT_TELEA)
    except cv2.error:
        return None
    return np.ascontiguousarray(repaired[y1 - py1:box[3] - py1, x1 - px1:box[2] - px1])


def _build_rotated_block(
    frame: np.ndarray, block_lines: Sequence[Any], text: str, angle: float,
) -> ReplacementBlock | None:
    """倾斜或竖排段落：在摆正的裁图上取色、量字号，盖住旋转后的四边形。"""
    if cv2 is None:
        return None
    height, width = frame.shape[:2]
    repaint: list[ReplacementLine] = []
    colors: list[Color] = []
    sizes: list[int] = []
    tops: list[float] = []
    points: list[np.ndarray] = []
    complex_any = False
    for line in block_lines:
        orient = line_orientation(line)
        orient = LineOrientation(orient.cx, orient.cy, orient.length, orient.thickness, angle)
        pad = max(4, int(orient.thickness // 4))
        crop, inner = _straighten(frame, orient, pad)
        info = analyze_line(crop, inner)
        polygon = _oriented_polygon(orient, 2.0)
        patch = _inpaint_polygon(frame, polygon) if info.complex_background else None
        repaint.append(ReplacementLine(_polygon_box(frame, polygon), info.fill, patch, polygon))
        colors.append(info.text_color)
        sizes.append(fit_font_size(inner, info.ink_height, str(getattr(line, "text", ""))))
        tops.append(_frame_box(line, angle)[1])
        points.append(_line_points(line))
        complex_any = complex_any or info.complex_background
    all_points = np.concatenate(points)
    framed = _to_frame(all_points, angle)
    fx1, fy1 = float(framed[:, 0].min()), float(framed[:, 1].min())
    fx2, fy2 = float(framed[:, 0].max()), float(framed[:, 1].max())
    center = _from_frame((fx1 + fx2) / 2.0, (fy1 + fy2) / 2.0, angle)
    pitch = (tops[-1] - tops[0]) / (len(tops) - 1) if len(tops) > 1 else fy2 - fy1
    rect = (
        max(0, int(math.floor(all_points[:, 0].min()))), max(0, int(math.floor(all_points[:, 1].min()))),
        min(width, int(math.ceil(all_points[:, 0].max()))), min(height, int(math.ceil(all_points[:, 1].max()))),
    )
    source = join_block_text(line.text for line in block_lines)
    return ReplacementBlock(
        rect=rect,
        lines=tuple(repaint),
        text=text,
        source=source,
        font_size=int(np.median(sizes)),
        text_color=colors[0],
        line_pitch=float(pitch),
        complex_background=complex_any,
        angle=float(angle),
        center=(float(center[0]), float(center[1])),
        length=fx2 - fx1,
        thickness=fy2 - fy1,
        vertical=abs(angle - 90.0) < 10.0 and any(_is_cjk(ch) for ch in source),
    )


def _expand(box: Box, width: int, height: int, pad: int = 2) -> Box:
    x1, y1, x2, y2 = box
    return max(0, x1 - pad), max(0, y1 - pad), min(width, x2 + pad), min(height, y2 + pad)


def _normalize_box(value: Any, width: int, height: int) -> Box | None:
    try:
        x1, y1, x2, y2 = (int(round(float(v))) for v in value)
    except (TypeError, ValueError):
        return None
    x1, x2 = sorted((max(0, min(width, x1)), max(0, min(width, x2))))
    y1, y2 = sorted((max(0, min(height, y1)), max(0, min(height, y2))))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return x1, y1, x2, y2


# ---------- 组装 ----------

def build_payload(
    frame: np.ndarray,
    blocks: Iterable[tuple[Sequence[Any], str]],
    *,
    origin: tuple[int, int] = (0, 0),
    frozen: bool = False,
) -> ReplacementPayload:
    """把 ``(段内 OCR 行, 段落译文)`` 列表转换成覆盖层 payload。"""
    height, width = frame.shape[:2]
    out: list[ReplacementBlock] = []
    for block_lines, translation in blocks:
        text = str(translation or "").strip()
        pairs = [
            (line, box) for line in block_lines
            if (box := _normalize_box(line.box, width, height)) is not None
        ]
        if not text or not pairs:
            continue
        angle = line_orientation(pairs[0][0]).angle
        if angle != 0.0:
            rotated = _build_rotated_block(frame, [line for line, _ in pairs], text, angle)
            if rotated is not None:
                out.append(rotated)
                continue
        boxes = [box for _, box in pairs]
        repaint: list[ReplacementLine] = []
        colors: list[Color] = []
        sizes: list[int] = []
        complex_any = False
        for line, box in pairs:
            info = analyze_line(frame, box)
            expanded = _expand(box, width, height)
            patch = _inpaint_polygon(frame, _box_polygon(expanded)) if info.complex_background else None
            repaint.append(ReplacementLine(expanded, info.fill, patch))
            colors.append(info.text_color)
            sizes.append(fit_font_size(box, info.ink_height, str(getattr(line, "text", ""))))
            complex_any = complex_any or info.complex_background
        first = boxes[0]
        font_size = int(np.median(sizes))
        if len(boxes) > 1:
            pitch = (boxes[-1][1] - boxes[0][1]) / (len(boxes) - 1)
        else:
            pitch = float(_box_height(first))
        rect = (
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        )
        out.append(ReplacementBlock(
            rect=rect,
            lines=tuple(repaint),
            text=text,
            source=join_block_text(line.text for line in block_lines),
            font_size=font_size,
            text_color=colors[0],
            line_pitch=float(pitch),
            complex_background=complex_any,
        ))
    return ReplacementPayload(
        origin=origin,
        size=(int(width), int(height)),
        blocks=tuple(out),
        base_image=np.ascontiguousarray(frame[..., :3]) if frozen else None,
    )


__all__ = [
    "LineAnalysis",
    "LineOrientation",
    "ReplacementBlock",
    "ReplacementLine",
    "ReplacementPayload",
    "analyze_line",
    "build_payload",
    "estimate_text_color",
    "fit_font_size",
    "group_blocks",
    "join_block_text",
    "line_orientation",
]
