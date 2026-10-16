# -*- coding: utf-8 -*-
"""持续翻译监视管线编排器 (PR 9 & PR 11).

解耦为单职责组件协同工作：
- CaptureService: 窗口/区域截屏与位移检测
- LatestFrameBuffer: 最新帧覆盖单槽缓冲（削峰防滞后）
- FrameChangeDetector: 两阶段轻量画面差分与噪点过滤
- AdaptivePollingController: 动态自适应轮询状态机
- OcrService: ROI 局部识别与全图 OCR 回退兜底
- TextChangeDetector: OCR 文本变化判定与空帧清空状态机
- TranslationRouter: 逐行调用本地模型翻译（翻完一行显示一行）
- ResultManager: 世代校验与信号派发（防乱序覆盖）
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
import traceback
import uuid
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from .adaptive_polling import AdaptivePollingController
from .applog import get_logger
from .capture_service import CaptureService
from .frame_detector import FrameChangeDetector
from .latest_frame_buffer import LatestFrameBuffer, FramePacket
from .ocr_engine import OcrEngine
from .ocr_service import OcrService
from .ocr_stabilizer import OcrStabilizer
from .pipelines import GenerationTracker
from .result_manager import ResultManager
from .replacement import (
    ReplacementBlock,
    ReplacementPayload,
    build_payload,
    group_blocks,
    join_block_text,
)
from .scene_text_state import SceneTextState, _boxes_intersect
from .text_change_detector import TextChangeDetector
from .translation_runtime.router import TranslationRouter

_log = get_logger("watch")

class WindowWatcher(QThread):
    """持续翻译监视管线编排线程 (PR 9 & PR 11).

    监视源二选一：hwnd（窗口，含被遮挡部分）或 region（屏幕区域 x,y,w,h）。

    保留全部既有公共信号：
    - subtitle_ready(translation)                译文更新（字幕条模式）
    - replacement_ready(payload)                 原位替换的段落、底色与译文
    - history_ready(source, translation, mode)   有实质新译文时写入历史
    - window_moved(x, y, w, h)                   目标位置变化
    - content_cleared()                          连续无文字时清空
    - stopped(reason)                            监视结束
    """

    subtitle_ready = Signal((str,), (str, int))
    replacement_ready = Signal((object,), (object, int))
    history_ready = Signal((str, str, str), (str, str, str, int))
    window_moved = Signal(int, int, int, int)
    content_cleared = Signal()
    stopped = Signal(str)

    # 画面无实质变化时最多连续跳过的 OCR 轮数（保险起见周期性强制识别）
    _MAX_SKIPPED_FRAMES = 5
    _CLEAR_GRACE_SECONDS = 1.2

    def __init__(
        self,
        ocr: OcrEngine,
        translator: TranslationRouter,
        cfg: dict,
        hwnd: int | None = None,
        region: tuple[int, int, int, int] | None = None,
        display_mode: str = "replace",
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
        self._ocr = ocr
        self._translator = translator
        self._cfg = cfg
        self._display_mode = display_mode
        self._profile = profile
        # 原位替换最近一次派发时各行的 (文字, 位置)；没变就不重新取色排版
        self._replacement_layout_key: tuple | None = None

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
        self._text_change_detector = TextChangeDetector(empty_clear_delay_s=self._CLEAR_GRACE_SECONDS)
        self._result_manager = ResultManager(generation_tracker=self._generation_tracker, parent=self)

        # 把 ResultManager 的信号（含带世代号的重载）转发为 WindowWatcher 的公共信号
        rm = self._result_manager
        rm.subtitle_ready[str, int].connect(lambda t, g: self.subtitle_ready[str, int].emit(t, g))
        rm.subtitle_ready.connect(self.subtitle_ready.emit)
        rm.replacement_ready[object, int].connect(
            lambda payload, g: self.replacement_ready[object, int].emit(payload, g)
        )
        rm.replacement_ready.connect(self.replacement_ready.emit)
        rm.history_ready[str, str, str, int].connect(
            lambda s, t, m, g: self.history_ready[str, str, str, int].emit(s, t, m, g)
        )
        rm.history_ready.connect(self.history_ready.emit)
        self._result_manager.window_moved.connect(self.window_moved.emit)
        self._result_manager.content_cleared.connect(self.content_cleared.emit)
        self._result_manager.stopped.connect(self.stopped.emit)

        self._running = True
        self._paused = threading.Event()
        self._inference_thread: threading.Thread | None = None
        self._consumer_stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._last_frame: np.ndarray | None = None
        self._skipped_frames = 0
        self._work_revision: int = 0
        self._content_revision: int = 0
        # 最近一次重置（停止/暂停/移动选区/切换语言或显示模式）时的内容版本；更早截取的帧一律丢弃
        self._reset_revision: int = 0
        self._revision_lock = threading.Lock()
        self._consecutive_grab_fails: int = 0
        self._last_captured_frame: np.ndarray | None = None
        self._scene_text_state = SceneTextState()
        self._last_processed_work_rev: int = 0
        self._last_processed_epoch: int = 0
        self._translation_session_tag: str = f"watcher_{uuid.uuid4().hex[:8]}"
        self._last_translate_error: str = ""
        # 本次监视已翻译过的行（只在内存中，不受“保存本地翻译缓存”开关影响，不落盘）
        self._line_memory: OrderedDict[tuple[str, str], str] = OrderedDict()

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
        with self._revision_lock:
            return self._work_revision

    @property
    def content_revision(self) -> int:
        with self._revision_lock:
            return self._content_revision

    # ==========================================
    # 控制与生命周期
    # ==========================================

    def set_display_mode(self, mode: str) -> None:
        """运行中切换字幕条与原位替换。"""
        if mode not in ("subtitle", "replace"):
            return
        self._display_mode = mode
        self._reset_revisions()
        self._scene_text_state.clear()
        self._last_captured_frame = None
        self._frame_buffer.clear()
        with self._state_lock:
            self._text_change_detector.reset(clear_cache=True)
        self._ocr_stabilizer.reset()
        self._last_frame = None
        self._skipped_frames = 0

    def set_region(self, region: tuple[int, int, int, int] | None) -> None:
        """区域监视时拖动选区后更新（线程安全）。"""
        with self._region_lock:
            self._region = region
        self._capture_service.set_region(region)
        self._reset_revisions()
        self._scene_text_state.clear()
        self._last_captured_frame = None
        self._frame_buffer.clear()
        with self._state_lock:
            self._text_change_detector.reset(clear_cache=False)
        self._ocr_stabilizer.reset()
        self._last_frame = None
        self._skipped_frames = 0
        self._last_processed_epoch = 0

    def _rotate_translation_session(self) -> str:
        """轮转翻译会话标签并中断上一代未完成的在途请求。

        彻底解决 Sticky Cancel 与恢复运行/切换语言的冲突：
        在途批次持有旧取消令牌，剩余行随该批次一起退出；
        新标签用于后续帧，避免恢复运行时复用旧批次。
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
        self._reset_revisions()
        self._rotate_translation_session()
        with self._state_lock:
            self._text_change_detector.reset(clear_cache=True)
        self._line_memory.clear()
        self._ocr_stabilizer.reset()
        self._last_frame = None
        self._last_captured_frame = None

    def stop(self) -> None:
        """停止监视并清空缓冲与世代。"""
        self._running = False
        self._consumer_stop_event.set()
        self._reset_revisions()
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
            self._reset_revisions()
            self._rotate_translation_session()
        else:
            self._last_captured_frame = None
            self._last_frame = None
            self._paused.clear()

    _LINE_MEMORY_LIMIT = 500

    def _remember(self, source: str, target: str, translation: str) -> None:
        key = (source, target)
        self._line_memory[key] = translation
        self._line_memory.move_to_end(key)
        while len(self._line_memory) > self._LINE_MEMORY_LIMIT:
            self._line_memory.popitem(last=False)

    def _cached_translation(self, source: str, target: str) -> str | None:
        """先查本次监视的内存记录，再查翻译缓存；都未命中时返回 None。"""
        remembered = self._line_memory.get((source, target))
        if remembered is not None:
            self._line_memory.move_to_end((source, target))
            return remembered
        lookup = getattr(self._translator, "cached", None)
        if lookup is None:
            return None
        try:
            value = lookup(source, target)
        except Exception:
            return None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _translate_and_show(
        self,
        lines: list[Any],
        text: str,
        gen_id: int,
        frame: np.ndarray | None = None,
    ) -> None:
        """增量显示一批文字。

        先一次性显示缓存里已有译文的部分，再只把未命中的交给模型，翻完一条补上一条；
        尚未翻完的暂不显示，空译文或出错的显示原文。
        原位替换以段落为单位翻译（需要 frame 取背景色），字幕条逐行翻译。
        """
        target = str(self._cfg.get("target_language", "zh"))
        replace = self._display_mode == "replace" and frame is not None
        session_tag = self._translation_session_tag
        if replace:
            units = [(block, join_block_text(line.text for line in block))
                     for block in group_blocks(lines, frame)]
        else:
            units = [([line], line.text.strip()) for line in lines if line.text.strip()]
        results: list[str | None] = [self._cached_translation(src, target) for _, src in units]
        # 每段的取色与修补只做一次，后续增量显示直接复用
        prepared: dict[int, ReplacementBlock] = {}

        def show() -> bool:
            if replace:
                for index, ((block, _), tr) in enumerate(zip(units, results)):
                    if tr is not None and index not in prepared:
                        built = build_payload(frame, [(block, tr)]).blocks
                        if built:
                            prepared[index] = built[0]
                payload = ReplacementPayload(
                    (0, 0),
                    (int(frame.shape[1]), int(frame.shape[0])),
                    tuple(prepared[index] for index in sorted(prepared)),
                )
                return self._result_manager.dispatch_replacement(payload, gen_id)
            return self._result_manager.dispatch_subtitle(
                "\n".join(tr for tr in results if tr is not None), gen_id
            )

        if replace:
            self._replacement_layout_key = self._layout_key(lines)
            # 文字已变：即使还没有任何译文也立即派发，让界面先撤下上一屏的旧译文
            completed = show()
        else:
            completed = not any(tr is not None for tr in results) or show()
        failed = False
        for index, (_, source) in enumerate(units):
            if not completed:
                break
            if results[index] is not None:
                continue
            if not self._batch_is_current(gen_id, session_tag):
                completed = False
                break
            line_failed = False
            try:
                translated = str(self._translator.translate(
                    source, target, session_tag=session_tag
                ) or "").strip()
            except Exception as e:
                # 出错（如模型尚未加载完）可能是暂时的，整批结束后交给下一帧重试
                failed = line_failed = True
                if str(e) != self._last_translate_error:
                    self._last_translate_error = str(e)
                    _log.warning("单行翻译失败，暂显示原文 (gen=%s): %s", gen_id, e)
                translated = ""
            # 暂停、切换语言、停止或画面已变：丢弃本批剩余行
            if not self._batch_is_current(gen_id, session_tag):
                completed = False
                break
            results[index] = translated or source
            if not line_failed:
                # 模型对同一原文输出确定，空译文（显示原文）也记住，避免反复调用
                self._remember(source, target, results[index])
            completed = show()

        if completed and not failed:
            self._last_translate_error = ""
            # 全是原文时不记历史
            if any(tr != src for (_, src), tr in zip(units, results)):
                mode_tag = f"{self._profile}_{self._display_mode}"
                self._result_manager.dispatch_history(
                    text, "\n".join(tr for tr in results if tr is not None), mode_tag,
                    gen_id,
                )
        else:
            # 被取消或出错的批次让下一帧重新处理
            with self._state_lock:
                self._text_change_detector.rollback(text)
            self._ocr_stabilizer.reset()

    @staticmethod
    def _layout_key(lines: list[Any]) -> tuple:
        return tuple((line.text, tuple(line.box)) for line in lines)

    def _refresh_cached_replacement(
        self, lines: list[Any], frame: np.ndarray, gen_id: int
    ) -> None:
        """文字没变但位置变了（滚动、重排）：用缓存译文按新位置重新取色排版，不调用模型。

        只有背景在动（如视频）而文字位置没变时不重复计算。
        """
        key = self._layout_key(lines)
        if key == self._replacement_layout_key:
            return
        target = str(self._cfg.get("target_language", "zh"))
        ready = []
        for block in group_blocks(lines, frame):
            translated = self._cached_translation(
                join_block_text(line.text for line in block), target
            )
            if translated is None:
                return  # 有段落未翻译过，交给下一次文字变化处理
            ready.append((block, translated))
        if self._result_manager.dispatch_replacement(build_payload(frame, ready), gen_id):
            self._replacement_layout_key = key

    def _reset_revisions(self) -> None:
        """作废所有在途世代与已截取的帧（停止、暂停、移动选区、切换语言或显示模式时调用）。"""
        self._generation_tracker.reset()
        self._replacement_layout_key = None
        with self._revision_lock:
            self._content_revision += 1
            self._reset_revision = self._content_revision

    def _batch_is_current(self, gen_id: int, session_tag: str) -> bool:
        """本批翻译是否仍有效：未停止、会话未轮换、世代未前进。

        只按世代判断（文字真正变化、清空、暂停、移动选区或切换语言时才前进），
        不按像素变化判断：视频字幕等背景持续变化的场景不会打断翻译。
        """
        if not self._running or self._consumer_stop_event.is_set():
            return False
        if session_tag != self._translation_session_tag:
            return False
        return self._generation_tracker.is_active(gen_id)

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
                # 用户设置的间隔作为长时间静止时的轮询间隔（可调大到 800ms 以上）
                if cfg_interval > 0:
                    self._polling_controller.deep_idle_interval = cfg_interval

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

                with self._revision_lock:
                    if has_visual_change:
                        self._work_revision += 1
                    if invalidates_content:
                        self._content_revision += 1
                    current_work_rev = self._work_revision
                    current_content_rev = self._content_revision

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
                dynamic_delay = self._polling_controller.current_interval
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
                # 1. 尝试从缓冲中拉取最新帧（带超时，超时后循环检查 stop_event）
                pulled = self._frame_buffer.get(timeout=0.1)
                if pulled is None:
                    if not self._running or self._consumer_stop_event.is_set():
                        break
                    continue

                img = pulled.frame
                packet_epoch = pulled.content_revision or pulled.epoch
                with self._revision_lock:
                    captured_before_reset = packet_epoch < self._reset_revision
                if captured_before_reset:
                    # 重置前截取的旧帧（如拖动选区前的位置），不识别也不翻译
                    continue
                packet_work_rev = pulled.work_revision
                packet_roi = pulled.roi_box
                has_visual_change = pulled.has_changed
                packet_diff_ratio = pulled.changed_ratio

                # 2. 记录当前世代；静止帧和无文本变化的 OCR 不应使排队中的译文过期。
                frame_gen_id = self._generation_tracker.current_generation

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
                    # 字幕及已由 OCR 连续帧确认的微变，直接派发；原位替换的其它小变化仍保留二次确认。
                    is_sub = (self._display_mode == "subtitle")
                    confirmed = self._ocr_stabilizer.last_change_was_debounced is True
                    sim_threshold = 1.0 if is_sub else threshold
                    event, text = self._text_change_detector.observe(
                        lines, sim_threshold, exact_change=is_sub or confirmed
                    )

                # 只有真实文本变化或清空才开启新世代；期间发生过重置（世代已作废）则丢弃本帧。
                if event in ("change", "clear"):
                    next_gen_id = self._generation_tracker.next_generation_if_active(frame_gen_id)
                    if next_gen_id is None:
                        with self._state_lock:
                            self._text_change_detector.rollback(text)
                        continue
                    frame_gen_id = next_gen_id

                # 9. 结果派发与增量翻译，内部异常隔离防护
                if event == "clear":
                    _log.info("连续空 OCR 已确认，清空持续翻译显示")
                    self._scene_text_state.clear()
                    self._result_manager.dispatch_cleared(frame_gen_id)
                elif event == "change":
                    self._translate_and_show(lines, text, frame_gen_id, img)
                elif (
                    self._display_mode == "replace"
                    and lines
                    and diff_has_changed
                    and not self._ocr_stabilizer.has_pending_candidate
                    and not self._text_change_detector.has_pending_candidate
                    and "\n".join(line.text for line in lines).strip() == text
                ):
                    self._refresh_cached_replacement(lines, img, frame_gen_id)

                # 10. 检查退出条件（用于同步单测：捕获循环已结束且缓冲已排空）
                if not self._running and self._frame_buffer.is_empty:
                    break
            except Exception:
                _log.exception("推理消费工作线程异常拦截，安全恢复")
                time.sleep(0.02)
                if not self._running and self._frame_buffer.is_empty:
                    break

        _log.debug("推理消费线程退出")
