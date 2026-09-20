# -*- coding: utf-8 -*-
"""主程序：托盘常驻，热键调度各功能。"""

import sys
import threading
import time

from PySide6.QtCore import QEventLoop, QMimeData, QObject, QSharedMemory, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QSystemTrayIcon,
)

from . import capture, config
from . import __version__
from .applog import LOG_PATH, get_logger, setup_logging
from .hotkeys import HotkeyManager
from .i18n import get_language, set_language, t as _t
from .runtime_resources import RuntimeResources
from .paths import ICON_ICO
from .ui.overlays import (
    AnnotateCtrl,
    AnnotationOverlay,
    RegionWatchFrame,
    SubtitleBar,
)
from .ui.region_selector import RegionSelector
from .ui.window_picker import WindowPicker
from .ui.windows import (
    HistoryWindow,
    HotkeyEdit,
    InputTranslateWindow,
    SettingsWindow,
)
from .pipelines import GenerationTracker
from .window_watcher import WindowWatcher
from .workers import OcrTranslateWorker

# 单实例：QSharedMemory 键
_INSTANCE_KEY = "ScreenTranslator_SingleInstance_v1"
_SHUTDOWN_ABORT_SECONDS = 5.0
_SHUTDOWN_HARD_LIMIT_SECONDS = 30.0


class _PreloadSignals(QObject):
    """把普通 Python 线程的预热结果安全送回 Qt 主线程。"""

    status = Signal(str, str, str)


def _clone_mime_data(source) -> QMimeData:
    """物化剪贴板全部 MIME 格式，之后可原样恢复。"""
    clone = QMimeData()
    if source is None:
        return clone
    for fmt in source.formats():
        clone.setData(fmt, source.data(fmt))
    return clone


def _tray_icon() -> QIcon:
    """优先用打包的 icon.ico，缺失时回退纯色方块。"""
    ico = ICON_ICO
    if ico.exists():
        return QIcon(str(ico))
    pm = QPixmap(32, 32)
    pm.fill(QColor(32, 122, 244))
    return QIcon(pm)


def _hotkey_text(pynput_str: str) -> str:
    """配置里的 pynput 串 → 菜单可读文案（Alt+Q）。"""
    return HotkeyEdit._display(pynput_str)


