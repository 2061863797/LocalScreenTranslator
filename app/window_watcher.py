# -*- coding: utf-8 -*-
"""持续翻译监视线程：定时捕获目标（窗口或屏幕区域）→ OCR → 差异检测 → 翻译。"""

import threading
import time
import traceback

import numpy as np
from PySide6.QtCore import QThread, Signal

from . import capture
from .applog import get_logger
from .ocr_engine import OcrEngine
from .pipelines import LiveTranslationState
from .textlang import is_already_target_language
from .translator import Translator

# 备注缓存哨兵：已是目标语，跳过翻译且不叠标签
_SKIP_TARGET = "\x00SKIP_TARGET"

_log = get_logger("watch")


def _dilate_mask(mask: np.ndarray, radius: int = 2) -> np.ndarray:
    """扩张少量像素，覆盖 DWM 缩放和文字抗锯齿产生的边缘。"""
    active = np.asarray(mask, dtype=bool)
    if not active.any() or radius <= 0:
        return active.copy()
    try:
        import cv2

        ksize = 2 * radius + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize))
        m_u8 = active.view(np.uint8) if active.dtype == bool else active.astype(np.uint8)
        dilated = cv2.dilate(m_u8, kernel)
        return dilated > 0
    except Exception:
        height, width = active.shape
        expanded = active.copy()
        for dy in range(-radius, radius + 1):
            src_y1 = max(0, -dy)
            src_y2 = min(height, height - dy)
            dst_y1 = max(0, dy)
            dst_y2 = min(height, height + dy)
            for dx in range(-radius, radius + 1):
                src_x1 = max(0, -dx)
                src_x2 = min(width, width - dx)
                dst_x1 = max(0, dx)
                dst_x2 = min(width, width + dx)
                expanded[dst_y1:dst_y2, dst_x1:dst_x2] |= active[
                    src_y1:src_y2, src_x1:src_x2
                ]
        return expanded


def _is_invalid_window_handle_error(exc: BaseException) -> bool:
    """目标窗在检查与截图之间关闭时，pywin32 返回错误 1400。"""
    code = getattr(exc, "winerror", None)
    if code is None and exc.args:
        code = exc.args[0]
    return code == 1400


