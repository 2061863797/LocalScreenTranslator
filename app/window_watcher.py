# -*- coding: utf-8 -*-
"""持续翻译监视管线编排器 (PR 9 & PR 11).

解耦为单职责组件协同工作：
- CaptureService: 窗口/区域截屏与位移检测
- LatestFrameBuffer: 最新帧覆盖单槽缓冲（削峰防滞后）
- FrameChangeDetector: 两阶段轻量画面差分与噪点过滤
- AdaptivePollingController: 动态自适应轮询状态机
- OcrService: ROI 局部识别与全图 OCR 回退兜底
- TextChangeDetector: OCR 文本变化判定与空帧清空状态机
- TranslationManager: 字幕行级增量翻译与逐行标注翻译
- ResultManager: 世代校验与信号派发（防乱序覆盖）
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable, List, Optional, Tuple

import numpy as np
from PySide6.QtCore import QThread, Signal

from .adaptive_polling import AdaptivePollingController
from .applog import get_logger
from .capture_service import CaptureService, is_invalid_window_handle_error
from .frame_detector import FrameChangeDetector
from .latest_frame_buffer import LatestFrameBuffer, FramePacket
from .ocr_engine import OcrEngine
from .ocr_service import OcrService
from .ocr_stabilizer import OcrStabilizer
from .pipelines import GenerationTracker, WatchCycleContext
from .result_manager import ResultManager
from .scene_text_state import SceneTextState, _boxes_intersect
from .text_change_detector import TextChangeDetector
from .translation_manager import TranslationManager
from .translator import Translator

_log = get_logger("watch")

# 兼容常量与辅助函数
_SKIP_TARGET = "\x00SKIP_TARGET"
_FRAME_SAMPLE_STEP = 4
_FRAME_DIFF_TOLERANCE = 12


def _frame_changed(previous: np.ndarray, current: np.ndarray) -> bool:
    """当前帧相对上一帧是否有实质变化（尺寸不同一律算有变化）。"""
    if previous.shape != current.shape:
        return True
    step = _FRAME_SAMPLE_STEP
    before = previous[::step, ::step]
    after = current[::step, ::step]
    if before.size == 0:
        return True
    # int16 避免 uint8 相减回绕把小差当成大差
    diff = np.abs(before.astype(np.int16) - after).max()
    return bool(diff > _FRAME_DIFF_TOLERANCE)


def _is_invalid_window_handle_error(exc: BaseException) -> bool:
    """目标窗在检查与截图之间关闭时，pywin32 返回错误 1400 (兼容导出)。"""
    return is_invalid_window_handle_error(exc)


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


class WindowWatcher(QThread):
    """持续翻译监视管线编排线程 (PR 9 & PR 11).

    监视源二选一：hwnd（窗口，含被遮挡部分）或 region（屏幕区域 x,y,w,h）。

    保留全部既有公共信号：
    - subtitle_ready(translation)                译文更新（字幕条模式）
    - annotations_ready(items)                   逐行 [(box, 译文), ...]（备注模式）
    - history_ready(source, translation, mode)   有实质新译文时写入历史
    - window_moved(x, y, w, h)                   目标位置变化
    - content_cleared()                          连续无文字时清空
    - stopped(reason)                            监视结束
    """

    subtitle_ready = Signal((str,), (str, int, int))
    annotations_ready = Signal((list,), (list, int, int))
    history_ready = Signal((str, str, str), (str, str, str, int, int))
    window_moved = Signal(int, int, int, int)
    content_cleared = Signal()
    stopped = Signal(str)

    # 画面无实质变化时最多连续跳过的 OCR 轮数（保险起见周期性强制识别）
    _MAX_SKIPPED_FRAMES = 5
    _CLEAR_GRACE_SECONDS = 1.2

    def __init__(
        self,
        ocr: OcrEngine,
        translator: Translator,
        cfg: dict,
        hwnd: int | None = None,
        region: tuple[int, int, int, int] | None = None,
        display_mode: str = "subtitle",
        profile: str = "window",
        generation_tracker: GenerationTracker | None = None,
    ):
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

        # 1. 世代追踪器
        self._generation_tracker = (
            generation_tracker if generation_tracker is not None else GenerationTracker()
        )

        # 2. 解耦子组件初始化
        self._capture_service = CaptureService(hwnd=self._hwnd, region=self._region)
        self._frame_buffer = LatestFrameBuffer()
        self._frame_detector = FrameChangeDetector()
        self._polling_controller = AdaptivePollingController()
        self._ocr_service = OcrService(ocr_engine=self._ocr)
        self._ocr_stabilizer = OcrStabilizer()
        self._ocr_reset_requested = threading.Event()
        self._text_change_detector = TextChangeDetector(empty_clear_delay_s=self._CLEAR_GRACE_SECONDS)
        self._translation_manager = TranslationManager(translator=self._translator)
        self._result_manager = ResultManager(generation_tracker=self._generation_tracker)

        # 连接 ResultManager 内部信号至 WindowWatcher 公共信号（支持元数据多重重载透传）
        try:
            self._result_manager.subtitle_ready[str, int, int].connect(
                lambda t, g, r: self.subtitle_ready[str, int, int].emit(t, g, r)
            )
        except Exception:
            pass
        self._result_manager.subtitle_ready.connect(self.subtitle_ready.emit)

        try:
            self._result_manager.annotations_ready[list, int, int].connect(
                lambda items, g, r: self.annotations_ready[list, int, int].emit(items, g, r)
            )
        except Exception:
            pass
        self._result_manager.annotations_ready.connect(self.annotations_ready.emit)

        try:
            self._result_manager.history_ready[str, str, str, int, int].connect(
                lambda s, t, m, g, r: self.history_ready[str, str, str, int, int].emit(s, t, m, g, r)
            )
        except Exception:
            pass
        self._result_manager.history_ready.connect(self.history_ready.emit)
        self._result_manager.window_moved.connect(self.window_moved.emit)
        self._result_manager.content_cleared.connect(self.content_cleared.emit)
        self._result_manager.stopped.connect(self.stopped.emit)

        self._running = True
        self._paused = threading.Event()
        self._inference_thread: threading.Thread | None = None
        self._consumer_stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._last_skip_target: bool | None = None
        self._last_frame: np.ndarray | None = None
        self._skipped_frames = 0
        self._work_revision: int = 0
        self._content_revision: int = 0
        self._content_epoch: int = 0
        self._content_epoch_lock = threading.Lock()
        self._consecutive_grab_fails: int = 0
        self._last_captured_frame: np.ndarray | None = None
        self._scene_text_state = SceneTextState()
        self._last_processed_work_rev: int = 0
        self._last_processed_epoch: int = 0
        self._translation_session_tag: str = f"watcher_{uuid.uuid4().hex[:8]}"

    # ==========================================
    # 解耦组件只读访问器
    # ==========================================

    @property
    def generation_tracker(self) -> GenerationTracker:
        return self._generation_tracker

    @property
    def frame_buffer(self) -> LatestFrameBuffer:
        return self._frame_buffer

    @property
    def capture_service(self) -> CaptureService:
        return self._capture_service

    @property
    def result_manager(self) -> ResultManager:
        return self._result_manager

    @property
    def translation_manager(self) -> TranslationManager:
        return self._translation_manager

    @property
    def ocr_service(self) -> OcrService:
        return self._ocr_service

    @property
    def ocr_stabilizer(self) -> OcrStabilizer:
        return self._ocr_stabilizer

    @property
    def frame_detector(self) -> FrameChangeDetector:
        return self._frame_detector

    @property
    def polling_controller(self) -> AdaptivePollingController:
        return self._polling_controller

    @property
    def text_change_detector(self) -> TextChangeDetector:
        return self._text_change_detector

    @property
    def scene_text_state(self) -> SceneTextState:
        return self._scene_text_state

    @property
    def work_revision(self) -> int:
        with self._content_epoch_lock:
            return self._work_revision

    @property
    def content_revision(self) -> int:
        with self._content_epoch_lock:
            return self._content_revision

    @property
    def content_epoch(self) -> int:
        with self._content_epoch_lock:
            return self._content_revision

    # ==========================================
    # 兼容既有测试与调用方的属性代理
    # ==========================================

    @property
    def _state(self) -> TextChangeDetector:
        return self._text_change_detector

    @_state.setter
    def _state(self, value: Any) -> None:
        self._text_change_detector = value

    @property
    def _last_rect(self) -> tuple[int, int, int, int] | None:
        return self._capture_service.last_rect

    @_last_rect.setter
    def _last_rect(self, value: tuple[int, int, int, int] | None) -> None:
        with self._capture_service._target_lock:
            self._capture_service._last_rect = value

    @property
    def _last_text(self) -> str:
        with self._state_lock:
            return self._text_change_detector.last_text

    @_last_text.setter
    def _last_text(self, value: str) -> None:
        with self._state_lock:
            self._text_change_detector.last_text = value

    @property
    def _empty_ocr_frames(self) -> int:
        with self._state_lock:
            return self._text_change_detector.empty_frames

    @_empty_ocr_frames.setter
    def _empty_ocr_frames(self, value: int) -> None:
        with self._state_lock:
            self._text_change_detector.empty_frames = value

    @property
    def _line_tr_cache(self) -> dict[str, str]:
        with self._state_lock:
            return self._translation_manager.line_cache

    @_line_tr_cache.setter
    def _line_tr_cache(self, value: dict[str, str]) -> None:
        with self._state_lock:
            self._translation_manager.line_cache = value

    def _observe_empty_ocr_frame(self) -> None:
        """连续两轮无文字才清空，兼顾及时消失和单帧 OCR 抖动。"""
        self._empty_ocr_frames += 1
        if self._empty_ocr_frames < 2 or not self._last_text:
            return
        self._last_text = ""
        self.content_cleared.emit()
        _log.info("连续两轮未识别到文字，已清空持续翻译显示")

    # ==========================================
    # 控制与生命周期
    # ==========================================

    def set_display_mode(self, mode: str) -> None:
        """运行中切换字幕模式与逐行标注模式。"""
        if mode not in ("subtitle", "annotate"):
            return
        self._display_mode = mode
        self._generation_tracker.reset()
        with self._content_epoch_lock:
            self._content_revision += 1
            self._content_epoch = self._content_revision
        self._scene_text_state.clear()
        self._last_captured_frame = None
        self._frame_buffer.clear()
        with self._state_lock:
            self._text_change_detector.reset(clear_cache=True)
        self._translation_manager.clear_cache()
        self._ocr_stabilizer.reset()
        self._ocr_reset_requested.set()
        self._last_skip_target = None
        self._last_frame = None
        self._skipped_frames = 0
        self.set_annotation_mask(None, reset_reference=True)

    def set_region(self, region: tuple[int, int, int, int] | None) -> None:
        """区域监视时拖动选区后更新（线程安全）。"""
        with self._region_lock:
            self._region = region
        self._capture_service.set_region(region)
        self._generation_tracker.reset()
        with self._content_epoch_lock:
            self._content_revision += 1
            self._content_epoch = self._content_revision
        self._scene_text_state.clear()
        self._last_captured_frame = None
        self._frame_buffer.clear()
        with self._state_lock:
            self._text_change_detector.reset(clear_cache=False)
        self._ocr_stabilizer.reset()
        self._ocr_reset_requested.set()
        self._last_frame = None
        self._skipped_frames = 0
        self._last_processed_epoch = 0
        self.set_annotation_mask(None, reset_reference=True)

    def _rotate_translation_session(self) -> str:
        """轮转翻译会话标签并中断上一代未完成的在途请求。

        彻底解决 Sticky Cancel 与恢复运行/切换语言的冲突：
        旧标签被置为永久取消态（保证旧线程被释放后立刻短路退出，不争抢锁），
        新标签获得独立全新的命名空间，保证后续翻译 100% 正常执行。
        """
        old_tag = self._translation_session_tag
        self._translation_session_tag = f"watcher_{uuid.uuid4().hex[:8]}"
        try:
            if hasattr(self._translator, "abort_inflight"):
                try:
                    self._translator.abort_inflight(tag=old_tag)
                except TypeError:
                    self._translator.abort_inflight()
        except Exception:
            pass
        return self._translation_session_tag

    def on_target_language_changed(self) -> None:
        """目标语言改变时重置检测器与缓存，中断旧语言在途请求，强制当前帧重新翻译。"""
        self._generation_tracker.reset()
        with self._content_epoch_lock:
            self._content_revision += 1
            self._content_epoch = self._content_revision
        self._rotate_translation_session()
        with self._state_lock:
            self._text_change_detector.reset(clear_cache=True)
        self._translation_manager.clear_cache()
        self._ocr_stabilizer.reset()
        self._ocr_reset_requested.set()
        self._last_frame = None
        self._last_captured_frame = None

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
        if reference is None or reference.shape != image.shape:
            with self._annotation_mask_lock:
                self._annotation_clean_frame = image.copy()
            return clean

        if mask is not None:
            clean = image.copy()
            clean[mask] = reference[mask]
        return clean

    def _sync_clean_frame(self, image: np.ndarray) -> None:
        """在更新遮罩前保存干净底图。"""
        with self._annotation_mask_lock:
            self._annotation_clean_frame = image.copy()

    def _check_and_notify_cleared(self) -> None:
        """连续两轮无文字才清空，兼顾及时消失和单帧 OCR 抖动。"""
        with self._state_lock:
            event, _ = self._text_change_detector.observe([], 0.0)
        if event == "clear":
            self._notify_cleared()

    def _notify_cleared(self) -> None:
        self._result_manager.dispatch_cleared()
        _log.info("连续两轮未识别到文字，已清空持续翻译显示")

    def stop(self) -> None:
        """停止监视并清空缓冲与世代。"""
        self._running = False
        self._consumer_stop_event.set()
        self._generation_tracker.reset()
        with self._content_epoch_lock:
            self._content_revision += 1
            self._content_epoch = self._content_revision
        self._frame_buffer.clear()
        self._scene_text_state.clear()
        self._last_captured_frame = None
        self._last_processed_epoch = 0
        self._last_processed_work_rev = 0
        try:
            if hasattr(self._translator, "abort_inflight"):
                try:
                    self._translator.abort_inflight(tag=self._translation_session_tag)
                except TypeError:
                    self._translator.abort_inflight()
        except Exception:
            pass

    def set_paused(self, paused: bool) -> None:
        """暂停或恢复监视。
        暂停时立即作废当前世代与内容版本，丢弃在途翻译，UI 冻结；
        恢复时重置捕获基准，强制拉取最新帧刷新。
        """
        if paused:
            self._paused.set()
            self._generation_tracker.reset()
            with self._content_epoch_lock:
                self._content_revision += 1
                self._content_epoch = self._content_revision
            self._rotate_translation_session()
        else:
            self._last_captured_frame = None
            self._last_frame = None
            self._paused.clear()

    def _is_content_invalidated(self, diff_res: Any, has_visual_change: bool) -> bool:
        """检查变动是否需要作废当前在途内容 (PR 1 / Content Revision).
        
        判定原则：
        1. 若画面没有发生任何变动，不作废；
        2. 若发生场景级突变（changed_ratio >= 0.35 或 尺寸不符），100% 作废；
        3. 若变动 ROI 区域与上一轮 OCR 识别到的任意既有文字框相交，说明字幕/文字发生增删改，100% 作废；
        4. 若此前画面完全无文字，但现在出现有效变动（可能有新字幕出现），100% 作废；
        5. 若画面变化仅局限于无文字的背景/边角（如游戏粒子、背景动画），绝不作废在途翻译，杜绝画面饥饿。
        """
        if not has_visual_change:
            return False
        if getattr(diff_res, "invalidates_content", False):
            return True
        ratio = getattr(diff_res, "changed_ratio", 0.0)
        if ratio >= 0.35:
            return True

        existing_lines = self._scene_text_state.lines
        if not existing_lines:
            return True

        boxes = [getattr(line, "box", None) or line for line in existing_lines]
        boxes = [b for b in boxes if b]
        if hasattr(diff_res, "has_change_in_boxes"):
            step = getattr(self._frame_detector, "step", 4)
            margin_bottom = 24 if self._display_mode == "subtitle" else 0
            expand_ratio = 1.0 if self._display_mode == "subtitle" else 0.0
            return diff_res.has_change_in_boxes(
                boxes,
                step=step,
                min_changed_pixels=3,
                expand_bottom_ratio=expand_ratio,
                margin_bottom_px=margin_bottom,
            )

        roi_box = getattr(diff_res, "roi_box", None)
        if roi_box is not None:
            return any(_boxes_intersect(box, roi_box, margin=6) for box in boxes)

        return True

    def _grab(self) -> tuple[tuple[int, int, int, int] | None, np.ndarray | None]:
        """按当前目标抓取屏幕或窗口。"""
        with self._region_lock:
            region = self._region
        return self._capture_service.grab(self._hwnd, region)

    @staticmethod
    def _line_box_bounds(box: Any) -> tuple[int, int, int, int] | None:
        """将 OCR 多边形或绝对矩形转换为包围框；未知格式回退全图识别。"""
        try:
            if box is None or len(box) != 4:
                return None
            if isinstance(box[0], (list, tuple)):
                xs = [int(point[0]) for point in box]
                ys = [int(point[1]) for point in box]
                return min(xs), min(ys), max(xs), max(ys)
            x1, y1, x2, y2 = (int(value) for value in box)
            return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
        except (TypeError, ValueError, IndexError):
            return None

    def _expand_roi_for_text(
        self,
        roi_box: tuple[int, int, int, int],
        frame: np.ndarray,
        existing_lines: list[Any],
    ) -> tuple[int, int, int, int] | None:
        """将变动区扩到相交旧文本的完整行，避免只识别被修改的几个字。"""
        bounds = []
        for line in existing_lines:
            box = getattr(line, "box", None)
            if box is None and isinstance(line, (tuple, list)) and len(line) > 1:
                box = line[1]
            parsed = self._line_box_bounds(box)
            if parsed is None:
                return None
            bounds.append(parsed)

        h, w = frame.shape[:2]
        expanded = roi_box
        for _ in range(len(bounds) + 1):
            padded = self._ocr_service.compute_padded_roi(expanded, w, h)
            next_box = expanded
            for box in bounds:
                if _boxes_intersect(box, padded):
                    next_box = (
                        min(next_box[0], box[0]),
                        min(next_box[1], box[1]),
                        max(next_box[2], box[2]),
                        max(next_box[3], box[3]),
                    )
            if next_box == expanded:
                return expanded
            expanded = next_box
        return None

    def _recognize_live_lines(
        self,
        frame: np.ndarray,
        roi_box: tuple[int, int, int, int] | None,
        diff_ratio: float,
    ) -> list[Any]:
        """局部变动时识别完整相关行，并与画面其它稳定行合并。"""
        existing = self._scene_text_state.lines
        use_roi = (
            roi_box is not None
            and diff_ratio < 0.35
            and bool(existing)
            and not self._scene_text_state.should_force_full_refresh()
        )
        expanded_roi = self._expand_roi_for_text(roi_box, frame, existing) if use_roi else None
        raw_lines = self._ocr_service.recognize_frame(frame, roi_box=expanded_roi)

        if expanded_roi is not None and not self._ocr_service.last_was_fallback:
            padded_roi = self._ocr_service.last_padded_roi
            if not raw_lines and any(
                _boxes_intersect(getattr(line, "box", None), padded_roi)
                for line in existing
            ):
                # 局部空识别可能只是裁剪或 OCR 抖动，先用整图确认再进入清空状态机。
                raw_lines = self._ocr_service.recognize_frame(frame, force_full=True)
            else:
                merged_lines = self._scene_text_state.preview_roi(raw_lines, padded_roi)
                _, stable_lines = self._ocr_stabilizer.process(merged_lines)
                return self._scene_text_state.commit_roi(stable_lines)

        _, stable_lines = self._ocr_stabilizer.process(raw_lines)
        return self._scene_text_state.update_full(stable_lines)

    def _annotate_translate(self, lines: list[Any], target: str) -> tuple[list[tuple[Any, str]], str]:
        """备注：按行增量翻译，委托 TranslationManager 处理。"""
        skip_key = f"{self._profile}_annotate_skip_target_lang"
        skip_target = bool(self._cfg.get(skip_key))
        return self._translation_manager.translate_annotations(
            lines, target, skip_target=skip_target, is_running_fn=lambda: self._running
        )

    # ==========================================
    # 核心管线编排循环
    # ==========================================

    def run(self) -> None:
        _log.info(
            "监视线程启动 profile=%s mode=%s hwnd=%s region=%s",
            self._profile, self._display_mode, self._hwnd, self._region,
        )
        self._consumer_stop_event.clear()
        self._inference_thread = threading.Thread(
            target=self._inference_loop,
            name=f"WatcherInference-{self._profile}",
            daemon=True,
        )
        self._inference_thread.start()

        try:
            while self._running:
                while self._running and self._paused.is_set():
                    time.sleep(0.05)
                if not self._running:
                    break

                p = self._profile
                cfg_interval = float(self._cfg.get(f"{p}_watch_interval_ms", 120)) / 1000.0

                # 备注「跳过目标语」开关变化响应
                if self._display_mode == "annotate":
                    skip_key = f"{self._profile}_annotate_skip_target_lang"
                    skip_now = bool(self._cfg.get(skip_key))
                    if skip_now != self._last_skip_target:
                        self._last_skip_target = skip_now
                        with self._state_lock:
                            self._text_change_detector.reset(clear_cache=True)
                        self._translation_manager.clear_cache()

                t0 = time.time()

                # 阶段 1: 画面捕获与窗口验证
                try:
                    rect, img = self._grab()
                except Exception as e:
                    if not self._capture_service.is_invalid_window_handle_error(e):
                        raise
                    _log.info("监视目标窗口已关闭 hwnd=%s", self._hwnd)
                    self._result_manager.dispatch_stopped("目标窗口已关闭")
                    return

                if img is None:
                    self._consecutive_grab_fails += 1
                    if self._consecutive_grab_fails >= 10:
                        _log.warning("监视目标持续无法捕获，结束")
                        self._result_manager.dispatch_stopped("监视目标已关闭或无法捕获")
                        return
                    time.sleep(0.05)
                    continue
                self._consecutive_grab_fails = 0

                # 备注浮层像素剔除：在画面差分与缓冲压入前先剔除自身译文浮层，彻底杜绝自反馈与假变动
                if (
                    self._profile == "region"
                    and self._display_mode == "annotate"
                    and bool(self._cfg.get("annotate_capture_visible"))
                ):
                    img = self._remove_annotation_overlay(img)

                # 阶段 2: 目标位移追踪
                if self._capture_service.has_moved(rect):
                    self._result_manager.dispatch_moved(*rect)

                prior_boxes = [
                    getattr(line, "box", None) or line
                    for line in self._scene_text_state.lines
                ]
                try:
                    diff_res = self._frame_detector.detect(
                        self._last_captured_frame, img, prior_text_boxes=prior_boxes
                    )
                    has_visual_change = diff_res.has_changed
                    roi_box = diff_res.roi_box
                    diff_ratio = diff_res.changed_ratio
                except Exception:
                    diff_res = None
                    has_visual_change = True
                    roi_box = None
                    diff_ratio = 1.0

                invalidates_content = (
                    self._last_captured_frame is None
                    or self._is_content_invalidated(diff_res, has_visual_change)
                )

                with self._content_epoch_lock:
                    if has_visual_change:
                        self._work_revision += 1
                    if invalidates_content:
                        self._content_revision += 1
                    current_work_rev = self._work_revision
                    current_content_rev = self._content_revision
                    self._content_epoch = self._content_revision

                if has_visual_change or self._last_captured_frame is None:
                    self._last_captured_frame = img

                packet = FramePacket(
                    frame=img,
                    epoch=current_content_rev,
                    roi_box=roi_box,
                    timestamp=t0,
                    has_changed=has_visual_change,
                    changed_ratio=diff_ratio,
                    work_revision=current_work_rev,
                    content_revision=current_content_rev,
                )

                # 阶段 4: 压入 LatestFrameBuffer（削峰解耦，异步投递至推理消费线程）
                self._frame_buffer.put(packet)

                # 自适应轮询间隔休眠
                dynamic_delay = (
                    self._polling_controller.current_interval
                    if hasattr(self._polling_controller, "current_interval")
                    else 0.12
                )
                effective_interval = (
                    min(cfg_interval, dynamic_delay)
                    if cfg_interval > 0
                    else dynamic_delay
                )

                remaining = max(0.01, effective_interval - (time.time() - t0))
                end = time.time() + remaining
                while self._running and time.time() < end:
                    time.sleep(min(0.02, max(0.001, end - time.time())))

            _log.info("监视捕获循环正常退出")
            self._result_manager.dispatch_stopped("已停止监视")
        except Exception:
            _log.exception("监视捕获线程异常")
            self._result_manager.dispatch_stopped("监视出错：\n" + traceback.format_exc(limit=3))
        finally:
            # 1. 显式置位消费停止事件并清空缓冲队列
            self._consumer_stop_event.set()
            self._frame_buffer.clear()

            # 2. 强力主动打断任何底层阻塞的在途网络请求
            try:
                if hasattr(self._translator, "abort_inflight"):
                    try:
                        self._translator.abort_inflight(tag=self._translation_session_tag)
                    except TypeError:
                        self._translator.abort_inflight()
            except Exception:
                pass

            # 推理线程可能仍在 OCR 中；保持 QThread 存活，避免提前释放共享资源。
            while self._inference_thread is not None and self._inference_thread.is_alive():
                self._inference_thread.join(timeout=2.0)
                if self._inference_thread.is_alive():
                    _log.warning("推理工作线程仍在退出，继续等待并中断在途翻译请求")
                    try:
                        if hasattr(self._translator, "abort_inflight"):
                            self._translator.abort_inflight(tag=self._translation_session_tag)
                    except Exception:
                        pass

            self._capture_service.release()
            try:
                if hasattr(self._translator, "release_session"):
                    self._translator.release_session(self._translation_session_tag)
            except Exception:
                pass

    def _inference_loop(self) -> None:
        """后台推理工作循环: 从 LatestFrameBuffer 消费最新帧并执行 OCR 与翻译 (PR 11 / PR 13)."""
        _log.debug("推理消费线程启动")
        threshold = float(self._cfg.get(f"{self._profile}_watch_diff_threshold", 0.9))

        while not self._consumer_stop_event.is_set():
            try:
                if self._ocr_reset_requested.is_set():
                    self._ocr_reset_requested.clear()
                    self._ocr_stabilizer.reset()

                # 1. 尝试从缓冲中拉取最新帧（带超时，超时后循环检查 stop_event）
                pulled = self._frame_buffer.get(timeout=0.1)
                if pulled is None:
                    if not self._running or self._consumer_stop_event.is_set():
                        break
                    continue

                if isinstance(pulled, FramePacket):
                    img = pulled.frame
                    packet_epoch = pulled.content_revision or pulled.epoch
                    packet_work_rev = pulled.work_revision
                    packet_roi = pulled.roi_box
                    has_visual_change = pulled.has_changed
                    packet_diff_ratio = pulled.changed_ratio
                else:
                    img, packet_epoch = pulled
                    packet_work_rev = packet_epoch
                    packet_roi = None
                    has_visual_change = True
                    packet_diff_ratio = 1.0

                # 2. 开启当前推理世代，确保异步结果与会话状态同步
                frame_gen_id = self._generation_tracker.next_generation()

                # 3. 画面两阶段轻量差分检测（纯净算法，绝不被单像素噪声绕过）
                # 双版本号加固 (PR 1 / Work Revision)：
                # 若 packet_work_rev != self._last_processed_work_rev，说明在推理繁忙期间抓屏捕获到了变动帧！
                # 即使该变动帧在缓冲队列中被后续的静态帧覆盖（has_visual_change=False），但由于 work_revision 是新的，
                # 推理端绝不漏掉变动帧，立即触发处理，彻底杜绝 Latest-Frame-Wins 覆盖漏洞！
                work_changed = (packet_work_rev != self._last_processed_work_rev)
                epoch_changed = (packet_epoch != self._last_processed_epoch)
                if work_changed or epoch_changed:
                    diff_has_changed = True
                    roi_box = packet_roi
                    diff_ratio = packet_diff_ratio if packet_diff_ratio > 0.0 else 1.0
                elif packet_roi is not None or not has_visual_change:
                    diff_has_changed = has_visual_change
                    roi_box = packet_roi
                    diff_ratio = packet_diff_ratio
                else:
                    diff_result = self._frame_detector.detect(self._last_frame, img)
                    diff_has_changed = diff_result.has_changed
                    roi_box = diff_result.roi_box
                    diff_ratio = diff_result.changed_ratio

                # 4. 更新自适应轮询状态机
                self._polling_controller.on_frame(diff_has_changed)

                # 5. 静止画面跳过 OCR 判定（连续不超过 _MAX_SKIPPED_FRAMES 帧）
                # 关键修复：当存在正在等待防抖确认的候选文本（OcrStabilizer 或 TextChangeDetector），或处于清空确认中时，坚决禁止跳过 OCR！
                can_skip_ocr = (
                    not self._ocr_stabilizer.has_pending_candidate
                    and not self._text_change_detector.has_pending_candidate
                )
                if (
                    self._last_frame is not None
                    and self._skipped_frames < self._MAX_SKIPPED_FRAMES
                    and not diff_has_changed
                    and can_skip_ocr
                ):
                    self._skipped_frames += 1
                    if not self._running and self._frame_buffer.is_empty:
                        break
                    continue

                self._skipped_frames = 0
                self._last_frame = img

                # 6. 备注浮层像素剔除
                if (
                    self._profile == "region"
                    and self._display_mode == "annotate"
                    and bool(self._cfg.get("annotate_capture_visible"))
                ):
                    img = self._remove_annotation_overlay(img)

                # 7. 局部 OCR 与全图回退，防抖后再提交场景文本
                lines: list[Any] = []
                try:
                    lines = self._recognize_live_lines(img, roi_box, diff_ratio)
                except Exception as e:
                    _log.warning("OCR 识别异常 (gen=%s): %s", frame_gen_id, e)
                    if not self._running and self._frame_buffer.is_empty:
                        break
                    continue

                # 仅在 OCR 成功执行后，才宣告该 work 与 epoch 已被成功消费，确保异常帧能被下一周期立即重试
                self._last_processed_epoch = packet_epoch
                self._last_processed_work_rev = packet_work_rev

                # 8. 文本变化检测与空帧清空判定
                with self._state_lock:
                    # 字幕及已由 OCR 连续帧确认的微变，直接派发；备注模式的其它小变化仍保留二次确认。
                    is_sub = (self._display_mode == "subtitle")
                    confirmed = self._ocr_stabilizer.last_change_was_debounced is True
                    sim_threshold = 1.0 if is_sub else threshold
                    event, text = self._text_change_detector.observe(
                        lines, sim_threshold, exact_change=is_sub or confirmed
                    )

                # 9. 结果派发与增量翻译，内部异常隔离防护
                if event == "clear":
                    _log.info("连续空 OCR 已确认，清空持续翻译显示")
                    self._scene_text_state.clear()
                    self._result_manager.dispatch_cleared(frame_gen_id)
                elif event == "change":
                    target = str(self._cfg.get("target_language", "zh"))
                    mode_tag = (
                        f"{self._profile}_annotate"
                        if self._display_mode == "annotate"
                        else f"{self._profile}_subtitle"
                    )
                    try:
                        if self._display_mode == "annotate":
                            skip_target = bool(self._cfg.get(f"{self._profile}_annotate_skip_target_lang"))
                            items, translation = self._translation_manager.translate_annotations(
                                lines,
                                target,
                                frame_gen_id,
                                skip_target=skip_target,
                                is_running_fn=lambda: self._running and not self._consumer_stop_event.is_set(),
                                session_tag=self._translation_session_tag,
                            )
                            if self._running and not self._consumer_stop_event.is_set():
                                # Latest-Content-Wins 校验：翻译耗时期间画面若有实质新变化，丢弃旧译文防闪烁
                                with self._content_epoch_lock:
                                    is_fresh = (packet_epoch == self._content_epoch)
                                if not is_fresh:
                                    _log.debug("画面内容已变动，丢弃过时逐行备注 (epoch=%s vs %s)", packet_epoch, self._content_epoch)
                                    with self._state_lock:
                                        self._text_change_detector.rollback(text)
                                    continue

                                dispatched = self._result_manager.dispatch_annotations(items, frame_gen_id, content_rev=packet_epoch)
                                if dispatched:
                                    if translation:
                                        self._result_manager.dispatch_history(
                                            text, translation, mode_tag, frame_gen_id, content_rev=packet_epoch
                                        )
                                else:
                                    with self._state_lock:
                                        self._text_change_detector.rollback(text)
                        else:
                            if not self._running or self._consumer_stop_event.is_set():
                                break
                            translation = self._translation_manager.translate_subtitle(
                                text,
                                target,
                                frame_gen_id,
                                session_tag=self._translation_session_tag,
                            )
                            if self._running and not self._consumer_stop_event.is_set():
                                # Latest-Content-Wins 校验：翻译耗时期间画面若有实质新变化，丢弃旧译文防闪烁
                                with self._content_epoch_lock:
                                    is_fresh = (packet_epoch == self._content_epoch)
                                if not is_fresh:
                                    _log.debug("画面内容已变动，丢弃过时字幕译文 (epoch=%s vs %s)", packet_epoch, self._content_epoch)
                                    with self._state_lock:
                                        self._text_change_detector.rollback(text)
                                    continue

                                dispatched = self._result_manager.dispatch_subtitle(translation, frame_gen_id, content_rev=packet_epoch)
                                if dispatched:
                                    if translation:
                                        self._result_manager.dispatch_history(
                                            text, translation, mode_tag, frame_gen_id, content_rev=packet_epoch
                                        )
                                else:
                                    with self._state_lock:
                                        self._text_change_detector.rollback(text)
                    except Exception as e:
                        _log.warning("翻译处理异常 (gen=%s): %s", frame_gen_id, e)
                        with self._state_lock:
                            self._text_change_detector.rollback(text)
                        self._ocr_stabilizer.reset()
                        if not self._running and self._frame_buffer.is_empty:
                            break
                        continue

                # 10. 检查退出条件（用于同步单测：捕获循环已结束且缓冲已排空）
                if not self._running and self._frame_buffer.is_empty:
                    break
            except Exception:
                _log.exception("推理消费工作线程异常拦截，安全恢复")
                time.sleep(0.02)
                if not self._running and self._frame_buffer.is_empty:
                    break

        _log.debug("推理消费线程退出")