class App:
    def __init__(self):
        setup_logging()
        self.log = get_logger("app")
        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)
        capture.configure_qt_screens(self.qapp.screens())

        self.cfg = config.load()
        set_language(self.cfg.get("ui_language", "zh"))
        self._refresh_application_name()
        self.log.info(
            "配置已加载 target=%s max_tokens=%s port=%s model=%s ui=%s",
            self.cfg.get("target_language"),
            self.cfg.get("max_tokens"),
            self.cfg.get("server_port"),
            self.cfg.get("model_path"),
            get_language(),
        )
        self.resources = RuntimeResources.create(self.cfg)
        self.storage = self.resources.storage
        self.server = self.resources.server
        self.translator = self.resources.translator
        self.ocr = self.resources.ocr
        self.generation_tracker = GenerationTracker()

        # UI 组件（区域/窗口持续翻译：字幕条 + 备注；区域另有可拖/固定识别框）
        self.selector = RegionSelector()
        self.subtitle = SubtitleBar()
        self.annotation = AnnotationOverlay()
        self.annotation.set_text_color(
            self.cfg.get("annotate_text_color", "#00F0FF")
        )
        self.annotation.set_capture_visible(
            bool(self.cfg.get("annotate_capture_visible"))
        )
        self.annotate_ctrl = AnnotateCtrl()
        self.region_frame = RegionWatchFrame()
        self._region_topmost_timer = QTimer()
        self._region_topmost_timer.setInterval(500)
        self._region_topmost_timer.timeout.connect(self._restack_watch_layer)
        # 窗口翻译高频跟随：只查目标位置（轻量 Win32 调用），拖动时浮层
        # 平滑贴合；OCR 轮询周期（几百 ms）只管内容，不管位置。
        self._window_follow_timer = QTimer()
        self._window_follow_timer.setInterval(33)
        self._window_follow_timer.timeout.connect(self._follow_target_window)
        self.subtitle.mode_changed.connect(self._on_subtitle_mode_changed)
        self.subtitle.stop_requested.connect(
            lambda: self._stop_continuous_translate("已从字幕条关闭")
        )
        self.subtitle.switch_to_annotate.connect(
            lambda: self._switch_watch_display(True)
        )
        self.annotate_ctrl.stop_requested.connect(
            lambda: self._stop_continuous_translate("已从备注条关闭")
        )
        self.annotate_ctrl.switch_to_subtitle.connect(
            lambda: self._switch_watch_display(False)
        )
        self.annotate_ctrl.skip_target_changed.connect(self._on_annotate_skip_target)
        self.subtitle.pause_changed.connect(self._set_watch_paused)
        self.annotate_ctrl.pause_changed.connect(self._set_watch_paused)
        self.region_frame.region_moved.connect(self._on_region_frame_moved)
        # 当前会话是否备注模式（与 cfg 同步，便于运行中切换）
        self._watch_annotate: bool | None = None
        self._watch_paused = False

        # 热键先于设置窗：录入热键时需 pause 全局监听
        self.hotkeys = HotkeyManager(self.cfg)

        self._preload_status = {"llama": "pending", "ocr": "pending"}
        self._preload_errors = {"llama": "", "ocr": ""}

        # 功能窗口（按需显示）
        self.settings_win = SettingsWindow(
            self.cfg,
            on_saved=self._on_settings_saved,
            hotkey_manager=self.hotkeys,
            on_retry_runtime=self._retry_runtime,
        )
        self.translate_win = InputTranslateWindow(
            self.translator, self.cfg, ensure_server=self._ensure_server
        )
        self.history_win = HistoryWindow(
            self.storage,
            on_open=lambda s, t: self.translate_win.show_result(s, t),
        )
        from .ui.topmost import restore_window_geometry

        self.settings_win._remembered_geometry = restore_window_geometry(
            self.settings_win, self.cfg.get("settings_window_geometry")
        )
        self.history_win._remembered_geometry = restore_window_geometry(
            self.history_win, self.cfg.get("history_window_geometry")
        )
        self.translate_win._remembered_geometry = restore_window_geometry(
            self.translate_win, self.cfg.get("translate_window_geometry")
        )
        self._subtitle_geometry_valid = restore_window_geometry(
            self.subtitle, self.cfg.get("subtitle_geometry")
        )
        if self._subtitle_geometry_valid:
            self.subtitle._user_size = (self.subtitle.width(), self.subtitle.height())

        self._workers: list[OcrTranslateWorker] = []  # 防止线程被垃圾回收
        self._retired_watchers: list[WindowWatcher] = []
        self._preload_threads: list[threading.Thread] = []
        self._preload_signals = _PreloadSignals()
        self._preload_signals.status.connect(self._on_preload_status)
        self._quitting = False
        self._shutdown_server_thread: threading.Thread | None = None
        self._shutdown_abort_thread: threading.Thread | None = None
        self._shutdown_started_at: float | None = None
        self._shutdown_resources_closed = False
        self._word_copy_busy = False
        self._word_old_mime: QMimeData | None = None
        # 划词多阶段复制会话状态（_word_translate 每次重置）
        self._word_marker = ""
        self._word_copy_phase = 0
        self._word_copy_kinds: tuple[str, ...] = ()
        self._watcher: WindowWatcher | None = None
        self._watch_hwnd: int | None = None
        self._watch_region: tuple[int, int, int, int] | None = None  # 区域监视时的选区
        self._watch_rect: tuple[int, int, int, int] | None = None  # 当前监视几何
        self._watch_profile: str | None = None  # "window" | "region"
        self._pending_mode = None  # 当前框选动作的用途（None=非主流程发起）
        self._window_picker: WindowPicker | None = None  # 选窗对话框（互斥用）
        self._stopping_watch = False  # 停线程时 processEvents 防重入

        self.selector.region_selected.connect(self._on_region)
        self.selector.cancelled.connect(self._on_region_cancelled)

        # 热键信号（实例在设置窗之前已创建）
        self.hotkeys.screenshot_triggered.connect(lambda: self._start_select("screenshot"))
        self.hotkeys.word_triggered.connect(self._word_translate)
        self.hotkeys.window_triggered.connect(self._toggle_window_watch)
        self.hotkeys.region_watch_triggered.connect(self._toggle_region_watch)

        self._build_tray()
        # 预创建框选遮罩原生窗口，减轻首次截屏翻译整屏闪一下
        self.selector.prepare()
        # 托盘建好后立刻后台预热：翻译模型 + OCR（二者并行）
        self._preload_models()

    # ---------- 初始化 ----------
    def _build_tray(self):
        self.tray = QSystemTrayIcon(_tray_icon())
        menu = QMenu()
        # (action, cfg_key, i18n_title_key)
        self._tray_hotkey_items: list[tuple[QAction, str, str]] = []
        for cfg_key, title_key, slot in [
            ("hotkey_screenshot", "hk_shot", lambda: self._start_select("screenshot")),
            ("hotkey_word", "hk_word", self._word_translate),
            ("hotkey_window", "hk_win_full", self._toggle_window_watch),
            ("hotkey_region_watch", "hk_region_full", self._toggle_region_watch),
        ]:
            act = QAction(menu)
            act.triggered.connect(slot)
            menu.addAction(act)
            self._tray_hotkey_items.append((act, cfg_key, title_key))
        menu.addSeparator()
        self._act_history = QAction(menu)
        self._act_history.triggered.connect(
            lambda: self._show_window(self.history_win)
        )
        menu.addAction(self._act_history)
        self._act_settings = QAction(menu)
        self._act_settings.triggered.connect(
            lambda: self._show_window(self.settings_win)
        )
        menu.addAction(self._act_settings)
        self._act_open_log = QAction(menu)
        self._act_open_log.triggered.connect(self._open_log)
        menu.addAction(self._act_open_log)
        menu.addSeparator()
        self._act_quit = QAction(menu)
        self._act_quit.triggered.connect(self._quit)
        menu.addAction(self._act_quit)
        menu.aboutToShow.connect(self._refresh_tray_hotkeys)
        self.tray.setContextMenu(menu)
        self._refresh_tray_hotkeys()
        self.tray.show()

    def _tray_hotkey_label(self, cfg_key: str, title_key: str) -> str:
        return f"{_t(title_key)}（{_hotkey_text(self.cfg[cfg_key])}）"

    def _refresh_tray_hotkeys(self):
        """用当前配置与界面语言更新托盘菜单。"""
        for act, cfg_key, title_key in self._tray_hotkey_items:
            act.setText(self._tray_hotkey_label(cfg_key, title_key))
        self._act_history.setText(_t("tray_history"))
        self._act_settings.setText(_t("tray_settings"))
        self._act_open_log.setText(_t("tray_open_log"))
        self._act_quit.setText(_t("tray_quit"))
        self.tray.setToolTip(
            _t(
                "tray_tip",
                shot=_hotkey_text(self.cfg["hotkey_screenshot"]),
                word=_hotkey_text(self.cfg["hotkey_word"]),
                win=_hotkey_text(self.cfg["hotkey_window"]),
                reg=_hotkey_text(self.cfg["hotkey_region_watch"]),
            )
        )

    def apply_ui_language(self):
        """设置保存后：刷新托盘与各窗口/浮层文案。"""
        set_language(self.cfg.get("ui_language", "zh"))
        self._refresh_application_name()
        self._refresh_tray_hotkeys()
        try:
            self.settings_win._lang = get_language()
            self.settings_win._apply_i18n()
        except Exception:
            pass
        for w in (
            self.history_win,
            self.translate_win,
            self.subtitle,
            self.annotate_ctrl,
            self.region_frame,
        ):
            try:
                w.apply_ui_language()
            except Exception:
                pass

    def _refresh_application_name(self):
        name = _t("app_name")
        self.qapp.setApplicationName(name)
        self.qapp.setApplicationDisplayName(name)

    def _ensure_server(self) -> bool:
        """只做快速就绪检查；启动/重试始终在后台，绝不阻塞 UI。"""
        if self._quitting:
            return False
        if self.server.is_healthy(timeout=0.2):
            return True
        state = self._preload_status.get("llama", "pending")
        if state == "fail" and not self._has_live_preload("llama-preload"):
            self.log.info("翻译服务上次启动失败，后台重试")
            self._preload_status["llama"] = "pending"
            self._start_preload_thread("llama-preload", self._load_llama)
        else:
            self.log.info("翻译服务尚未就绪 state=%s", state)
        self.tray.showMessage(_t("app_name"), _t("msg_wait_model"))
        return False

    def _has_live_preload(self, name: str) -> bool:
        self._preload_threads = [t for t in self._preload_threads if t.is_alive()]
        return any(t.name == name for t in self._preload_threads)

    def _start_preload_thread(self, name: str, target) -> None:
        if self._quitting or self._has_live_preload(name):
            return
        thread = threading.Thread(target=target, daemon=True, name=name)
        self._preload_threads.append(thread)
        thread.start()

    def _on_preload_status(self, key: str, value: str, err: str):
        """Qt 主线程槽：更新状态和托盘，避免后台线程直接碰 UI。"""
        self._preload_status[key] = value
        self._preload_errors[key] = err
        self._sync_runtime_status()
        if value == "fail" and err:
            name = _t("msg_name_llama") if key == "llama" else _t("msg_name_ocr")
            self.log.error("预热失败 %s: %s", key, err)
            if not self._quitting:
                self.tray.showMessage(
                    _t("msg_preload_fail", name=name),
                    err[:200],
                    QSystemTrayIcon.MessageIcon.Warning,
                )
            return
        self.log.info("预热进度 %s=%s", key, value)
        if all(self._preload_status.get(k) == "ok" for k in ("llama", "ocr")):
            self.log.info("预热完成：OCR 与翻译模型均已就绪")

    def _sync_runtime_status(self):
        state = {}
        for key in ("llama", "ocr"):
            status = self._preload_status.get(key, "pending")
            detail = self._preload_errors.get(key, "")
            if status == "ok":
                if key == "ocr":
                    detail = self.ocr.provider or "ONNX Runtime"
                else:
                    detail = self.server.actual_device
            state[key] = (status, detail)
        self.settings_win.set_runtime_status(state)

    def _retry_runtime(self):
        for key, name, target in (
            ("llama", "llama-preload", self._load_llama),
            ("ocr", "ocr-preload", self._load_ocr),
        ):
            if self._preload_status.get(key) == "fail":
                self._preload_status[key] = "pending"
                self._preload_errors[key] = ""
                self._start_preload_thread(name, target)
        self._sync_runtime_status()

    def _load_llama(self):
        try:
            self.log.info("预热：启动 llama-server…")
            self.server.start()
            try:
                self.translator.translate("ok", "简体中文")
                self.log.info("预热：翻译空转成功")
            except Exception as e:
                self.log.warning("预热：翻译空转失败（不阻断）: %s", e)
            self._preload_signals.status.emit("llama", "ok", "")
        except Exception as e:
            self._preload_signals.status.emit("llama", "fail", str(e))

    def _load_ocr(self):
        try:
            self.log.info("预热：加载 ONNX OCR…")
            self.ocr.preload()
            self.log.info("预热：OCR 空转成功")
            self._preload_signals.status.emit("ocr", "ok", "")
        except Exception as e:
            self._preload_signals.status.emit("ocr", "fail", str(e))

    def _preload_models(self):
        """启动时并行预热：llama 翻译服务 + ONNX OCR + 一次空转推理。

        首次翻译卡顿常见原因：
        1) 翻译模型还在加载 / 首次 GPU 推理未热身
        2) OCR 首次创建 ONNX/DirectML 会话并编译图，第一次识别会更慢
        """
        if getattr(self, "_preload_started", False):
            return
        self._preload_started = True
        self._preload_status.update(llama="pending", ocr="pending")
        self.log.info("开始后台预热 OCR + 翻译模型")
        self._start_preload_thread("llama-preload", self._load_llama)
        self._start_preload_thread("ocr-preload", self._load_ocr)

    # ---------- 截屏翻译 ----------
    def _start_select(self, mode: str):
        if self._quitting:
            return
        if mode not in ("screenshot", "region_watch"):
            self.log.warning("忽略未知框选模式 mode=%s", mode)
            return
        if self._pending_mode is not None or self.selector.isVisible():
            self.log.info("已有框选操作，忽略重复触发 mode=%s", mode)
            return
        if mode == "screenshot" and not self._ensure_server():
            return
        capture.configure_qt_screens(self.qapp.screens())
        self.log.info("开始框选 mode=%s", mode)
        self._pending_mode = mode
        try:
            self.selector.start()
        except Exception as e:
            self._pending_mode = None
            self.log.exception("启动框选失败 mode=%s", mode)
            self._show_error(str(e))

    def _on_region(self, x: int, y: int, w: int, h: int):
        if self._pending_mode is None:
            return  # 本次框选由其他功能发起，不在这里处理
        mode, self._pending_mode = self._pending_mode, None
        if mode == "region_watch":
            self._start_region_watch(x, y, w, h)
            return
        if mode != "screenshot":
            self.log.warning("忽略未知框选结果 mode=%s", mode)
            return
        region = (x, y, w, h)
        # 优先用框选时已截好的静态底图裁切（无二次截屏、减轻闪一下）
        img = self.selector.take_crop()
        if img is None:
            img = capture.grab_region(x, y, w, h)
        if not self._ensure_server():
            return
        worker = OcrTranslateWorker(
            self.ocr, self.translator, self.cfg,
            image=img, do_translate=True,
            target_language=self.translate_win.target_language,
        )
        worker.finished_ok.connect(
            lambda source, translation, r=region:
                self._show_screenshot_result(r, source, translation)
        )
        worker.failed.connect(self._show_error)
        self._run_worker(worker)

    def _show_screenshot_result(
        self, region: tuple[int, int, int, int], source: str, translation: str
    ):
        if self._quitting:
            return
        x, y, w, h = region
        if self.cfg.get("history_enabled", True):
            self.storage.add_history(source, translation, "screenshot")
        self.translate_win.show_result(source, translation, x, y + h + 8)

    # ---------- 划词翻译 ----------
    def _word_translate(self):
        """划词：多路复制（终端常需 Ctrl+Shift+C）→ 剪贴板 → 失败则提示。

        终端/部分应用问题：
        - Ctrl+C 是中断不是复制
        - 仅 Ctrl+Shift+C 或「选中即复制」有效
        - 焦点控件需 WM_COPY
        """
        if self._quitting or self._word_copy_busy:
            self.log.info("划词复制尚未结束，忽略重复触发")
            return
        if not self._ensure_server():
            return
        import time as _time

        from . import selection as sel

        self._word_copy_busy = True
        self._word_old_mime = _clone_mime_data(QApplication.clipboard().mimeData())
        # 唯一标记：只有剪贴板变成「非标记」才算复制成功
        self._word_marker = f"\u200bST{_time.time_ns()}\u200b"
        QApplication.clipboard().setText(self._word_marker)
        # 复制阶段：wm_copy → ctrl_c → ctrl_shift_c → ctrl_insert
        self._word_copy_phase = 0
        self._word_copy_kinds = (
            "wm_copy", "ctrl_c", "ctrl_shift_c", "ctrl_insert"
        )
        self._word_fire_copy_phase()
        # 每阶段轮询约 350ms，共 4 阶段
        self._word_poll_clipboard(attempts=12)

    def _word_fire_copy_phase(self):
        from . import selection as sel

        kinds = self._word_copy_kinds
        i = int(self._word_copy_phase)
        if i < 0 or i >= len(kinds):
            return
        kind = kinds[i]
        self.log.info("划词复制阶段 %s", kind)
        # 每阶段重新写入标记，避免上一阶段残留误判
        marker = self._word_marker
        try:
            if marker:
                QApplication.clipboard().setText(marker)
            if kind == "wm_copy":
                sel.try_wm_copy()
            else:
                sel.send_copy_shortcut(kind)
        except Exception as e:
            # 当前方式失败仍让轮询继续，超时后会自动尝试下一种复制方式。
            self.log.warning("划词复制阶段失败 kind=%s: %s", kind, e)

    def _word_clipboard_text(self) -> str:
        """读取剪贴板；若仍是标记或空则返回空串。"""
        marker = self._word_marker
        text = QApplication.clipboard().text()
        if not text:
            return ""
        if marker and text == marker:
            return ""
        return text.strip()

    def _word_poll_clipboard(self, attempts: int):
        """高频轮询；本阶段失败则进入下一复制方式。"""
        if not self._word_copy_busy or self._quitting:
            return
        text = self._word_clipboard_text()
        if text:
            self._word_finish_with_text(text)
            return
        if attempts > 0:
            QTimer.singleShot(
                30, lambda: self._word_poll_clipboard(attempts - 1)
            )
            return
        # 本阶段超时 → 下一阶段
        phase = int(self._word_copy_phase) + 1
        kinds = self._word_copy_kinds
        if phase < len(kinds):
            self._word_copy_phase = phase
            self._word_fire_copy_phase()
            self._word_poll_clipboard(attempts=12)
            return
        # 全部复制方式失败
        self._word_finish_with_text("")

    def _word_finish_with_text(self, text: str):
        """有选中文本则译；拿不到则提示（仅划词复制，不做附近 OCR）。"""
        pos = QCursor.pos()
        self._restore_word_clipboard()

        if self._quitting:
            return
        if not text:
            self.tray.showMessage(_t("msg_word_title"), _t("msg_word_empty"))
            return
        self.log.info("划词拿到文本 chars=%d", len(text))
        worker = OcrTranslateWorker(
            self.ocr, self.translator, self.cfg, text=text,
            target_language=self.translate_win.target_language,
        )
        worker.finished_ok.connect(
            lambda s, t: self._show_word_result(pos.x(), pos.y(), s, t)
        )
        worker.failed.connect(self._show_error)
        self._run_worker(worker)

    def _restore_word_clipboard(self):
        """恢复划词前的全部剪贴板格式，并使旧轮询立即失效。"""
        old = self._word_old_mime
        self._word_old_mime = None
        self._word_copy_busy = False
        try:
            if old is not None:
                QApplication.clipboard().setMimeData(old)
        except Exception:
            pass

    def _show_word_result(self, x: int, y: int, source: str, translation: str):
        if self._quitting:
            return
        if self.cfg.get("history_enabled", True):
            self.storage.add_history(source, translation, "word")
        self.translate_win.show_result(source, translation, x + 12, y + 16)

    # ---------- 窗口 / 区域持续翻译 ----------
    def _is_continuous_active(self) -> bool:
        """是否已有持续翻译会话：监视线程在跑，或任一浮层仍显示。"""
        if self._watcher is not None and self._watcher.isRunning():
            return True
        try:
            if self.subtitle.isVisible():
                return True
        except Exception:
            pass
        try:
            if self.annotation.isVisible():
                return True
        except Exception:
            pass
        try:
            if self.annotate_ctrl.isVisible():
                return True
        except Exception:
            pass
        try:
            if self.region_frame.isVisible():
                return True
        except Exception:
            pass
        return False

    def _is_continuous_selecting(self) -> bool:
        """是否正在选择持续翻译目标（选窗对话框 / 区域框选）。"""
        picker = self._window_picker
        if picker is not None:
            try:
                if picker.isVisible():
                    return True
            except RuntimeError:
                self._window_picker = None
        if self._pending_mode == "region_watch":
            return True
        try:
            # 框选遮罩开着且用途是区域持续翻译
            if self.selector.isVisible() and self._pending_mode == "region_watch":
                return True
        except Exception:
            pass
        return False

    def _cancel_continuous_select(self):
        """取消进行中的选窗 / 区域框选（不动截屏/取字框选）。"""
        if self._pending_mode == "region_watch":
            self._pending_mode = None
            try:
                if self.selector.isVisible():
                    self.selector.cancel()
            except Exception:
                pass
        picker = self._window_picker
        if picker is not None:
            try:
                picker.reject()
            except Exception:
                pass
            self._window_picker = None

    def _on_region_cancelled(self):
        """框选 Esc / 无效框 / 程序 cancel：清掉本次框选状态。"""
        mode = self._pending_mode
        if mode is None:
            return
        self._pending_mode = None
        if mode == "region_watch":
            self.log.info("区域持续翻译：框选已取消")
        else:
            self.log.info("框选已取消 mode=%s", mode)

    def _hide_watch_ui(self):
        """立刻藏起所有持续翻译浮层（不等线程结束）。"""
        try:
            self._region_topmost_timer.stop()
        except Exception:
            pass
        try:
            self._window_follow_timer.stop()
        except Exception:
            pass
        try:
            self.subtitle.set_paused(False)
            self.subtitle.set_interactive(False)
            self.subtitle.hide()
            self.subtitle.set_capture_visible(True)
        except Exception:
            pass
        try:
            self.annotation.hide()
            self.annotation.clear()
        except Exception:
            pass
        try:
            self.annotate_ctrl.set_paused(False)
            self.annotate_ctrl.hide()
        except Exception:
            pass
        try:
            self.region_frame.hide_frame()
        except Exception:
            pass
        # 隐藏后再解绑，恢复默认置顶（供区域翻译）；避免改 flag 时闪一下
        try:
            self._set_watch_layer_owner(None)
        except Exception:
            pass

    def _set_watch_layer_owner(self, owner_hwnd: int | None) -> None:
        """窗口翻译：字幕/备注跟目标窗同层；None=不绑定（区域翻译仍置顶）。"""
        for w in (self.subtitle, self.annotation, self.annotate_ctrl):
            try:
                w.set_layer_owner(owner_hwnd)
            except Exception:
                pass

    def _restack_watch_layer(self) -> None:
        """窗口浮层贴回目标上方；区域浮层持续保持在 TOPMOST 层最前。"""
        if not self._watch_hwnd and self._watch_region is None:
            return
        for w in (self.subtitle, self.annotation, self.annotate_ctrl):
            try:
                w.restack_layer()
            except Exception:
                pass
        if self._watch_region is not None:
            try:
                self.region_frame.restack_layer()
            except Exception:
                pass

    def _disconnect_watcher(self, w) -> None:
        """断开监视线程全部信号，避免停止后仍刷新 UI / 历史。"""
        if w is None:
            return
        for sig in (
            w.stopped,
            w.subtitle_ready,
            w.annotations_ready,
            w.history_ready,
            w.window_moved,
            w.content_cleared,
        ):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass

    def _retain_watcher(self, w: WindowWatcher) -> None:
        """保留尚未退出的 QThread，避免局部引用释放时被销毁。"""
        if w in self._retired_watchers:
            return
        self._retired_watchers.append(w)
        w.finished.connect(lambda w=w: self._drop_retired_watcher(w))

    def _drop_retired_watcher(self, w: WindowWatcher) -> None:
        try:
            self._retired_watchers.remove(w)
        except ValueError:
            pass

    def _join_watcher(self, w, *, label: str = "监视线程") -> bool:
        """请求停止并短暂等待；尽量少进事件循环，避免停线程时重入开新会话。"""
        if w is None:
            return True
        self._disconnect_watcher(w)
        w.stop()
        was = self._stopping_watch
        self._stopping_watch = True
        try:
            # 先纯等待，不处理用户输入事件
            if w.wait(1500):
                return True
            # 仍在跑：少量 processEvents（排除用户输入）保 UI 不假死
            for _ in range(10):
                if not w.isRunning():
                    break
                self.qapp.processEvents(
                    QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
                )
                if w.wait(100):
                    break
            if w.isRunning():
                self.log.warning("%s仍在运行（可能卡在翻译请求），UI 已关闭", label)
                self._retain_watcher(w)
                return False
            return True
        finally:
            self._stopping_watch = was

    def _stop_continuous_translate(self, reason: str = "已停止监视"):
        """停止窗口/区域持续翻译：先关 UI，再停线程，避免「关不掉」。"""
        if self._stopping_watch and self._watcher is None:
            # 已在停止流程中，避免 processEvents 重入再开会话
            self._hide_watch_ui()
            self._cancel_continuous_select()
            return
        self.log.info("停止持续翻译: %s", reason)
        # 0) 若还在选窗/框选，一并取消
        self._cancel_continuous_select()
        # 1) 先藏 UI，翻译卡在后台时用户也能马上关掉浮层
        self.generation_tracker.reset()
        self._hide_watch_ui()
        w = self._watcher
        self._watcher = None
        self._watch_hwnd = None
        self._watch_region = None
        self._watch_rect = None
        self._watch_profile = None
        self._watch_annotate = None
        self._join_watcher(w, label="监视线程")
        # 不弹托盘气泡，写日志即可

    def _toggle_window_watch(self):
        if self._quitting:
            return
        # 停止流程中（等旧线程）不再开新会话，避免叠两个
        if self._stopping_watch:
            self._stop_continuous_translate("已停止窗口持续翻译")
            return
        # 已有会话 或 正在选窗/框选 → 一律只停/取消，不同时开第二个
        if self._is_continuous_active() or self._is_continuous_selecting():
            self._stop_continuous_translate("已停止窗口持续翻译")
            return
        if not self._ensure_server():
            return
        capture.configure_qt_screens(self.qapp.screens())
        picker = WindowPicker()
        self._window_picker = picker
        try:
            accepted = bool(picker.exec())
            hwnd = picker.selected_hwnd if accepted else None
        finally:
            self._window_picker = None
        if not accepted or hwnd is None:
            return
        if self._stopping_watch:
            self.log.info("停止过程中忽略新的窗口持续翻译启动")
            return
        # 选窗过程中若残留了其它会话，启动前清干净（保证同时只有一个）
        if self._is_continuous_active():
            self._stop_continuous_translate("切换为窗口持续翻译")
        if self._stopping_watch:
            return
        rect = capture.get_window_rect(hwnd)
        annotate = bool(self.cfg.get("window_watch_annotate"))
        self._launch_watcher(
            hwnd=hwnd, rect=rect, annotate=annotate, profile="window"
        )

    def _toggle_region_watch(self):
        """框选屏幕区域持续翻译（显示/间隔在设置里单独配置）。"""
        if self._quitting:
            return
        if self._stopping_watch:
            self._stop_continuous_translate("已停止区域持续翻译")
            return
        # 已有会话 或 正在选 → 只停，不叠第二个
        if self._is_continuous_active() or self._is_continuous_selecting():
            self._stop_continuous_translate("已停止区域持续翻译")
            return
        if not self._ensure_server():
            return
        self._start_select("region_watch")

    def _start_region_watch(self, x: int, y: int, w: int, h: int):
        if self._stopping_watch:
            self.log.info("停止过程中忽略区域持续翻译启动")
            return
        region = (x, y, w, h)
        # 框选完成后若还有旧会话/浮层，先清再启
        if self._is_continuous_active():
            self._stop_continuous_translate("切换为区域持续翻译")
        if self._stopping_watch:
            return
        annotate = bool(self.cfg.get("region_watch_annotate"))
        self._launch_watcher(
            region=region, rect=region, annotate=annotate, profile="region"
        )

    def _launch_watcher(self, rect, annotate: bool,
                        hwnd: int | None = None, region=None,
                        profile: str = "window"):
        if self._stopping_watch:
            self.log.info("停止过程中拒绝启动持续翻译 profile=%s", profile)
            return
        # 硬互斥：启动前停掉旧监视线程，避免两个 WindowWatcher 并行
        old = self._watcher
        self._watcher = None
        if old is not None:
            self.log.info("启动前停止旧监视线程 profile=%s", self._watch_profile)
            if not self._join_watcher(old, label="旧监视线程"):
                self.log.warning("旧监视线程尚未退出，本次启动已取消")
                return
        # 上次超时保留的线程可能还在跑最后一帧（卡在翻译请求），
        # 此时不启动新会话，避免两路抓屏+OCR+翻译并行空耗资源。
        self._retired_watchers = [
            w for w in self._retired_watchers if w.isRunning()
        ]
        if self._retired_watchers:
            self.log.warning(
                "仍有 %d 个旧监视线程未退出（可能卡在翻译请求），本次启动已取消",
                len(self._retired_watchers),
            )
            return
        if self._stopping_watch:
            return
        # 只取消区域持续翻译的框选，不动截屏/取字
        if self._pending_mode == "region_watch":
            self._pending_mode = None
            try:
                if self.selector.isVisible():
                    self.selector.cancel()
            except Exception:
                pass

        if hwnd is not None:
            self._watch_region = None
            self.region_frame.hide_frame()
        self._watch_hwnd = hwnd
        self._watch_region = region
        self._watch_rect = rect
        self._watch_profile = profile
        self._subtitle_geometry_valid = True
        annotate = bool(annotate)
        self._watch_annotate = annotate
        self._watch_paused = False
        self.subtitle.set_paused(False)
        self.annotate_ctrl.set_paused(False)
        # 写回对应 profile 的显示模式（内存；设置页保存才会落盘）
        self.cfg[f"{profile}_watch_annotate"] = annotate
        self.log.info(
            "启动持续翻译 profile=%s annotate=%s hwnd=%s region=%s rect=%s",
            profile, annotate, hwnd, region, rect,
        )
        # 区域翻译：单独识别框，可拖动 / 固定
        if region is not None:
            self.region_frame.show_region(region)
            # 区域无目标窗：浮层保持全局置顶
            self._set_watch_layer_owner(None)
        else:
            self.region_frame.hide_frame()
            # 窗口翻译：字幕/备注与被译窗口同层，不压在其它应用上
            self._set_watch_layer_owner(hwnd)
        # 备注=贴原文旁（窗口/区域相同）；字幕条=外侧，不盖住目标
        self._apply_watch_font_size(profile)
        self._apply_watch_display(annotate, rect, announce=True)
        # 显示后再应用一次（setWindowFlags / 首次 show 可能重建 HWND）
        self._set_watch_layer_owner(hwnd)
        if region is not None:
            self._restack_watch_layer()
            self._region_topmost_timer.start()
            self._window_follow_timer.stop()
        else:
            self._region_topmost_timer.stop()
            self._window_follow_timer.start()
        display = "annotate" if annotate else "subtitle"
        self.generation_tracker.reset()
        self._watcher = WindowWatcher(
            self.ocr, self.translator, self.cfg,
            hwnd=hwnd, region=region, display_mode=display,
            profile=profile,
            generation_tracker=self.generation_tracker,
        )
        self._watcher.subtitle_ready.connect(self.subtitle.set_text)
        self._watcher.annotations_ready.connect(self._on_watch_annotations)
        self._watcher.history_ready.connect(self._on_watch_history)
        self._watcher.window_moved.connect(self._on_target_window_moved)
        self._watcher.content_cleared.connect(self._on_watch_content_cleared)
        self._watcher.stopped.connect(self._on_watch_stopped)
        self._watcher.start()

    def _set_watch_paused(self, paused: bool):
        watcher = self._watcher
        if watcher is None or not watcher.isRunning():
            return
        self._watch_paused = bool(paused)
        watcher.set_paused(self._watch_paused)
        self.subtitle.set_paused(self._watch_paused)
        self.annotate_ctrl.set_paused(self._watch_paused)
        if self.annotate_ctrl.isVisible() and self._watch_rect is not None:
            self.annotate_ctrl.place_above(self._watch_rect)
        self.log.info("持续翻译%s", "已暂停" if self._watch_paused else "已继续")

    def _on_region_frame_moved(self, x: int, y: int, w: int, h: int):
        """区域识别框被拖动/缩放：同步 OCR 选区与字幕/备注位置。"""
        if self._watch_region is None:
            return
        rect = (x, y, w, h)
        self._watch_region = rect
        self._watch_rect = rect
        if self._watcher and self._watcher.isRunning():
            self._watcher.set_region(rect)
        if self._watch_annotate is True:
            # 移动/缩放后旧译文坐标已失效；先清空，确保下一帧是干净底图。
            self.annotation.set_items([])
        self.annotation.update_geometry(rect)
        self._sync_annotation_mask()
        # 跟随模式才重贴；自由/固定保持用户拖好的字幕位置
        if self.subtitle.mode == "follow":
            self.subtitle.attach_below(rect, outside=True, match_target_size=True)
        if self.annotate_ctrl.isVisible():
            self.annotate_ctrl.place_above(rect)
        self.log.info("区域识别框更新 %s", rect)

    def _on_watch_annotations(self, items: list) -> None:
        """主线程绘制备注，并把精确像素遮罩同步给下一轮 OCR。"""
        if not self._watcher or self._watch_annotate is not True:
            return
        self.annotation.set_items(items)
        self._sync_annotation_mask()

    def _sync_annotation_mask(self) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        mask = None
        # 浮层被排除捕获时（默认），OCR 抓屏天然看不到译文，无需遮罩
        if (
            self._watch_region is not None
            and self._watch_annotate is True
            and bool(self.cfg.get("annotate_capture_visible"))
        ):
            mask = self.annotation.capture_mask()
        watcher.set_annotation_mask(mask)

    def _on_watch_content_cleared(self) -> None:
        """源文字连续消失时同步清空译文，不保留上一条过期内容。"""
        if not self._watcher:
            return
        if self._watch_annotate is True:
            self.annotation.set_items([])
            self._sync_annotation_mask()
        else:
            self.subtitle.set_text("")

    def _on_watch_history(self, source: str, translation: str, mode: str):
        """持续翻译有实质新译文时异步写入历史，避免磁盘 I/O 阻塞 UI 渲染循环 (PR 12)。"""
        if not self.cfg.get("history_enabled", True) or self._quitting:
            return
        try:
            if hasattr(self.storage, "add_history_async"):
                self.storage.add_history_async(source, translation, mode)
            else:
                self.storage.add_history(source, translation, mode)
        except Exception:
            self.log.exception("持续翻译历史写入失败")

    _on_window_translated = _on_watch_history

    def _apply_watch_font_size(self, profile: str | None = None) -> None:
        """把当前窗口或区域翻译的独立字号应用到两种显示模式。"""
        profile = profile or self._watch_profile
        if profile not in ("window", "region"):
            return
        try:
            size = int(self.cfg.get(f"{profile}_watch_font_size", 0))
        except (TypeError, ValueError):
            size = 0
        self.subtitle.set_font_size(size)
        self.annotation.set_font_size(size)

    def _apply_watch_display(self, annotate: bool, rect, *, announce: bool = False):
        """备注：贴原文旁（窗口/区域同）；字幕条：目标外侧，不遮挡。"""
        if annotate:
            self.annotate_ctrl.set_paused(self._watch_paused)
            self.subtitle.set_interactive(False)
            self.subtitle.hide()
            self.annotation.update_geometry(rect)
            # 备注层立即占位（有译文后 set_items 再显示）；不 raise 防闪
            if not self.annotation.isVisible():
                self.annotation.show()
            # 备注模式控制条（跳过目标语 + 关闭）；按当前会话 profile 读配置
            self.annotate_ctrl.set_skip_target(
                bool(self.cfg.get(self._annotate_skip_cfg_key()))
            )
            self.annotate_ctrl.place_above(rect)
        else:
            self.subtitle.set_paused(self._watch_paused)
            self.annotation.clear()
            try:
                self.annotate_ctrl.hide()
            except Exception:
                pass
            # 外置字幕可缩放；outside=True 永远不盖住目标窗/识别区
            self.subtitle.set_interactive(True)
            mode = str(self.cfg.get("subtitle_mode", "follow"))
            self.subtitle.set_mode(
                mode if mode in ("follow", "free", "pinned") else "follow"
            )
            is_region = self._watch_region is not None or self._watch_profile == "region"
            self.subtitle.set_capture_visible(
                not is_region or bool(self.cfg.get("annotate_capture_visible"))
            )
            self.subtitle.attach_below(rect, outside=True, match_target_size=is_region)
            # PR 1 零闪烁：不在启动时注入占位文本，保持隐藏直到第一帧有效翻译到达
            self.subtitle.hide()

    def _switch_watch_display(self, annotate: bool):
        """运行中切换字幕 ↔ 备注（不停止监视线程）。"""
        if self._stopping_watch:
            return
        if self._watcher is None or not self._watcher.isRunning():
            self.log.info("无进行中的持续翻译，忽略显示模式切换")
            return
        annotate = bool(annotate)
        if self._watch_annotate is not None and bool(self._watch_annotate) == annotate:
            return  # 已是目标模式
        rect = self._watch_rect
        if rect is None:
            if self._watch_region is not None:
                rect = self._watch_region
            elif self._watch_hwnd is not None:
                rect = capture.get_window_rect(self._watch_hwnd)
        if rect is None:
            self.log.warning("切换显示模式失败：无目标区域")
            return
        profile = self._watch_profile
        if profile not in ("window", "region"):
            profile = "region" if self._watch_region is not None else "window"
        self._watch_annotate = annotate
        self.cfg[f"{profile}_watch_annotate"] = annotate
        try:
            config.save(self.cfg)
        except Exception:
            pass
        mode = "annotate" if annotate else "subtitle"
        self.generation_tracker.reset()
        try:
            self._watcher.set_display_mode(mode)
        except Exception:
            self.log.exception("set_display_mode 失败")
            return
        self._apply_watch_display(annotate, rect, announce=False)
        # 切换模式后 HWND 可能重建，重新挂层
        if self._watch_hwnd is not None:
            self._set_watch_layer_owner(self._watch_hwnd)
        if annotate:
            # 备注尚无新结果时先空层；下一轮监视会 set_items
            self.annotation.set_items([])
            self.log.info("已切换为备注模式 profile=%s", profile)
        else:
            self.log.info("已切换为字幕条模式 profile=%s", profile)
        self._sync_annotation_mask()

    def _annotate_skip_cfg_key(self) -> str:
        """当前会话对应的「跳过目标语」配置键。

        注意：self._watch_profile 是 str 属性（window/region），不可再定义同名方法。
        """
        p = self._watch_profile
        if p not in ("window", "region"):
            p = "region" if self._watch_region is not None else "window"
        return f"{p}_annotate_skip_target_lang"

    def _on_annotate_skip_target(self, on: bool):
        """备注条「跳过目标语」：写入当前会话（窗口/区域）各自配置，下轮生效。"""
        on = bool(on)
        key = self._annotate_skip_cfg_key()
        self.cfg[key] = on
        # 去掉旧共用键，避免下次 load 再迁移覆盖
        self.cfg.pop("annotate_skip_target_lang", None)
        try:
            config.save(self.cfg)
        except Exception:
            pass
        self.log.info("备注跳过目标语 %s: %s", key, on)

    def _on_subtitle_mode_changed(self, mode: str):
        """字幕条：跟随 / 自由 / 固定。跟随模式重新吸附到目标外侧。"""
        self.cfg["subtitle_mode"] = mode
        try:
            config.save(self.cfg)
        except Exception:
            self.log.exception("保存字幕模式失败")
        if mode != "follow":
            return
        if self._watch_hwnd is not None:
            self.subtitle.attach_below(
                capture.get_window_rect(self._watch_hwnd), outside=True
            )
        elif self._watch_region is not None:
            self.subtitle.attach_below(
                self._watch_region, outside=True, match_target_size=True
            )

    def _follow_target_window(self) -> None:
        """高频跟随目标窗口位置；变化时才更新浮层，不动 OCR 节奏。"""
        hwnd = self._watch_hwnd
        if hwnd is None:
            self._window_follow_timer.stop()
            return
        try:
            rect = capture.get_window_rect(hwnd)
        except Exception:
            # 目标窗刚关闭等瞬态；结束会话的善后由监视线程负责
            self._window_follow_timer.stop()
            return
        if rect != self._watch_rect:
            self._on_target_window_moved(*rect)

    def _on_target_window_moved(self, x: int, y: int, w: int, h: int):
        # 区域监视的几何由识别框控制，窗口 rect 信号不改 region
        if self._watch_region is not None:
            return
        rect = (x, y, w, h)
        self._watch_rect = rect
        # 跟随模式才重贴；自由/固定保持用户位置
        if self.subtitle.isVisible() and self.subtitle.mode == "follow":
            self.subtitle.attach_below(rect, outside=True)
        if self.annotation.isVisible():
            self.annotation.update_geometry(rect)
        if self.annotate_ctrl.isVisible():
            self.annotate_ctrl.place_above(rect)
        # 目标窗 z 序可能变了：字幕/备注贴回目标正上方（仍非全局置顶）
        self._restack_watch_layer()

    def _on_watch_stopped(self, reason: str):
        """线程自行结束（目标关闭/出错）时的清理。"""
        self.log.info("持续翻译线程结束: %s", reason.replace("\n", " | ")[:300])
        # 可能已被用户手动停止（_watcher 已置空）
        if self._watcher is not None and not self._watcher.isRunning():
            self._watcher = None
        self._hide_watch_ui()
        self._watch_hwnd = None
        self._watch_region = None
        self._watch_rect = None
        self._watch_profile = None
        self._watch_annotate = None

    # ---------- 公共 ----------
    def _show_window(self, win):
        """设置/历史/翻译窗：置顶到最前，避免被挡住找不到。"""
        from .ui.topmost import center_on_cursor_screen, raise_to_front

        if not getattr(win, "_remembered_geometry", False):
            center_on_cursor_screen(win)
            win._remembered_geometry = True
        raise_to_front(win, activate=True)

    def _save_window_geometries(self):
        from .ui.topmost import window_geometry_value

        for key, window in (
            ("settings_window_geometry", self.settings_win),
            ("history_window_geometry", self.history_win),
            ("translate_window_geometry", self.translate_win),
        ):
            if getattr(window, "_remembered_geometry", False):
                self.cfg[key] = window_geometry_value(window)
        if self._subtitle_geometry_valid:
            self.cfg["subtitle_geometry"] = window_geometry_value(self.subtitle)
        self.cfg["subtitle_mode"] = self.subtitle.mode
        try:
            config.save(self.cfg)
        except Exception:
            self.log.exception("保存窗口位置失败")

    def _on_settings_saved(self):
        # 热键可能已修改：重新注册监听，并刷新托盘菜单文案
        conflicts = self.hotkeys.start()
        if conflicts:
            self.tray.showMessage(
                _t("hk_conflict_title"),
                "；".join(conflicts)[:200],
                QSystemTrayIcon.MessageIcon.Warning,
            )
        self.apply_ui_language()
        self.translate_win.sync_language_from_cfg()
        self.translate_win.sync_font_size_from_cfg()
        # 备注译文颜色
        try:
            self.annotation.set_text_color(
                self.cfg.get("annotate_text_color", "#00F0FF")
            )
        except Exception:
            pass
        # 备注译文是否参与截屏/录屏（关闭时区域备注免遮罩还原，更快）
        try:
            self.annotation.set_capture_visible(
                bool(self.cfg.get("annotate_capture_visible"))
            )
        except Exception:
            pass
        # 备注条上的「跳过目标语」与设置同步（按当前会话类型）
        self.annotate_ctrl.set_skip_target(
            bool(self.cfg.get(self._annotate_skip_cfg_key()))
        )
        # 若正在监视：仅当「显示模式」与当前会话不一致时才切换（避免只改颜色/热键也清缓存重译）
        if (
            self._watcher
            and self._watcher.isRunning()
            and self._watch_rect
            and self._watch_profile
        ):
            self._apply_watch_font_size(self._watch_profile)
            self._sync_annotation_mask()
            key = f"{self._watch_profile}_watch_annotate"
            annotate = bool(self.cfg.get(key))
            cur = self._watch_annotate
            if cur is None or bool(cur) != annotate:
                self._switch_watch_display(annotate)

    def _run_worker(self, worker: OcrTranslateWorker):
        if self._quitting:
            worker.requestInterruption()
            return
        self._workers.append(worker)

        def _drop(_w=worker):
            try:
                self._workers.remove(_w)
            except ValueError:
                pass

        worker.finished.connect(_drop)
        worker.start()

    def _show_error(self, msg: str):
        if self._quitting:
            return
        # 完整报错写 app.log，托盘只显示摘要
        self.log.error("任务失败:\n%s", msg)
        self.tray.showMessage(
            _t("msg_error"), msg[:200], QSystemTrayIcon.MessageIcon.Warning
        )

    def _open_log(self):
        """用系统默认方式打开日志文件。"""
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if not LOG_PATH.exists():
                LOG_PATH.write_text("", encoding="utf-8")
            # Windows：用默认程序打开
            import os

            os.startfile(str(LOG_PATH))  # type: ignore[attr-defined]
            self.log.info("用户打开日志 %s", LOG_PATH)
        except Exception as e:
            self.log.exception("打开日志失败")
            from .ui.topmost import topmost_message

            topmost_message(
                "information",
                _t("log_title"),
                _t("msg_log_open_fail", path=str(LOG_PATH), e=e),
            )

    def _quit(self):
        if self._quitting:
            return
        self._quitting = True
        self._shutdown_started_at = time.monotonic()
        self.log.info("退出程序")
        self._save_window_geometries()
        self._restore_word_clipboard()
        self._stop_continuous_translate("程序退出")
        self.hotkeys.stop()
        self.tray.hide()
        for worker in list(self._workers):
            worker.requestInterruption()
        input_worker = self.translate_win.active_worker()
        if input_worker is not None:
            input_worker.requestInterruption()
        QTimer.singleShot(50, self._poll_shutdown)

    def _poll_shutdown(self):
        """等待所有会触碰 OCR/Translator/UI/Storage 的任务自然收尾。"""
        self._workers = [w for w in self._workers if w.isRunning()]
        self._retired_watchers = [w for w in self._retired_watchers if w.isRunning()]
        self._preload_threads = [t for t in self._preload_threads if t.is_alive()]
        input_worker = self.translate_win.active_worker()
        tasks_running = bool(
            self._workers
            or self._retired_watchers
            or self._preload_threads
            or input_worker is not None
        )
        elapsed = time.monotonic() - (
            self._shutdown_started_at or time.monotonic()
        )
        if (
            tasks_running
            and elapsed >= _SHUTDOWN_ABORT_SECONDS
            and self._shutdown_abort_thread is None
        ):
            self.log.warning(
                "退出等待 %.1f 秒仍有后台任务，停止 llama-server 以取消本地请求",
                elapsed,
            )
            self._shutdown_abort_thread = threading.Thread(
                target=self.resources.interrupt_server,
                daemon=True,
                name="llama-abort",
            )
            self._shutdown_abort_thread.start()
        abort_running = bool(
            self._shutdown_abort_thread is not None
            and self._shutdown_abort_thread.is_alive()
        )
        if tasks_running or abort_running:
            if elapsed >= _SHUTDOWN_HARD_LIMIT_SECONDS:
                self.log.critical(
                    "退出超过 %.1f 秒，执行应用事件循环兜底退出",
                    elapsed,
                )
                self.qapp.quit()
                return
            QTimer.singleShot(100, self._poll_shutdown)
            return
        if not self._shutdown_resources_closed:
            self.resources.close_clients()
            self._shutdown_resources_closed = True
            # 所有请求线程退出、HTTP 会话关闭后，最后停止 llama 服务。
            self._shutdown_server_thread = threading.Thread(
                target=self.resources.stop_server,
                daemon=True,
                name="llama-stop",
            )
            self._shutdown_server_thread.start()
            QTimer.singleShot(100, self._poll_shutdown)
            return
        if self._shutdown_server_thread is not None and self._shutdown_server_thread.is_alive():
            if elapsed >= _SHUTDOWN_HARD_LIMIT_SECONDS:
                self.log.critical(
                    "llama-server 停止超过退出上限，执行应用事件循环兜底退出"
                )
                self.qapp.quit()
                return
            QTimer.singleShot(100, self._poll_shutdown)
            return
        self.qapp.quit()

    def exec(self) -> int:
        conflicts = self.hotkeys.start()
        if conflicts:
            self.log.warning("热键冲突: %s", conflicts)
        self.log.info(
            "应用已启动 v%s 截屏=%s 划词=%s 窗口=%s 区域=%s log=%s",
            __version__,
            self.cfg.get("hotkey_screenshot"),
            self.cfg.get("hotkey_word"),
            self.cfg.get("hotkey_window"),
            self.cfg.get("hotkey_region_watch"),
            LOG_PATH,
        )
        # 进入事件循环后再显示，确保窗口能正确居中并获得焦点。
        QTimer.singleShot(0, lambda: self._show_window(self.settings_win))
        return self.qapp.exec()


