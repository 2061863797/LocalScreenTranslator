# -*- coding: utf-8 -*-
"""主程序：托盘常驻，热键调度各功能。"""

import sys
import threading
import time
from typing import Any

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

    status = Signal(str, str, str, int)


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
            "配置已加载 target=%s model=%s ui=%s",
            self.cfg.get("target_language"),
            self.cfg.get("model_path"),
            get_language(),
        )
        self.resources = RuntimeResources.create(self.cfg)
        self.storage = self.resources.storage
        self.translator = self.resources.translator
        self.ocr = self.resources.ocr
        self._applied_translation_signature = self.translator.desired_signature()
        self._translation_config_revision = 0
        self._translation_load_lock = threading.Lock()
        self.generation_tracker = GenerationTracker()

        # UI 组件（窗口固定备注；区域支持字幕条和备注，并有可拖/固定识别框）
        self.selector = RegionSelector()
        self.subtitle = SubtitleBar()
        self.annotation = AnnotationOverlay()
        self.annotation.set_text_color(
            self.cfg.get("annotate_text_color", "#00F0FF")
        )
        self.annotate_ctrl = AnnotateCtrl()
        self.region_frame = RegionWatchFrame()
        self.subtitle.set_region_frame(self.region_frame)
        self.annotate_ctrl.set_region_frame(self.region_frame)
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
        self.subtitle.pause_changed.connect(self._set_watch_paused)
        self.annotate_ctrl.pause_changed.connect(self._set_watch_paused)
        self.subtitle.language_changed.connect(self._on_language_pair_changed)
        self.annotate_ctrl.language_changed.connect(self._on_language_pair_changed)
        self.subtitle.sync_languages(
            "自动",
            str(self.cfg.get("target_language", "简体中文")),
        )
        self.annotate_ctrl.sync_languages(
            "自动",
            str(self.cfg.get("target_language", "简体中文")),
        )
        self.region_frame.region_moved.connect(self._on_region_frame_moved)
        # 当前会话是否备注模式（与 cfg 同步，便于运行中切换）
        self._watch_annotate: bool | None = None
        self._watch_paused = False
        self._last_target_language = str(self.cfg.get("target_language", "简体中文"))

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
            self.translator,
            self.cfg,
            ensure_ready=self._ensure_translation_ready,
            on_target_language_changed=self._on_target_language_changed,
        )
        self.history_win = HistoryWindow(
            self.storage,
            on_open=lambda s, t: self.translate_win.show_result(s, t),
            on_clear_cache=self.translator.clear_cache,
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
            self.subtitle,
            self.cfg.get("subtitle_geometry"),
            geometry_setter=self.subtitle.set_content_geometry,
        )
        if self._subtitle_geometry_valid:
            subtitle_size = self.subtitle.content_size()
            self.subtitle._user_size = (
                subtitle_size.width(), subtitle_size.height()
            )

        self._workers: list[OcrTranslateWorker] = []  # 防止线程被垃圾回收
        self._retired_watchers: list[WindowWatcher] = []
        self._preload_threads: list[threading.Thread] = []
        self._preload_signals = _PreloadSignals()
        self._preload_signals.status.connect(self._on_preload_status)
        self._quitting = False
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
        self._watch_session_id: int = 0
        self._last_target_language: str = str(self.cfg.get("target_language", "简体中文"))
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
        # 托盘建好后后台预热：先初始化 OCR，再加载翻译模型
        self._preload_models()

    # ---------- 初始化 ----------
    def _build_tray(self):
        from .ui.theme import MENU_STYLE
        self.tray = QSystemTrayIcon(_tray_icon())
        menu = QMenu()
        menu.setStyleSheet(MENU_STYLE)
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

    def _ensure_translation_ready(self) -> bool:
        """只做快速就绪检查；启动/重试始终在后台，绝不阻塞 UI。"""
        if self._quitting:
            return False
        if self.translator.is_ready():
            return True
        state = self._preload_status.get("llama", "pending")
        if state == "fail" and not self._has_live_preload("translation-preload"):
            self.log.info("翻译模型上次加载失败，后台重试")
            self._preload_status["llama"] = "pending"
            self._start_preload_thread("translation-preload", self._load_llama)
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

    def _on_preload_status(self, key: str, value: str, err: str, revision: int):
        """Qt 主线程槽：更新状态和托盘，避免后台线程直接碰 UI。"""
        if key == "llama" and revision != self._translation_config_revision:
            return
        if key == "llama" and value == "ok":
            self._applied_translation_signature = self.translator.desired_signature()
            if revision and self._watcher and self._watcher.isRunning():
                self._watcher.on_target_language_changed()
            if revision and self.translate_win.isVisible():
                self.translate_win._go()
        if key == "llama" and value == "fail" and (
            self.translator.desired_signature() != self._applied_translation_signature
        ):
            _mode, path, device = self._applied_translation_signature
            self.cfg.update(model_path=path, llama_device=device)
            config.save(self.cfg)
            self.settings_win.reload_from_cfg()
            if self._watcher and self._watcher.isRunning():
                self._watcher.on_target_language_changed()
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
                    detail = self.ocr.provider
                else:
                    detail = self.translator.actual_device
            state[key] = (status, detail)
        self.settings_win.set_runtime_status(state)

    def _retry_runtime(self):
        retry_llama = self._preload_status.get("llama") == "fail"
        retry_ocr = self._preload_status.get("ocr") == "fail"
        for key, failed in (("llama", retry_llama), ("ocr", retry_ocr)):
            if failed:
                self._preload_status[key] = "pending"
                self._preload_errors[key] = ""
        if retry_llama and retry_ocr:
            self._start_preload_thread(
                "translation-preload", self._load_ocr_then_translation
            )
        else:
            if retry_llama:
                self._start_preload_thread("translation-preload", self._load_llama)
            if retry_ocr:
                self._start_preload_thread("ocr-preload", self._load_ocr)
        self._sync_runtime_status()

    def _load_llama(self, revision: int | None = None):
        revision = self._translation_config_revision if revision is None else revision
        with self._translation_load_lock:
            if revision != self._translation_config_revision or self._quitting:
                return
            signature = self.translator.desired_signature()
            try:
                self.log.info("预热：后台加载本地翻译模型…")
                self.translator.preload(signature)
                self._preload_signals.status.emit("llama", "ok", "", revision)
            except Exception as e:
                self._preload_signals.status.emit("llama", "fail", str(e), revision)

    def _load_ocr(self, revision: int = 0):
        try:
            self.log.info("预热：加载 ONNX OCR 模型…")
            self.ocr.preload()
            self.log.info("预热：OCR 空转成功")
            self._preload_signals.status.emit("ocr", "ok", "", revision)
        except Exception as e:
            self._preload_signals.status.emit("ocr", "fail", str(e), revision)

    def _load_ocr_then_translation(self):
        """先完成 OCR 初始化，再加载本地翻译模型。"""
        self._load_ocr()
        if not self._quitting:
            self._load_llama()

    def _preload_models(self):
        """启动时先完成 OCR 初始化，再加载本地翻译模型。"""
        if getattr(self, "_preload_started", False):
            return
        self._preload_started = True
        self._preload_status.update(llama="pending", ocr="pending")
        self.log.info("开始后台预热 OCR（完成后加载翻译模型）")
        self._start_preload_thread(
            "translation-preload", self._load_ocr_then_translation
        )

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
        if mode == "screenshot" and not self._ensure_translation_ready():
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
        if not self._ensure_translation_ready():
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
        if not self._ensure_translation_ready():
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
        try:
            self.region_frame.set_control_bar_external(False)
            self.subtitle.set_region_controls_visible(False)
            self.annotate_ctrl.set_region_controls_visible(False)
        except Exception:
            pass
        # 隐藏后再解绑，恢复默认置顶（供区域翻译）；避免改 flag 时闪一下
        try:
            self._set_watch_layer_owner(None)
        except Exception:
            pass

    def _set_watch_layer_owner(self, owner_hwnd: int | None) -> None:
        """窗口备注层跟目标窗同层；None=不绑定（区域翻译仍置顶）。"""
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
        self._watch_session_id += 1
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
        if not self._ensure_translation_ready():
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
        # 窗口持续翻译固定使用备注模式。
        annotate = True
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
        if not self._ensure_translation_ready():
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
        # 此时主动再次触发 abort 并快速回收，避免两路并行或永久阻塞。
        if self._retired_watchers:
            for rw in self._retired_watchers:
                try:
                    rw.stop()
                    rw.wait(300)
                except Exception:
                    pass
            self._retired_watchers = [
                w for w in self._retired_watchers if w.isRunning()
            ]
        if self._retired_watchers:
            self.log.warning(
                "仍有 %d 个旧监视线程未退出（等待资源释放中），本次启动已取消",
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
            self.region_frame.set_control_bar_external(False)
            self.region_frame.hide_frame()
        self._watch_hwnd = hwnd
        self._watch_region = region
        self._watch_rect = rect
        self._watch_profile = profile
        self._subtitle_geometry_valid = True
        # 窗口持续翻译固定使用备注模式；区域模式仍支持字幕与备注两种显示。
        annotate = True if profile == "window" else bool(annotate)
        self._watch_annotate = annotate
        self._watch_paused = False
        self.subtitle.set_paused(False)
        self.annotate_ctrl.set_paused(False)
        # 区域模式保留设置值；窗口模式固定为备注模式。
        if profile == "region":
            self.cfg["region_watch_annotate"] = annotate
        self.log.info(
            "启动持续翻译 profile=%s annotate=%s hwnd=%s region=%s rect=%s",
            profile, annotate, hwnd, region, rect,
        )
        # 区域翻译：单独识别框，可拖动 / 固定
        if region is not None:
            self.region_frame.set_control_bar_external(annotate)
            self.region_frame.show_region(region)
            # 区域无目标窗：浮层保持全局置顶
            self._set_watch_layer_owner(None)
        else:
            self.region_frame.set_control_bar_external(False)
            self.region_frame.hide_frame()
            # 窗口备注层与被译窗口同层，不压在其它应用上
            self._set_watch_layer_owner(hwnd)
        # 窗口固定使用备注；区域备注贴原文旁，区域字幕条放在目标外侧
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
        self._watch_session_id += 1
        current_session = self._watch_session_id
        watcher = WindowWatcher(
            self.ocr, self.translator, self.cfg,
            hwnd=hwnd, region=region, display_mode=display,
            profile=profile,
            generation_tracker=self.generation_tracker,
        )
        self._watcher = watcher

        # 会话多重守卫：仅当信号来自当前活跃 watcher 且 session 匹配，
        # 且 generation 和 content_revision 仍然有效时才渲染 UI，杜绝 Qt 队列排队滞留信号
        try:
            watcher.subtitle_ready[str, int, int].connect(
                lambda text, gen_id, rev, s=current_session, w=watcher: self._on_watch_subtitle_guarded(text, s, w, gen_id, rev)
            )
        except Exception:
            watcher.subtitle_ready.connect(
                lambda text, s=current_session, w=watcher: self._on_watch_subtitle_guarded(text, s, w)
            )

        try:
            watcher.annotations_ready[list, int, int].connect(
                lambda items, gen_id, rev, s=current_session, w=watcher: self._on_watch_annotations_guarded(items, s, w, gen_id, rev)
            )
        except Exception:
            watcher.annotations_ready.connect(
                lambda items, s=current_session, w=watcher: self._on_watch_annotations_guarded(items, s, w)
            )

        try:
            watcher.history_ready[str, str, str, int, int].connect(
                lambda src, tr, m, g, r, s=current_session, w=watcher: self._on_watch_history_guarded(
                    src, tr, m, s, w, gen_id=g, content_rev=r
                )
            )
        except Exception:
            watcher.history_ready.connect(
                lambda src, tr, m, s=current_session, w=watcher: self._on_watch_history_guarded(src, tr, m, s, w)
            )
        watcher.window_moved.connect(
            lambda x, y, w_, h_, s=current_session, w=watcher: self._on_target_window_moved_guarded(x, y, w_, h_, s, w)
        )
        watcher.content_cleared.connect(
            lambda s=current_session, w=watcher: self._on_watch_content_cleared_guarded(s, w)
        )
        watcher.stopped.connect(
            lambda reason, s=current_session, w=watcher: self._on_watch_stopped_guarded(reason, s, w)
        )
        watcher.start()

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
        # 跟随模式才重贴；自由/固定保持用户拖好的字幕位置
        if self.subtitle.mode == "follow":
            self.subtitle.attach_below(rect, outside=True, match_target_size=True)
        if self.annotate_ctrl.isVisible():
            self.annotate_ctrl.place_above(rect)
        self.log.info("区域识别框更新 %s", rect)

    def _is_active_watch_session(self, session_id: int, watcher: Any) -> bool:
        return (
            session_id == self._watch_session_id
            and self._watcher is not None
            and self._watcher is watcher
        )

    def _on_watch_subtitle_guarded(
        self,
        text: str,
        session_id: int,
        watcher: Any,
        gen_id: int | None = None,
        content_rev: int | None = None,
    ) -> None:
        if not self._is_active_watch_session(session_id, watcher):
            return
        if self._watch_paused or (hasattr(watcher, "_paused") and watcher._paused.is_set()):
            self.log.debug("持续翻译已暂停，丢弃排队晚到的字幕刷新信号")
            return
        if gen_id is not None and hasattr(watcher, "generation_tracker"):
            if not watcher.generation_tracker.is_active(gen_id):
                self.log.debug("丢弃过时 generation 的字幕刷新信号: gen=%s", gen_id)
                return
        if content_rev is not None and hasattr(watcher, "content_revision"):
            if watcher.content_revision != content_rev:
                self.log.debug(
                    "丢弃过时 content_revision 的字幕刷新信号: rev=%s current=%s",
                    content_rev,
                    watcher.content_revision,
                )
                return
        self.subtitle.set_text(text)

    def _on_watch_annotations_guarded(
        self,
        items: list,
        session_id: int,
        watcher: Any,
        gen_id: int | None = None,
        content_rev: int | None = None,
    ) -> None:
        if not self._is_active_watch_session(session_id, watcher):
            return
        if self._watch_paused or (hasattr(watcher, "_paused") and watcher._paused.is_set()):
            self.log.debug("持续翻译已暂停，丢弃排队晚到的逐行备注信号")
            return
        if gen_id is not None and hasattr(watcher, "generation_tracker"):
            if not watcher.generation_tracker.is_active(gen_id):
                self.log.debug("丢弃过时 generation 的逐行备注信号: gen=%s", gen_id)
                return
        if content_rev is not None and hasattr(watcher, "content_revision"):
            if watcher.content_revision != content_rev:
                self.log.debug(
                    "丢弃过时 content_revision 的逐行备注信号: rev=%s current=%s",
                    content_rev,
                    watcher.content_revision,
                )
                return
        self._on_watch_annotations(items)

    def _on_watch_history_guarded(
        self,
        src: str,
        tr: str,
        mode: str,
        session_id: int,
        watcher: Any,
        gen_id: int | None = None,
        content_rev: int | None = None,
    ) -> None:
        if not self._is_active_watch_session(session_id, watcher):
            return
        if self._watch_paused or (hasattr(watcher, "_paused") and watcher._paused.is_set()):
            self.log.debug("持续翻译已暂停，丢弃排队晚到的历史记录信号")
            return
        if gen_id is not None and hasattr(watcher, "generation_tracker"):
            if not watcher.generation_tracker.is_active(gen_id):
                self.log.debug("丢弃过时 generation 的历史记录信号: gen=%s", gen_id)
                return
        if content_rev is not None and hasattr(watcher, "content_revision"):
            if watcher.content_revision != content_rev:
                self.log.debug(
                    "丢弃过时 content_revision 的历史记录信号: rev=%s current=%s",
                    content_rev,
                    watcher.content_revision,
                )
                return
        self._on_watch_history(src, tr, mode)

    def _on_target_window_moved_guarded(self, x: int, y: int, w: int, h: int, session_id: int, watcher: Any) -> None:
        if not self._is_active_watch_session(session_id, watcher):
            return
        self._on_target_window_moved(x, y, w, h)

    def _on_watch_content_cleared_guarded(self, session_id: int, watcher: Any) -> None:
        if not self._is_active_watch_session(session_id, watcher):
            return
        self._on_watch_content_cleared()

    def _on_watch_stopped_guarded(self, reason: str, session_id: int, watcher: Any) -> None:
        if not self._is_active_watch_session(session_id, watcher):
            self.log.info("忽略过时监视会话的 stopped 信号 (session=%s vs %s)", session_id, self._watch_session_id)
            return
        self._on_watch_stopped(reason)

    def _on_watch_annotations(self, items: list) -> None:
        """在主线程绘制备注译文。"""
        if not self._watcher or self._watch_annotate is not True:
            return
        self.annotation.set_items(items)

    def _on_watch_content_cleared(self) -> None:
        """源文字连续消失时同步清空译文，不保留上一条过期内容。"""
        if not self._watcher:
            return
        if self._watch_annotate is True:
            self.annotation.set_items([])
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
        """窗口固定备注；区域支持备注或显示在目标外侧的字幕条。"""
        if getattr(self, "_watch_profile", None) == "window":
            annotate = True
        is_region = getattr(self, "_watch_profile", None) == "region"
        self.region_frame.set_control_bar_external(is_region and annotate)
        self.subtitle.set_region_controls_visible(False)
        self.annotate_ctrl.set_region_controls_visible(is_region and annotate)
        if annotate:
            self.annotate_ctrl.set_paused(self._watch_paused)
            self.annotate_ctrl.set_subtitle_button_visible(is_region)
            self.subtitle.set_interactive(False)
            self.subtitle.hide()
            self.annotation.update_geometry(rect)
            # 首条有效译文到达前保持隐藏，避免空备注层闪现。
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
            # 区域字幕模式默认跟随翻译框，并清除历史自定义尺寸以确保窗口比例一致
            self.subtitle.reset_follow_mode()
            self.cfg["subtitle_mode"] = "follow"
            is_region = self._watch_region is not None or self._watch_profile == "region"
            self.subtitle.set_capture_visible(not is_region)
            if is_region:
                self.subtitle.attach_below(rect, outside=True, match_target_size=True)
            else:
                self.subtitle.attach_below(rect, outside=True, match_target_size=False)
            # PR 1 零闪烁：不在启动时注入占位文本，保持隐藏直到第一帧有效翻译到达
            self.subtitle.hide()

    def _switch_watch_display(self, annotate: bool):
        """区域运行中切换字幕与备注；窗口模式固定为备注。"""
        if self._stopping_watch:
            return
        if self._watcher is None or not self._watcher.isRunning():
            self.log.info("无进行中的持续翻译，忽略显示模式切换")
            return
        annotate = bool(annotate)
        profile = self._watch_profile
        if profile not in ("window", "region"):
            profile = "region" if self._watch_region is not None else "window"
        if profile == "window" and not annotate:
            self.log.info("窗口持续翻译固定为备注模式，忽略字幕切换")
            return
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
        self._watch_annotate = annotate
        # 区域模式保留用户选择；窗口模式不会进入字幕切换分支。
        if profile == "region":
            self.cfg["region_watch_annotate"] = annotate
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
            # 清空旧结果，下一轮收到有效译文时才显示备注层。
            self.annotation.set_items([])
            self.log.info("已切换为备注模式 profile=%s", profile)
        else:
            self.log.info("已切换为字幕条模式 profile=%s", profile)

    def _on_subtitle_mode_changed(self, mode: str):
        """字幕条：跟随 / 自由。跟随模式重新吸附到目标外侧。"""
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
        if self.subtitle.mode == "follow":
            self.subtitle.attach_below(rect, outside=True)
        if self.annotation.isVisible():
            self.annotation.update_geometry(rect)
        if self.annotate_ctrl.isVisible():
            self.annotate_ctrl.place_above(rect)
        # 目标窗 z 序可能变了：窗口备注层贴回目标正上方（仍非全局置顶）
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
            subtitle_geometry = self.subtitle.content_geometry()
            self.cfg["subtitle_geometry"] = list(subtitle_geometry.getRect())
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
        target = str(self.cfg.get("target_language", "简体中文"))
        if hasattr(self, "subtitle"):
            self.subtitle.sync_languages("自动", target)
        self.annotate_ctrl.sync_languages("自动", target)
        self.translate_win.sync_font_size_from_cfg()
        # 备注译文颜色
        try:
            self.annotation.set_text_color(
                self.cfg.get("annotate_text_color", "#00F0FF")
            )
        except Exception:
            pass
        # 目标语言热切换检测：通知 watcher 重置并强制重新翻译当前帧
        new_target = str(self.cfg.get("target_language", "简体中文"))
        target_changed = new_target != getattr(self, "_last_target_language", None)
        if target_changed and hasattr(self, "translator"):
            self.translator.abort_inflight()
            for worker in getattr(self, "_workers", ()):
                worker.requestInterruption()
        if target_changed:
            self._last_target_language = new_target

        translation_changed = False
        if hasattr(self, "translator") and hasattr(self.translator, "desired_signature"):
            signature = self.translator.desired_signature()
            translation_changed = signature != getattr(self, "_applied_translation_signature", signature)
            if translation_changed:
                self.translator.abort_inflight()
                self._translation_config_revision += 1
                revision = self._translation_config_revision
                self._preload_status["llama"] = "pending"
                self._preload_errors["llama"] = ""
                self._sync_runtime_status()
                self._start_preload_thread(
                    f"translation-preload-{revision}",
                    lambda rev=revision: self._load_llama(rev),
                )
        if target_changed and not translation_changed and self.translate_win.isVisible():
            self.translate_win._go()

        if (target_changed or translation_changed) and self._watcher and self._watcher.isRunning():
            self._watcher.on_target_language_changed()

        # 若正在监视：仅当「显示模式」与当前会话不一致时才切换（避免只改颜色/热键也清缓存重译）
        if (
            self._watcher
            and self._watcher.isRunning()
            and self._watch_rect
            and self._watch_profile
        ):
            self._apply_watch_font_size(self._watch_profile)
            if self._watch_profile == "region":
                annotate = bool(self.cfg.get("region_watch_annotate"))
                cur = self._watch_annotate
                if cur is None or bool(cur) != annotate:
                    self._switch_watch_display(annotate)

    def _on_target_language_changed(self, new_lang: str):
        self._on_language_pair_changed("自动", new_lang)

    def _on_language_pair_changed(self, _source: str, target: str):
        """全局同步目标语言，并使所有持续翻译中的旧请求失效。"""
        if target == self.cfg.get("target_language", "简体中文"):
            return
        self.translator.abort_inflight()
        for worker in self._workers:
            worker.requestInterruption()
        self.cfg.pop("source_language", None)
        self.cfg["target_language"] = target
        self._last_target_language = target
        config.save(self.cfg)
        self.translate_win.sync_language_from_cfg()
        self.subtitle.sync_languages("自动", target)
        self.annotate_ctrl.sync_languages("自动", target)
        if self.translate_win.isVisible():
            self.translate_win._go()
        if self._watcher and self._watcher.isRunning():
            self._watcher.on_target_language_changed()

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
                "退出等待 %.1f 秒仍有后台任务，取消进程内翻译请求",
                elapsed,
            )
            self._shutdown_abort_thread = threading.Thread(
                target=self.resources.interrupt_translation,
                daemon=True,
                name="translation-abort",
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
            if not self.resources.close_clients():
                if elapsed >= _SHUTDOWN_HARD_LIMIT_SECONDS:
                    self.log.critical("异步历史写入超过退出上限，数据库连接保持打开")
                    self.qapp.quit()
                    return
                QTimer.singleShot(100, self._poll_shutdown)
                return
            self._shutdown_resources_closed = True
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