class WindowWatcher(QThread):
    """每个设定间隔持续 OCR，文字有实质变化时才调用翻译模型。

    监视源二选一：hwnd（窗口，含被遮挡部分）或 region（屏幕区域 x,y,w,h）。

    subtitle_ready(translation)      译文更新（字幕条模式）
    annotations_ready(items)         逐行 [(box, 译文), ...]（备注模式）
    history_ready(source, translation, mode)  有实质新译文时写入历史
    window_moved(x, y, w, h)         目标位置变化
    stopped(reason)                  监视结束
    """

    subtitle_ready = Signal(str)
    annotations_ready = Signal(list)
    history_ready = Signal(str, str, str)
    window_moved = Signal(int, int, int, int)
    content_cleared = Signal()
    stopped = Signal(str)

    # 画面逐字节未变时最多连续跳过的 OCR 轮数（保险起见周期性强制识别）
    _MAX_SKIPPED_FRAMES = 5

    def __init__(self, ocr: OcrEngine, translator: Translator, cfg: dict,
                 hwnd: int | None = None,
                 region: tuple[int, int, int, int] | None = None,
                 display_mode: str = "subtitle",
                 profile: str = "window"):
        """profile: \"window\" | \"region\"，决定读哪套间隔/阈值配置。"""
        super().__init__()
        assert (hwnd is None) != (region is None), "hwnd 与 region 必须二选一"
        assert profile in ("window", "region"), "profile 须为 window 或 region"
        self._hwnd = hwnd
        self._region = region
        self._region_lock = threading.Lock()
        self._annotation_mask_lock = threading.Lock()
        self._annotation_mask: np.ndarray | None = None
        self._annotation_clean_frame: np.ndarray | None = None
        self._ocr = ocr
        self._translator = translator
        self._cfg = cfg
        self._display_mode = display_mode
        self._profile = profile
        self._running = True
        self._paused = threading.Event()
        self._state = LiveTranslationState()
        # _state 会被主线程（set_display_mode/set_region）与监视线程并发访问；
        # pipelines 的状态机本身无锁，统一在这里用 RLock 保护。
        self._state_lock = threading.RLock()
        self._last_rect = None
        self._last_skip_target: bool | None = None
        # 静止画面跳过 OCR 用：上一帧原始图像与已连续跳过帧数
        self._last_frame: np.ndarray | None = None
        self._skipped_frames = 0

    @property
    def _last_text(self):
        with self._state_lock:
            return self._state.last_text

    @_last_text.setter
    def _last_text(self, value):
        with self._state_lock:
            self._state.last_text = value

    @property
    def _empty_ocr_frames(self):
        with self._state_lock:
            return self._state.empty_frames

    @_empty_ocr_frames.setter
    def _empty_ocr_frames(self, value):
        with self._state_lock:
            self._state.empty_frames = value

    @property
    def _line_tr_cache(self):
        with self._state_lock:
            return self._state.line_cache

    @_line_tr_cache.setter
    def _line_tr_cache(self, value):
        with self._state_lock:
            self._state.line_cache = value

    def set_display_mode(self, mode: str):
        if mode not in ("subtitle", "annotate"):
            return
        self._display_mode = mode
        with self._state_lock:
            self._state.reset(clear_cache=True)
        self._last_skip_target = None
        # 单次赋值原子；最坏多跑一帧 OCR
        self._last_frame = None
        self._skipped_frames = 0
        self.set_annotation_mask(None, reset_reference=True)

    def set_region(self, region: tuple[int, int, int, int] | None):
        """区域监视时拖动选区后更新（线程安全）。"""
        with self._region_lock:
            self._region = region
        # 选区变了，旧行框可能失效，但同文案译文仍可复用
        with self._state_lock:
            self._state.reset(clear_cache=False)
        self._last_frame = None
        self._skipped_frames = 0
        self.set_annotation_mask(None, reset_reference=True)

    def set_annotation_mask(
        self, mask: np.ndarray | None, *, reset_reference: bool = False
    ) -> None:
        """更新译文像素遮罩；布局重置时同时丢弃旧干净帧。"""
        prepared = None
        if mask is not None and mask.ndim == 2 and mask.size:
            prepared = _dilate_mask(mask > 0)
        with self._annotation_mask_lock:
            self._annotation_mask = prepared
            if reset_reference:
                self._annotation_clean_frame = None

    def _remove_annotation_overlay(self, image: np.ndarray) -> np.ndarray:
        """用上一张干净帧恢复译文像素，OCR 永远只看到原始区域。"""
        with self._annotation_mask_lock:
            mask = self._annotation_mask
            reference = self._annotation_clean_frame

        clean = image
        mask_matches = mask is not None and mask.shape == image.shape[:2]
        reference_matches = (
            reference is not None and reference.shape == image.shape
        )
        if mask_matches and reference_matches:
            clean = image.copy()
            clean[mask] = reference[mask]
        elif mask_matches:
            # 正常流程在首个译文出现前已有干净帧；这里只处理极端竞态。
            try:
                import cv2

                clean = cv2.inpaint(
                    image,
                    mask.astype(np.uint8) * 255,
                    2,
                    cv2.INPAINT_TELEA,
                )
            except Exception:
                clean = image.copy()

        with self._annotation_mask_lock:
            self._annotation_clean_frame = clean.copy()
        return clean

    def _notify_cleared(self) -> None:
        self.content_cleared.emit()
        _log.info("连续两轮未识别到文字，已清空持续翻译显示")

    def _observe_empty_ocr_frame(self) -> None:
        """连续两轮无文字才清空，兼顾及时消失和单帧 OCR 抖动。"""
        with self._state_lock:
            event, _ = self._state.observe([], 0.0)
        if event == "clear":
            self._notify_cleared()

    def stop(self):
        self._running = False

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def _grab(self):
        if self._region is not None:
            with self._region_lock:
                region = self._region
            if region is None:
                return None, None
            x, y, w, h = region
            if w <= 0 or h <= 0:
                return None, None
            return region, capture.grab_region(x, y, w, h)
        rect = capture.get_window_rect(self._hwnd)
        return rect, capture.grab_window(self._hwnd)

    def run(self):
        _log.info(
            "监视线程启动 profile=%s mode=%s hwnd=%s region=%s",
            self._profile, self._display_mode, self._hwnd, self._region,
        )
        try:
            while self._running:
                while self._running and self._paused.is_set():
                    time.sleep(0.05)
                if not self._running:
                    break
                # 窗口 / 区域各自一套间隔与文本变化阈值
                # _cfg 为共享引用：设置保存后下一轮立即生效。单键读取在 GIL
                # 下是原子的，最坏用到上一帧旧值，无需加锁。
                p = self._profile
                interval = self._cfg[f"{p}_watch_interval_ms"] / 1000.0
                threshold = float(self._cfg[f"{p}_watch_diff_threshold"])
                target = self._cfg["target_language"]
                # 备注「跳过目标语」开关变化：立刻清空缓存并强制重跑一帧
                if self._display_mode == "annotate":
                    skip_key = f"{self._profile}_annotate_skip_target_lang"
                    skip_now = bool(self._cfg.get(skip_key))
                    if skip_now != self._last_skip_target:
                        self._last_skip_target = skip_now
                        with self._state_lock:
                            self._state.reset(clear_cache=True)
                t0 = time.time()

                try:
                    rect, img = self._grab()
                except Exception as e:
                    if not _is_invalid_window_handle_error(e):
                        raise
                    _log.info("监视目标窗口已关闭 hwnd=%s", self._hwnd)
                    self.stopped.emit("目标窗口已关闭")
                    return
                if img is None:
                    _log.warning("监视目标无法捕获，结束")
                    self.stopped.emit("监视目标已关闭或无法捕获")
                    return
                if rect != self._last_rect:
                    self._last_rect = rect
                    self.window_moved.emit(*rect)

                # 画面逐字节未变时跳过 OCR（结果必然相同）；为防极端情况
                # 每隔 _MAX_SKIPPED_FRAMES 帧仍强制识别一次。
                if (
                    self._last_frame is not None
                    and self._skipped_frames < self._MAX_SKIPPED_FRAMES
                    and img.shape == self._last_frame.shape
                    and np.array_equal(img, self._last_frame)
                ):
                    self._skipped_frames += 1
                else:
                    self._skipped_frames = 0
                    self._last_frame = img
                    # 仅当浮层允许被捕获（会出现在自家抓屏里）才需要还原底图
                    if (
                        self._profile == "region"
                        and self._display_mode == "annotate"
                        and bool(self._cfg.get("annotate_capture_visible"))
                    ):
                        img = self._remove_annotation_overlay(img)
                    lines = self._ocr.recognize(img)
                    with self._state_lock:
                        event, text = self._state.observe(lines, threshold)

                    if event == "clear":
                        self._notify_cleared()
                    elif event == "change":
                        mode_tag = (
                            f"{self._profile}_annotate"
                            if self._display_mode == "annotate"
                            else f"{self._profile}_subtitle"
                        )
                        if self._display_mode == "annotate":
                            items, translation = self._annotate_translate(
                                lines, target
                            )
                            if self._running:
                                self.annotations_ready.emit(items)
                                if translation:
                                    self.history_ready.emit(text, translation, mode_tag)
                        else:
                            translation = self._translator.translate(text, target)
                            if self._running:
                                self.subtitle_ready.emit(translation)
                                if translation:
                                    self.history_ready.emit(text, translation, mode_tag)
                        _log.info(
                            "监视重译 mode=%s chars=%d elapsed=%.2fs",
                            self._display_mode, len(text), time.time() - t0,
                        )

                remaining = max(0.02, interval - (time.time() - t0))
                end = time.time() + remaining
                while self._running and time.time() < end:
                    time.sleep(min(0.02, max(0.001, end - time.time())))

            _log.info("监视线程正常停止")
            self.stopped.emit("已停止监视")
        except Exception:
            _log.exception("监视线程异常")
            self.stopped.emit("监视出错：\n" + traceback.format_exc(limit=3))

    def _annotate_translate(self, lines, target: str):
        """备注：按行增量翻译，稳定原文走缓存，只请求变化行。

        可选跳过已是目标语言的行（不调模型、不叠标签）。
        """
        skip_key = f"{self._profile}_annotate_skip_target_lang"
        skip_target = bool(self._cfg.get(skip_key))

        srcs = [ln.text.strip() for ln in lines]
        todo_text: list[str] = []
        skipped = 0
        with self._state_lock:
            cache = self._state.line_cache
            for s in srcs:
                if not s:
                    continue
                if s in cache:
                    continue
                if skip_target and is_already_target_language(s, target):
                    cache[s] = _SKIP_TARGET
                    skipped += 1
                    continue
                todo_text.append(s)

        if todo_text and self._running:
            unique = list(dict.fromkeys(todo_text))
            # 翻译请求不持锁：期间主线程可安全清缓存（重置后会重新累积）
            trs = self._translator.translate_lines(unique, target)
            with self._state_lock:
                for s, tr in zip(unique, trs):
                    if tr:
                        self._state.line_cache[s] = tr
                self._state.prune_cache(srcs)

        items = []
        parts = []
        with self._state_lock:
            cache = self._state.line_cache
            for ln, s in zip(lines, srcs):
                if not s:
                    continue
                tr = cache.get(s, "")
                if not tr or tr == _SKIP_TARGET:
                    continue
                items.append((ln.box, tr))
                parts.append(tr)
            cache_size = len(cache)
        _log.info(
            "备注增量译 total=%d new=%d skip_target=%d cache=%d skip_on=%s",
            len([s for s in srcs if s]),
            len(todo_text),
            skipped,
            cache_size,
            skip_target,
        )
        return items, "\n".join(parts)