def _acquire_single_instance(qapp: QApplication) -> QSharedMemory | None:
    """确保仅一个实例；已有实例时返回 None。

    崩溃残留的共享内存：attach+detach 后再 create，避免误拦二次启动。
    """
    mem = QSharedMemory(_INSTANCE_KEY)
    if not mem.create(1):
        # 尝试清理崩溃残留（活实例仍会占用，create 会再次失败）
        stale = QSharedMemory(_INSTANCE_KEY)
        if stale.attach():
            stale.detach()
        if not mem.create(1):
            from .ui.topmost import topmost_message

            from .i18n import set_language, t

            try:
                set_language(config.load().get("ui_language", "zh"))
            except Exception:
                pass
            topmost_message(
                "warning",
                t("app_name"),
                t("msg_running"),
            )
            return None
    # 保持引用，防止被 GC 释放共享内存
    qapp._screen_translator_instance = mem  # type: ignore[attr-defined]
    return mem


def _install_exception_hooks() -> None:
    """未捕获异常写入 app.log，避免 pythonw 静默消失。"""
    import threading
    import traceback

    def _hook(exc_type, exc, tb):
        try:
            log = get_logger("crash")
            log.critical(
                "未捕获异常\n%s",
                "".join(traceback.format_exception(exc_type, exc, tb)),
            )
        except Exception:
            pass
        # 保留默认行为（stderr）；pythonw 无控制台时至少有文件日志
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook

    if hasattr(threading, "excepthook"):

        def _thread_hook(args):
            try:
                log = get_logger("crash")
                log.critical(
                    "线程未捕获异常 thread=%s\n%s",
                    getattr(args, "thread", None),
                    "".join(
                        traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback
                        )
                    ),
                )
            except Exception:
                pass

        threading.excepthook = _thread_hook  # type: ignore[assignment]


def main():
    _install_exception_hooks()
    # Qt 要求在创建 QApplication 前设置；否则混合缩放策略不会稳定生效。
    if QApplication.instance() is None:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    qapp = QApplication.instance() or QApplication(sys.argv)
    if _acquire_single_instance(qapp) is None:
        return 1
    app = App()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
