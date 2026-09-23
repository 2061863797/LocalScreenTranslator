# -*- coding: utf-8 -*-
"""功能窗口：设置、翻译历史、统一翻译窗口（输入/划词/截屏共用）。"""

from pathlib import Path
import os
import shutil

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..applog import LOG_PATH, get_log_emitter, recent_lines
from ..paths import (
    RUNTIME_MODELS,
    available_translation_models,
    is_gguf_model,
    resolve_path,
    to_portable_path,
)
from .overlays import _DraggableMixin
from .theme import (
    CornerSizeGrip,
    FLOAT_PANEL_STYLE,
    SETTINGS_STYLE,
    apply_frameless_float,
)
from ..i18n import get_language, set_qt_language, t as _ti
from ..i18n import t_lang
from .topmost import ensure_stays_on_top, raise_to_front, show_toast, topmost_message

LANGUAGES = [
    "简体中文", "繁体中文", "英语", "日语", "韩语", "法语", "德语",
    "俄语", "西班牙语", "葡萄牙语", "意大利语", "泰语", "越南语", "阿拉伯语",
]


class HotkeyEdit(QLineEdit):
    """点击后录入全局热键：键盘组合 或 鼠标侧键（可加 Ctrl/Alt/Shift）。

    存储：键盘如 <alt>+q；侧键如 mouse.x1、<ctrl>+mouse.x2。
    获得焦点时发 recording_changed(True)，便于暂停全局 HotkeyManager。
    """

    recording_changed = Signal(bool)

    _MOD_KEYS = {
        Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta,
    }

    def __init__(self, pynput_str: str):
        super().__init__()
        self.setReadOnly(True)
        self._value = pynput_str
        self.apply_ui_language()
        # 接收侧键（Back/Forward）
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def apply_ui_language(self):
        self.setPlaceholderText(_ti("hk_placeholder"))
        self.setText(self._display(self._value))

    @property
    def value(self) -> str:
        """返回热键配置串。"""
        return self._value

    def set_value(self, pynput_str: str):
        self._value = pynput_str
        self.setText(self._display(pynput_str))

    def focusInEvent(self, event):
        self.setText(_ti("hk_press"))
        self.recording_changed.emit(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.setText(self._display(self._value))
        self.recording_changed.emit(False)
        super().focusOutEvent(event)

    def _mod_parts(self, mods) -> list[str]:
        parts = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            parts.append("<ctrl>")
        if mods & Qt.KeyboardModifier.AltModifier:
            parts.append("<alt>")
        if mods & Qt.KeyboardModifier.ShiftModifier:
            parts.append("<shift>")
        if mods & Qt.KeyboardModifier.MetaModifier:
            parts.append("<cmd>")
        return parts

    def _commit(self, parts: list[str]):
        self._value = "+".join(parts)
        self.setText(self._display(self._value))
        self.clearFocus()

    def mousePressEvent(self, event):
        """录入态下捕获鼠标侧键（X1/X2）。左键仅用于聚焦，不写入。"""
        if not self.hasFocus():
            super().mousePressEvent(event)
            return
        btn = event.button()
        mouse_tok = None
        if btn == Qt.MouseButton.BackButton:
            mouse_tok = "mouse.x1"
        elif btn == Qt.MouseButton.ForwardButton:
            mouse_tok = "mouse.x2"
        if mouse_tok is None:
            # 左/右/中键不作为热键，避免点选输入框时误设
            if btn == Qt.MouseButton.LeftButton:
                super().mousePressEvent(event)
            return
        parts = self._mod_parts(event.modifiers())
        parts.append(mouse_tok)
        self._commit(parts)
        event.accept()

    def keyPressEvent(self, event):
        key = event.key()
        if key in self._MOD_KEYS or key == Qt.Key.Key_unknown:
            return  # 只按了修饰键，等主键
        if key == Qt.Key.Key_Escape:
            self.clearFocus()  # Esc 取消录入
            return
        parts = self._mod_parts(event.modifiers())

        main = self._main_key(key, event.text())
        if main is None:
            return
        if not parts:
            # 无修饰键的裸键盘键太容易误触；鼠标侧键允许单独
            self.setText(_ti("hk_need_mod"))
            return
        parts.append(main)
        self._commit(parts)

    @staticmethod
    def _main_key(key, text: str) -> str | None:
        """Qt 键码 → pynput 主键名。"""
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            return f"<f{key - Qt.Key.Key_F1 + 1}>"
        specials = {
            Qt.Key.Key_Space: "<space>",
            Qt.Key.Key_Tab: "<tab>",
            Qt.Key.Key_Return: "<enter>",
            Qt.Key.Key_Enter: "<enter>",
            Qt.Key.Key_Home: "<home>",
            Qt.Key.Key_End: "<end>",
            Qt.Key.Key_PageUp: "<page_up>",
            Qt.Key.Key_PageDown: "<page_down>",
            Qt.Key.Key_Insert: "<insert>",
            Qt.Key.Key_Delete: "<delete>",
            Qt.Key.Key_Up: "<up>",
            Qt.Key.Key_Down: "<down>",
            Qt.Key.Key_Left: "<left>",
            Qt.Key.Key_Right: "<right>",
        }
        if key in specials:
            return specials[key]
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return chr(key).lower()
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            return chr(key)
        if text and text.isprintable() and not text.isspace():
            return text.lower()
        return None

    @staticmethod
    def _display(pynput_str: str) -> str:
        """配置串 → 人类可读（<alt>+q → Alt+Q；mouse.x1 → 侧键1）。"""
        names = {
            "<ctrl>": "Ctrl", "<alt>": "Alt", "<shift>": "Shift", "<cmd>": "Win",
            "<space>": "Space", "<tab>": "Tab", "<enter>": "Enter",
            "mouse.x1": _ti("mouse_x1"),
            "mouse.x2": _ti("mouse_x2"),
            "mouse.button4": _ti("mouse_x1"),
            "mouse.button5": _ti("mouse_x2"),
            "mouse.back": _ti("mouse_x1"),
            "mouse.forward": _ti("mouse_x2"),
        }
        out = []
        for p in (pynput_str or "").split("+"):
            p = p.strip()
            if not p:
                continue
            pl = p.lower()
            if pl in names:
                out.append(names[pl])
            elif p in names:
                out.append(names[p])
            elif p.startswith("<f") and p.endswith(">"):
                out.append(p[1:-1].upper())
            elif p.startswith("<") and p.endswith(">"):
                out.append(p[1:-1].replace("_", " ").title())
            else:
                out.append(p.upper())
        return "+".join(out) if out else ""


def _settings_card(title: str, hint: str = ""):
    """设置页内容卡片。返回 (card, layout, title_label, hint_label)。"""
    card = QFrame()
    card.setObjectName("card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 12, 14, 14)
    lay.setSpacing(10)
    t = QLabel(title)
    t.setObjectName("cardTitle")
    lay.addWidget(t)
    # 始终创建 hint，便于 i18n 刷新
    h = QLabel(hint or "")
    h.setObjectName("cardHint")
    h.setWordWrap(True)
    lay.addWidget(h)
    return card, lay, t, h


def _form_row(label: str, widget: QWidget):
    """返回 (layout, label_widget)。"""
    row = QHBoxLayout()
    row.setSpacing(12)
    lab = QLabel(label)
    lab.setMinimumWidth(120)
    lab.setStyleSheet("color:rgba(255,255,255,200);")
    row.addWidget(lab)
    row.addWidget(widget, stretch=1)
    return row, lab


def _font_size_combo() -> QComboBox:
    """默认值保留旧字号；其余选项为明确的像素字号。"""
    combo = QComboBox()
    combo.addItem("", 0)
    for size in range(12, 21):
        combo.addItem(f"{size} px", size)
    return combo


def _max_tokens_combo() -> QComboBox:
    """明确的预设下拉框；自定义值使用旁边的数值框。"""
    combo = QComboBox()
    for value in (64, 128, 256, 512, 1024, 2048, 4096, 8192):
        combo.addItem(str(value), value)
    combo.addItem("自定义…", "custom")
    combo.setMinimumWidth(150)
    return combo


_MONITOR_INTERVAL_PRESETS = (200, 500, 800, 1000, 1500, 2000, 3000, 5000)


def _monitor_interval_controls() -> tuple[QComboBox, QSpinBox, QWidget]:
    """监控间隔预设；非预设值由旁边的数值框承接。"""
    combo = QComboBox()
    for value in _MONITOR_INTERVAL_PRESETS:
        combo.addItem(str(value), value)
    combo.addItem("自定义…", "custom")
    combo.setMinimumWidth(150)

    custom = QSpinBox()
    custom.setRange(200, 5000)
    custom.setSingleStep(100)
    custom.setVisible(False)

    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(combo)
    layout.addWidget(custom)
    layout.addStretch(1)
    combo.currentIndexChanged.connect(
        lambda _index: custom.setVisible(combo.currentData() == "custom")
    )
    return combo, custom, widget


def _set_monitor_interval(combo: QComboBox, custom: QSpinBox, value) -> None:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = 800
    index = combo.findData(interval)
    if index >= 0:
        combo.setCurrentIndex(index)
    else:
        combo.setCurrentIndex(combo.findData("custom"))
        custom.setValue(interval)
    custom.setVisible(combo.currentData() == "custom")


def _monitor_interval_value(combo: QComboBox, custom: QSpinBox) -> int:
    value = combo.currentData()
    return custom.value() if value == "custom" else int(value)


def _set_combo_data(combo: QComboBox, value, fallback=0) -> None:
    index = combo.findData(value)
    if index < 0:
        try:
            index = combo.findData(int(value))
        except (TypeError, ValueError):
            index = -1
    if index < 0:
        index = combo.findData(fallback)
    combo.setCurrentIndex(max(0, index))


class ModelCopyWorker(QThread):
    """大模型文件后台异步复制工作线程，避免 UI 主线程发生数秒至数十秒假死。"""
    progress = Signal(int)
    copy_finished = Signal(bool, str, str)  # success, target_path_str, error_msg

    def __init__(self, src_path: Path, dest_path: Path, parent=None):
        super().__init__(parent)
        self.src_path = src_path
        self.dest_path = dest_path

    def run(self):
        # 临时文件保持 .gguf 后缀（避免校验工具因扩展名拒认），拷贝完成后原子替换
        temp_dest = self.dest_path.with_name(f"{self.dest_path.stem}.importing.gguf")
        try:
            self.dest_path.parent.mkdir(parents=True, exist_ok=True)
            total_bytes = self.src_path.stat().st_size
            copied_bytes = 0
            chunk_size = 4 * 1024 * 1024  # 4MB 分块
            with open(self.src_path, "rb") as fsrc, open(temp_dest, "wb") as fdst:
                while True:
                    if self.isInterruptionRequested():
                        break
                    buf = fsrc.read(chunk_size)
                    if not buf:
                        break
                    fdst.write(buf)
                    copied_bytes += len(buf)
                    if total_bytes > 0:
                        self.progress.emit(int(copied_bytes * 100 / total_bytes))

            if self.isInterruptionRequested():
                if temp_dest.exists():
                    try:
                        temp_dest.unlink()
                    except Exception:
                        pass
                self.copy_finished.emit(False, "", "复制已被取消")
                return

            if not is_gguf_model(temp_dest):
                if temp_dest.exists():
                    temp_dest.unlink()
                self.copy_finished.emit(False, "", "拷贝后模型文件头校验失败（非合法 GGUF）")
                return

            os.replace(temp_dest, self.dest_path)
            self.copy_finished.emit(True, str(self.dest_path), "")
        except Exception as e:
            try:
                if temp_dest.exists():
                    temp_dest.unlink()
            except Exception:
                pass
            self.copy_finished.emit(False, "", str(e))


class SettingsWindow(_DraggableMixin, QWidget):
    """高级设置：侧栏分类 + 卡片内容；支持中/英界面。"""

    def __init__(self, cfg: dict, on_saved=None, hotkey_manager=None,
                 on_retry_runtime=None):
        super().__init__()
        apply_frameless_float(self)
        self.resize(780, 560)
        self.setMinimumSize(640, 440)
        ensure_stays_on_top(self)
        self._cfg = cfg
        self._on_saved = on_saved
        self._hotkey_manager = hotkey_manager
        self._on_retry_runtime = on_retry_runtime
        self._runtime_state = {"llama": ("pending", ""), "ocr": ("pending", "")}
        self._log_auto_scroll = True
        self._lang = "en" if str(cfg.get("ui_language", "zh")).lower().startswith("en") else "zh"

        self._ui_lang = QComboBox()
        self._ui_lang.addItem("中文", "zh")
        self._ui_lang.addItem("English", "en")
        self._ui_lang.setCurrentIndex(1 if self._lang == "en" else 0)

        self._target = QComboBox()
        self._target.addItems(LANGUAGES)
        self._translation_font_size = _font_size_combo()
        self._history_enabled = QCheckBox()
        self._translation_cache_enabled = QCheckBox()
        self._history_privacy_tip = QLabel()
        self._history_privacy_tip.setWordWrap(True)
        self._history_privacy_tip.setStyleSheet("color:#aaa;font-size:12px;")
        self._runtime_title = QLabel()
        self._runtime_title.setStyleSheet("font-weight:600;")
        self._runtime_text = QLabel()
        self._runtime_text.setWordWrap(True)
        self._runtime_retry = QPushButton()
        self._runtime_retry.clicked.connect(
            lambda: self._on_retry_runtime() if self._on_retry_runtime else None
        )

        self._hk_shot = HotkeyEdit(cfg["hotkey_screenshot"])
        self._hk_word = HotkeyEdit(cfg["hotkey_word"])
        self._hk_win = HotkeyEdit(cfg["hotkey_window"])
        self._hk_region = HotkeyEdit(cfg["hotkey_region_watch"])
        self._hk_edits = (
            self._hk_shot,
            self._hk_word,
            self._hk_win,
            self._hk_region,
        )
        for hk in self._hk_edits:
            hk.recording_changed.connect(self._on_hotkey_recording)

        (
            self._win_interval,
            self._win_interval_custom,
            self._win_interval_widget,
        ) = _monitor_interval_controls()
        self._win_font_size = _font_size_combo()
        self._win_annotate = QComboBox()
        self._win_annotate.addItem("", False)
        self._win_annotate.addItem("", True)
        (
            self._reg_interval,
            self._reg_interval_custom,
            self._reg_interval_widget,
        ) = _monitor_interval_controls()
        self._reg_font_size = _font_size_combo()
        self._reg_annotate = QComboBox()
        self._reg_annotate.addItem("", False)
        self._reg_annotate.addItem("", True)

        self._win_skip_target = QCheckBox()
        self._reg_skip_target = QCheckBox()

        self._ann_color = QLineEdit()
        self._ann_color.setPlaceholderText("#00F0FF")
        self._ann_color.setMaxLength(9)
        self._ann_color.setFixedWidth(100)
        self._ann_color_btn = QPushButton()
        self._ann_color_btn.setFixedWidth(52)
        self._ann_color_btn.clicked.connect(self._pick_annotate_color)
        self._ann_color_swatch = QLabel()
        self._ann_color_swatch.setFixedSize(22, 22)
        self._ann_color.textChanged.connect(self._refresh_color_swatch)
        self._ann_capture_visible = QCheckBox()

        self._max_tokens = _max_tokens_combo()
        self._max_tokens_custom = QSpinBox()
        self._max_tokens_custom.setRange(64, 8192)
        self._max_tokens_custom.setValue(512)
        self._max_tokens_custom.setVisible(False)
        self._max_tokens_presets = QPushButton()
        self._max_tokens_presets.setObjectName("ghostBtn")
        self._max_tokens_presets.clicked.connect(self._max_tokens.showPopup)
        self._max_tokens.currentIndexChanged.connect(self._on_max_tokens_choice)
        self._max_tokens_widget = QWidget()
        max_tokens_layout = QHBoxLayout(self._max_tokens_widget)
        max_tokens_layout.setContentsMargins(0, 0, 0, 0)
        max_tokens_layout.setSpacing(8)
        max_tokens_layout.addWidget(self._max_tokens)
        max_tokens_layout.addWidget(self._max_tokens_presets)
        max_tokens_layout.addWidget(self._max_tokens_custom)
        max_tokens_layout.addStretch(1)

        self._model_file = QComboBox()
        self._model_file.setMinimumContentsLength(24)
        self._btn_import_model = QPushButton()
        self._btn_import_model.clicked.connect(self._on_import_model)
        self._model_note = QLabel()
        self._model_note.setWordWrap(True)
        self._model_note.setStyleSheet("color:#aaa;font-size:12px;")
        self._llama_device = QComboBox()
        for value in ("auto", "gpu", "cpu"):
            self._llama_device.addItem("", value)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._page_general())
        self._stack.addWidget(self._page_hotkeys())
        self._stack.addWidget(self._page_window())
        self._stack.addWidget(self._page_region())
        self._stack.addWidget(self._page_advanced())
        self._stack.addWidget(self._page_log())

        nav = QFrame()
        nav.setObjectName("sideNav")
        nav.setFixedWidth(148)
        nav_lay = QVBoxLayout(nav)
        nav_lay.setContentsMargins(6, 6, 6, 6)
        nav_lay.setSpacing(4)
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_btns: list[QPushButton] = []
        for i in range(6):
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if i == 0:
                btn.setChecked(True)
            self._nav_group.addButton(btn, i)
            self._nav_btns.append(btn)
            nav_lay.addWidget(btn)
        nav_lay.addStretch()
        self._nav_group.idClicked.connect(self._stack.setCurrentIndex)

        title_bar = QWidget()
        title_bar.setObjectName("titleBar")
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(4, 0, 0, 0)
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self._title_lbl = QLabel()
        self._title_lbl.setObjectName("titleLabel")
        self._sub_lbl = QLabel()
        self._sub_lbl.setObjectName("subtitle")
        title_col.addWidget(self._title_lbl)
        title_col.addWidget(self._sub_lbl)
        tb.addLayout(title_col)
        tb.addStretch()
        self._btn_close = QPushButton("×")
        self._btn_close.setObjectName("closeBtn")
        self._btn_close.setFixedSize(32, 28)
        self._btn_close.clicked.connect(self.hide)
        tb.addWidget(self._btn_close)

        footer = QWidget()
        footer.setObjectName("footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(0, 10, 0, 0)
        foot.addStretch()
        self._btn_save = QPushButton()
        self._btn_save.setObjectName("primaryBtn")
        self._btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_save.clicked.connect(self._save)
        foot.addWidget(self._btn_save)
        foot.addSpacing(4)
        foot.addWidget(CornerSizeGrip(footer), 0, Qt.AlignmentFlag.AlignBottom)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(nav)
        body.addWidget(self._stack, stretch=1)

        container = QWidget()
        container.setObjectName("panel")
        inner = QVBoxLayout(container)
        inner.setContentsMargins(16, 14, 16, 12)
        inner.setSpacing(12)
        inner.addWidget(title_bar)
        inner.addLayout(body, stretch=1)
        inner.addWidget(footer)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self.setStyleSheet(SETTINGS_STYLE)

        get_log_emitter().line.connect(
            self._append_log_line, Qt.ConnectionType.QueuedConnection
        )
        self.reload_from_cfg()
        self._apply_i18n()
        self._ui_lang.currentIndexChanged.connect(self._preview_ui_language)
        self._load_recent_logs()

    def _tr(self, key: str, **kwargs) -> str:
        return t_lang(self._lang, key, **kwargs)

    def _preview_ui_language(self):
        """在设置页即时预览所选语言，保存后再写入全局配置。"""
        lang = self._ui_lang.currentData() or "zh"
        self._lang = "en" if str(lang).lower().startswith("en") else "zh"
        self._apply_i18n()

    def _apply_i18n(self):
        tr = self._tr
        self.setWindowTitle(tr("win_title"))
        self._title_lbl.setText(tr("title"))
        self._sub_lbl.setText(tr("subtitle"))
        self._btn_close.setToolTip(tr("close"))
        self._btn_save.setText(tr("save"))
        nav_keys = (
            "nav_general", "nav_hotkeys", "nav_window",
            "nav_region", "nav_advanced", "nav_log",
        )
        for btn, k in zip(self._nav_btns, nav_keys):
            btn.setText(tr(k))

        self._ui_lang.blockSignals(True)
        self._ui_lang.setItemText(0, tr("lang_zh"))
        self._ui_lang.setItemText(1, tr("lang_en"))
        self._ui_lang.blockSignals(False)

        self._lab_ui_lang.setText(tr("ui_lang"))
        self._hint_ui_lang.setText(tr("ui_lang_hint"))
        self._card_general_title.setText(tr("card_general"))
        self._card_general_hint.setText(tr("card_general_hint"))
        self._lab_target.setText(tr("target_lang"))
        self._lab_translation_font_size.setText(tr("translate_font_size"))
        self._translation_font_size.setItemText(0, tr("font_size_default"))
        self._translation_font_size.setToolTip(tr("translate_font_size_tip"))
        self._history_enabled.setText(tr("history_enabled"))
        self._translation_cache_enabled.setText(tr("translation_cache_enabled"))
        self._history_privacy_tip.setText(tr("history_privacy_tip"))
        self._runtime_title.setText(tr("runtime_status"))
        self._runtime_retry.setText(tr("runtime_retry"))
        self._refresh_runtime_status()
        self._lab_ann_color.setText(tr("ann_color"))
        self._ann_color.setToolTip(tr("ann_color_tip"))
        self._ann_color_btn.setText(tr("ann_color_pick"))
        self._ann_color_swatch.setToolTip(tr("ann_color_preview"))
        self._ann_color_note.setText(tr("ann_color_note"))
        self._ann_capture_visible.setText(tr("ann_capture_visible"))
        self._ann_capture_tip.setText(tr("ann_capture_visible_tip"))

        self._card_hk_title.setText(tr("card_hotkeys"))
        self._card_hk_hint.setText(tr("card_hotkeys_hint"))
        self._lab_hk_shot.setText(tr("hk_shot"))
        self._lab_hk_word.setText(tr("hk_word"))
        self._lab_hk_win.setText(tr("hk_win"))
        self._lab_hk_region.setText(tr("hk_region"))

        self._card_win_title.setText(tr("card_window"))
        self._card_win_hint.setText(tr("card_window_hint"))
        self._lab_win_interval.setText(tr("interval"))
        self._lab_win_font_size.setText(tr("watch_font_size"))
        self._win_font_size.setItemText(0, tr("font_size_default"))
        self._win_font_size.setToolTip(tr("watch_font_size_tip"))
        self._lab_win_mode.setText(tr("display_mode"))
        self._apply_interval_i18n(self._win_interval, self._win_interval_custom)
        wi = self._win_annotate.currentIndex()
        self._win_annotate.setItemText(0, tr("mode_sub_win"))
        self._win_annotate.setItemText(1, tr("mode_ann_win"))
        self._win_annotate.setCurrentIndex(wi)
        self._win_annotate.setToolTip(tr("tip_win_mode"))
        self._card_win_ann_title.setText(tr("card_win_ann"))
        self._card_win_ann_hint.setText(tr("card_win_ann_hint"))
        self._win_skip_target.setText(tr("skip_target"))
        self._win_skip_target.setToolTip(tr("skip_win_tip"))

        self._card_reg_title.setText(tr("card_region"))
        self._card_reg_hint.setText(tr("card_region_hint"))
        self._lab_reg_interval.setText(tr("interval"))
        self._lab_reg_font_size.setText(tr("watch_font_size"))
        self._reg_font_size.setItemText(0, tr("font_size_default"))
        self._reg_font_size.setToolTip(tr("watch_font_size_tip"))
        self._lab_reg_mode.setText(tr("display_mode"))
        self._apply_interval_i18n(self._reg_interval, self._reg_interval_custom)
        ri = self._reg_annotate.currentIndex()
        self._reg_annotate.setItemText(0, tr("mode_sub_reg"))
        self._reg_annotate.setItemText(1, tr("mode_ann_reg"))
        self._reg_annotate.setCurrentIndex(ri)
        self._reg_annotate.setToolTip(tr("tip_reg_mode"))
        self._card_reg_ann_title.setText(tr("card_reg_ann"))
        self._card_reg_ann_hint.setText(tr("card_reg_ann_hint"))
        self._reg_skip_target.setText(tr("skip_target"))
        self._reg_skip_target.setToolTip(tr("skip_reg_tip"))

        self._card_adv_title.setText(tr("card_advanced"))
        self._card_adv_hint.setText(tr("card_advanced_hint"))
        self._lab_model_file.setText(tr("model_file"))
        self._btn_import_model.setText("导入模型" if self._lang == "zh" else "Import Model")
        self._btn_import_model.setToolTip(
            "选择本地 .gguf 模型文件并自动导入至用户模型目录"
            if self._lang == "zh"
            else "Select a local .gguf model and import to user models directory"
        )
        self._model_file.setToolTip(
            tr("model_file_tip", directory=to_portable_path(RUNTIME_MODELS))
        )
        self._model_note.setText(
            tr("model_file_note", directory=to_portable_path(RUNTIME_MODELS))
        )
        self._lab_llama_device.setText(tr("llama_device"))
        for index, key in enumerate(("llama_device_auto", "llama_device_gpu", "llama_device_cpu")):
            self._llama_device.setItemText(index, tr(key))
        self._llama_device.setToolTip(tr("llama_device_tip"))
        self._lab_max_tokens.setText("max_tokens")
        self._max_tokens.setToolTip(tr("max_tokens_tip"))
        self._max_tokens_custom.setToolTip(tr("max_tokens_tip"))
        self._max_tokens_presets.setText(tr("max_tokens_presets"))
        recommended = self._max_tokens.findData(512)
        custom = self._max_tokens.findData("custom")
        if recommended >= 0:
            self._max_tokens.setItemText(recommended, tr("max_tokens_recommended"))
        if custom >= 0:
            self._max_tokens.setItemText(custom, tr("max_tokens_custom"))

        self._card_log_title.setText(tr("card_log"))
        self._card_log_hint.setText(tr("card_log_hint", log=LOG_PATH.name))
        self._log_view.setPlaceholderText(tr("log_placeholder"))
        self._btn_log_bottom.setText(tr("log_bottom"))
        self._btn_log_clear.setText(tr("log_clear"))
        self._btn_log_open.setText(tr("log_open"))

    def _wrap_scroll(self, *widgets: QWidget) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 2, 0)
        lay.setSpacing(12)
        for w in widgets:
            lay.addWidget(w)
        lay.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        scroll.setWidget(page)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(scroll)
        return wrap

    def _apply_interval_i18n(self, combo: QComboBox, custom: QSpinBox):
        suffix = self._tr("ms_suffix")
        for index in range(combo.count()):
            value = combo.itemData(index)
            combo.setItemText(
                index,
                self._tr("interval_custom")
                if value == "custom"
                else f"{value}{suffix}",
            )
        custom.setSuffix(suffix)

    def _annotate_color_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self._lab_ann_color = QLabel()
        self._lab_ann_color.setFixedWidth(100)
        row.addWidget(self._lab_ann_color)
        row.addWidget(self._ann_color_swatch)
        row.addWidget(self._ann_color)
        row.addWidget(self._ann_color_btn)
        row.addStretch(1)
        return row

    def _normalize_hex_color(self, text: str) -> str:
        s = (text or "").strip()
        if not s:
            return "#00F0FF"
        if not s.startswith("#"):
            s = "#" + s
        c = QColor(s)
        if not c.isValid():
            return "#00F0FF"
        return c.name(QColor.NameFormat.HexRgb).upper()

    def _refresh_color_swatch(self, _text: str = ""):
        hex_c = self._normalize_hex_color(self._ann_color.text())
        self._ann_color_swatch.setStyleSheet(
            f"background:{hex_c};border:1px solid #888;border-radius:3px;"
        )

    def _pick_annotate_color(self):
        cur = QColor(self._normalize_hex_color(self._ann_color.text()))
        set_qt_language(self._lang)
        dialog = QColorDialog(cur, self)
        dialog.setWindowTitle(self._tr("ann_color_dlg"))
        dialog.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        blank_icon = QPixmap(1, 1)
        blank_icon.fill(Qt.GlobalColor.transparent)
        dialog.setWindowIcon(QIcon(blank_icon))
        try:
            accepted = dialog.exec() == QColorDialog.DialogCode.Accepted
        finally:
            set_qt_language(get_language())
        if accepted:
            c = dialog.currentColor()
            self._ann_color.setText(c.name(QColor.NameFormat.HexRgb).upper())
        dialog.deleteLater()

    def _page_general(self) -> QWidget:
        card, lay, self._card_general_title, self._card_general_hint = _settings_card("", "")
        r1, self._lab_ui_lang = _form_row("", self._ui_lang)
        lay.addLayout(r1)
        self._hint_ui_lang = QLabel()
        self._hint_ui_lang.setWordWrap(True)
        self._hint_ui_lang.setStyleSheet("color:#aaa;font-size:12px;")
        lay.addWidget(self._hint_ui_lang)
        r2, self._lab_target = _form_row("", self._target)
        lay.addLayout(r2)
        r3, self._lab_translation_font_size = _form_row(
            "", self._translation_font_size
        )
        lay.addLayout(r3)
        lay.addWidget(self._history_enabled)
        lay.addWidget(self._translation_cache_enabled)
        lay.addWidget(self._history_privacy_tip)
        lay.addWidget(self._runtime_title)
        lay.addWidget(self._runtime_text)
        lay.addWidget(self._runtime_retry, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addLayout(self._annotate_color_row())
        self._ann_color_note = QLabel()
        self._ann_color_note.setWordWrap(True)
        self._ann_color_note.setStyleSheet("color:#aaa;font-size:12px;")
        lay.addWidget(self._ann_color_note)
        lay.addWidget(self._ann_capture_visible)
        self._ann_capture_tip = QLabel()
        self._ann_capture_tip.setWordWrap(True)
        self._ann_capture_tip.setStyleSheet("color:#aaa;font-size:12px;")
        lay.addWidget(self._ann_capture_tip)
        return self._wrap_scroll(card)

    def set_runtime_status(self, state: dict):
        self._runtime_state = dict(state)
        self._refresh_runtime_status()

    def _refresh_runtime_status(self):
        if not hasattr(self, "_runtime_text"):
            return
        labels = {"llama": self._tr("runtime_llama"), "ocr": self._tr("runtime_ocr")}
        state_keys = {
            "pending": "runtime_pending", "ok": "runtime_ok", "fail": "runtime_fail"
        }
        lines = []
        failed = False
        for key in ("llama", "ocr"):
            value = self._runtime_state.get(key, ("pending", ""))
            status, detail = value if isinstance(value, tuple) else (value, "")
            failed = failed or status == "fail"
            line = f"{labels[key]}：{self._tr(state_keys.get(status, 'runtime_pending'))}"
            if detail:
                line += f"（{detail}）"
            lines.append(line)
        self._runtime_text.setText("\n".join(lines))
        self._runtime_retry.setVisible(failed)

    def _page_hotkeys(self) -> QWidget:
        card, lay, self._card_hk_title, self._card_hk_hint = _settings_card("", "")
        r1, self._lab_hk_shot = _form_row("", self._hk_shot)
        r2, self._lab_hk_word = _form_row("", self._hk_word)
        r3, self._lab_hk_win = _form_row("", self._hk_win)
        r4, self._lab_hk_region = _form_row("", self._hk_region)
        for r in (r1, r2, r3, r4):
            lay.addLayout(r)
        return self._wrap_scroll(card)

    def _page_window(self) -> QWidget:
        card, lay, self._card_win_title, self._card_win_hint = _settings_card("", "")
        r1, self._lab_win_interval = _form_row("", self._win_interval_widget)
        r2, self._lab_win_font_size = _form_row("", self._win_font_size)
        r3, self._lab_win_mode = _form_row("", self._win_annotate)
        lay.addLayout(r1)
        lay.addLayout(r2)
        lay.addLayout(r3)
        tip_card, tip_lay, self._card_win_ann_title, self._card_win_ann_hint = _settings_card("", "")
        tip_lay.addWidget(self._win_skip_target)
        return self._wrap_scroll(card, tip_card)

    def _page_region(self) -> QWidget:
        card, lay, self._card_reg_title, self._card_reg_hint = _settings_card("", "")
        r1, self._lab_reg_interval = _form_row("", self._reg_interval_widget)
        r2, self._lab_reg_font_size = _form_row("", self._reg_font_size)
        r3, self._lab_reg_mode = _form_row("", self._reg_annotate)
        lay.addLayout(r1)
        lay.addLayout(r2)
        lay.addLayout(r3)
        tip_card, tip_lay, self._card_reg_ann_title, self._card_reg_ann_hint = _settings_card("", "")
        tip_lay.addWidget(self._reg_skip_target)
        return self._wrap_scroll(card, tip_card)

    def _page_advanced(self) -> QWidget:
        card, lay, self._card_adv_title, self._card_adv_hint = _settings_card("", "")
        model_row_widget = QWidget()
        model_row_lay = QHBoxLayout(model_row_widget)
        model_row_lay.setContentsMargins(0, 0, 0, 0)
        model_row_lay.setSpacing(6)
        model_row_lay.addWidget(self._model_file, 1)
        model_row_lay.addWidget(self._btn_import_model, 0)
        r0, self._lab_model_file = _form_row("", model_row_widget)
        r1, self._lab_llama_device = _form_row("", self._llama_device)
        r2, self._lab_max_tokens = _form_row("max_tokens", self._max_tokens_widget)
        lay.addLayout(r0)
        lay.addWidget(self._model_note)
        lay.addLayout(r1)
        lay.addLayout(r2)
        return self._wrap_scroll(card)

    def _page_log(self) -> QWidget:
        card, lay, self._card_log_title, self._card_log_hint = _settings_card("", "")
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        self._log_view.setMinimumHeight(280)
        self._log_view.verticalScrollBar().valueChanged.connect(self._on_log_scroll)
        bar = QHBoxLayout()
        self._btn_log_bottom = QPushButton()
        self._btn_log_bottom.setObjectName("ghostBtn")
        self._btn_log_bottom.clicked.connect(self._scroll_log_bottom)
        self._btn_log_clear = QPushButton()
        self._btn_log_clear.setObjectName("ghostBtn")
        self._btn_log_clear.clicked.connect(self._log_view.clear)
        self._btn_log_open = QPushButton()
        self._btn_log_open.setObjectName("ghostBtn")
        self._btn_log_open.clicked.connect(self._open_log_file)
        bar.addWidget(self._btn_log_bottom)
        bar.addWidget(self._btn_log_clear)
        bar.addWidget(self._btn_log_open)
        bar.addStretch()
        lay.addLayout(bar)
        lay.addWidget(self._log_view, stretch=1)
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(card)
        return page

    def showEvent(self, event):
        self.reload_from_cfg()
        self._apply_i18n()
        self._load_recent_logs()
        super().showEvent(event)

    def hideEvent(self, event):
        # 关窗时强制恢复全局热键，避免录入焦点丢失导致一直 pause
        hm = self._hotkey_manager
        if hm is not None:
            try:
                hm.resume()
            except Exception:
                pass
        super().hideEvent(event)

    def _load_recent_logs(self):
        lines = recent_lines(500)
        self._log_view.setPlainText("\n".join(lines))
        self._scroll_log_bottom()

    def _append_log_line(self, line: str):
        self._log_view.appendPlainText(line)
        if self._log_auto_scroll:
            self._scroll_log_bottom()

    def _scroll_log_bottom(self):
        self._log_auto_scroll = True
        bar = self._log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_log_scroll(self, value: int):
        bar = self._log_view.verticalScrollBar()
        self._log_auto_scroll = value >= bar.maximum() - 4

    def _open_log_file(self):
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if not LOG_PATH.exists():
                LOG_PATH.write_text("", encoding="utf-8")
            import os

            os.startfile(str(LOG_PATH))  # type: ignore[attr-defined]
        except Exception as e:
            topmost_message(
                "information",
                self._tr("log_title"),
                self._tr("log_open_fail", path=str(LOG_PATH), err=e),
                parent=self,
            )

    def reload_from_cfg(self):
        cfg = self._cfg
        self._lang = "en" if str(cfg.get("ui_language", "zh")).lower().startswith("en") else "zh"
        self._ui_lang.blockSignals(True)
        self._ui_lang.setCurrentIndex(1 if self._lang == "en" else 0)
        self._ui_lang.blockSignals(False)
        self._target.setCurrentText(cfg.get("target_language", "简体中文"))
        _set_combo_data(
            self._translation_font_size,
            cfg.get("translate_window_font_size", 0),
        )
        self._history_enabled.setChecked(bool(cfg.get("history_enabled", True)))
        self._translation_cache_enabled.setChecked(
            bool(cfg.get("translation_cache_enabled", True))
        )
        self._hk_shot.set_value(cfg["hotkey_screenshot"])
        self._hk_word.set_value(cfg["hotkey_word"])
        self._hk_win.set_value(cfg["hotkey_window"])
        self._hk_region.set_value(cfg["hotkey_region_watch"])
        _set_monitor_interval(
            self._win_interval,
            self._win_interval_custom,
            cfg.get("window_watch_interval_ms", 800),
        )
        _set_combo_data(self._win_font_size, cfg.get("window_watch_font_size", 0))
        self._win_annotate.setCurrentIndex(
            1 if cfg.get("window_watch_annotate") else 0
        )
        _set_monitor_interval(
            self._reg_interval,
            self._reg_interval_custom,
            cfg.get("region_watch_interval_ms", 800),
        )
        _set_combo_data(self._reg_font_size, cfg.get("region_watch_font_size", 0))
        self._reg_annotate.setCurrentIndex(
            1 if cfg.get("region_watch_annotate") else 0
        )
        self._win_skip_target.setChecked(
            bool(cfg.get("window_annotate_skip_target_lang"))
        )
        self._reg_skip_target.setChecked(
            bool(cfg.get("region_annotate_skip_target_lang"))
        )
        self._ann_color.setText(
            self._normalize_hex_color(str(cfg.get("annotate_text_color", "#00F0FF")))
        )
        self._refresh_color_swatch()
        self._ann_capture_visible.setChecked(
            bool(cfg.get("annotate_capture_visible"))
        )
        self._reload_model_choices()
        _set_combo_data(self._llama_device, cfg.get("llama_device", "auto"))
        max_tokens = int(cfg.get("max_tokens", 512))
        max_tokens_index = self._max_tokens.findData(max_tokens)
        if max_tokens_index >= 0:
            self._max_tokens.setCurrentIndex(max_tokens_index)
        else:
            self._max_tokens.setCurrentIndex(self._max_tokens.findData("custom"))
            self._max_tokens_custom.setValue(max_tokens)
        self._on_max_tokens_choice()

    def _on_max_tokens_choice(self, _index: int = -1):
        self._max_tokens_custom.setVisible(
            self._max_tokens.currentData() == "custom"
        )

    @staticmethod
    def _model_path_key(value: str) -> str:
        try:
            return str(resolve_path(value)).casefold()
        except (OSError, ValueError):
            return str(value).strip().casefold()

    def _reload_model_choices(self, select_path: str | None = None):
        """每次打开设置都重新扫描 runtime/models，下载或导入后无需重启即可看到列表。"""
        current = select_path if select_path else str(self._cfg.get("model_path") or "").strip()
        current_key = self._model_path_key(current) if current else ""
        models = available_translation_models()

        self._model_file.blockSignals(True)
        self._model_file.clear()
        current_index = -1
        for model in models:
            portable = to_portable_path(model)
            self._model_file.addItem(model.name, portable)
            if self._model_path_key(portable) == current_key:
                current_index = self._model_file.count() - 1

        # 兼容旧配置中的外部路径或已经丢失的文件；不在保存其它设置时擅自改模型。
        if current and current_index < 0:
            current_path = resolve_path(current)
            if is_gguf_model(current_path):
                label = self._tr("model_current_external", name=current_path.name)
            else:
                label = self._tr("model_current_missing", name=current_path.name)
            self._model_file.insertItem(0, label, to_portable_path(current))
            current_index = 0

        if self._model_file.count() == 0:
            self._model_file.addItem(self._tr("model_none"), "")
            self._model_file.setEnabled(False)
            current_index = 0
        else:
            self._model_file.setEnabled(True)
            if current_index < 0:
                current_index = 0
        self._model_file.setCurrentIndex(current_index)
        self._model_file.blockSignals(False)

    def _on_import_model(self):
        """支持用户从任意位置选择 .gguf 模型文件（支持直接引用外部路径或后台复制到 USER_MODELS）。"""
        from ..paths import USER_MODELS, RUNTIME_MODELS, is_gguf_model, to_portable_path

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 GGUF 模型文件" if self._lang == "zh" else "Select GGUF Model File",
            "",
            "GGUF 模型 (*.gguf);;所有文件 (*.*)" if self._lang == "zh" else "GGUF Model (*.gguf);;All Files (*.*)",
        )
        if not file_path:
            return
        p = Path(file_path)
        if not is_gguf_model(p):
            topmost_message(
                "warning",
                self._tr("title_warning"),
                "所选文件不是合法的 GGUF 模型文件。" if self._lang == "zh" else "Selected file is not a valid GGUF model.",
                parent=self,
            )
            return

        dest = USER_MODELS / p.name
        # 若已经在标准模型目录中：直接高亮选中
        if p.resolve() == dest.resolve() or p.parent.resolve() == RUNTIME_MODELS.resolve():
            self._reload_model_choices(select_path=to_portable_path(p))
            topmost_message(
                "information",
                self._tr("title_info"),
                f"已选择模型: {p.name}，点击“保存”后将提示重启生效。"
                if self._lang == "zh"
                else f"Selected model: {p.name}. Click 'Save' to apply restart.",
                parent=self,
            )
            return

        # 针对外部模型，提供两种友好模式：直接使用（零等待）与后台复制（自包含）
        size_mb = p.stat().st_size // (1024 * 1024) if p.exists() else 0
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("导入外部模型" if self._lang == "zh" else "Import External Model")
        msg_box.setText(
            f"已选择外部模型：{p.name} ({size_mb} MB)\n\n请选择使用方式："
            if self._lang == "zh"
            else f"External model selected: {p.name} ({size_mb} MB)\n\nChoose import mode:"
        )
        btn_direct = msg_box.addButton(
            "直接使用此路径" if self._lang == "zh" else "Use External Path Directly",
            QMessageBox.ButtonRole.AcceptRole,
        )
        btn_copy = msg_box.addButton(
            "复制到模型目录" if self._lang == "zh" else "Copy to Models Directory",
            QMessageBox.ButtonRole.ActionRole,
        )
        msg_box.addButton(QMessageBox.StandardButton.Cancel)

        msg_box.exec()
        clicked = msg_box.clickedButton()
        if clicked == btn_direct:
            # 直接使用外部路径（零等待，不复制几 GB 文件）
            self._reload_model_choices(select_path=to_portable_path(p))
            topmost_message(
                "information",
                self._tr("title_info"),
                f"已直接引用模型: {p.name}，点击“保存”后将提示重启生效。"
                if self._lang == "zh"
                else f"Directly referencing model: {p.name}. Click 'Save' to apply restart.",
                parent=self,
            )
            return
        elif clicked == btn_copy:
            if dest.exists():
                answer = QMessageBox.question(
                    self,
                    self._tr("title_warning"),
                    f"模型目录中已有同名文件 {dest.name}。确定替换吗？"
                    if self._lang == "zh"
                    else f"A model named {dest.name} already exists. Replace it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            # 后台线程异步复制，彻底消除 UI 假死
            self._btn_import_model.setEnabled(False)
            self._btn_import_model.setText("正在后台复制..." if self._lang == "zh" else "Copying...")
            worker = ModelCopyWorker(p, dest, self)

            def on_copy_finished(success: bool, target_path_str: str, err: str):
                self._btn_import_model.setEnabled(True)
                self._btn_import_model.setText(self._tr("model_import_btn"))
                if success:
                    target_p = Path(target_path_str)
                    self._reload_model_choices(select_path=to_portable_path(target_p))
                    topmost_message(
                        "information",
                        self._tr("title_info"),
                        f"已成功复制并导入模型: {target_p.name}，点击“保存”后将提示重启生效。"
                        if self._lang == "zh"
                        else f"Successfully imported model: {target_p.name}. Click 'Save' to apply restart.",
                        parent=self,
                    )
                else:
                    topmost_message(
                        "warning",
                        self._tr("title_warning"),
                        f"导入模型文件失败: {err}" if self._lang == "zh" else f"Failed to import model file: {err}",
                        parent=self,
                    )

            worker.copy_finished.connect(on_copy_finished)
            self._model_copy_worker = worker
            worker.start()

    def _on_hotkey_recording(self, on: bool):
        """录入热键时暂停全局监听，避免与输入框抢键。"""
        hm = self._hotkey_manager
        if hm is None:
            return
        if on:
            try:
                hm.pause()
            except Exception:
                pass
            return
        # 任一热键框仍有焦点则继续暂停
        if any(e.hasFocus() for e in self._hk_edits):
            return
        try:
            hm.resume()
        except Exception:
            pass

    def _save(self):
        from ..hotkeys import find_hotkey_conflicts

        lang = self._ui_lang.currentData() or "zh"
        self._lang = "en" if str(lang).startswith("en") else "zh"
        selected_tokens = self._max_tokens.currentData()
        max_tokens = (
            self._max_tokens_custom.value()
            if selected_tokens == "custom"
            else int(selected_tokens)
        )
        if not 64 <= max_tokens <= 8192:
            topmost_message(
                "warning",
                self._tr("max_tokens_invalid_title"),
                self._tr("max_tokens_invalid_body"),
                parent=self,
            )
            return
        old_model = str(self._cfg.get("model_path") or "").strip()
        old_device = str(self._cfg.get("llama_device", "auto")).lower()
        selected_device = str(self._llama_device.currentData() or "auto").lower()
        device_changed = selected_device != old_device
        selected_model = str(self._model_file.currentData() or "").strip()
        model_changed = bool(
            selected_model
            and self._model_path_key(selected_model) != self._model_path_key(old_model)
        )
        if model_changed and not is_gguf_model(resolve_path(selected_model)):
            topmost_message(
                "warning",
                self._tr("model_invalid_title"),
                self._tr("model_invalid_body", path=selected_model),
                parent=self,
            )
            return
        draft = {
            "ui_language": self._lang,
            "target_language": self._target.currentText(),
            "history_enabled": self._history_enabled.isChecked(),
            "translation_cache_enabled": self._translation_cache_enabled.isChecked(),
            "translate_window_font_size": int(
                self._translation_font_size.currentData() or 0
            ),
            "hotkey_screenshot": self._hk_shot.value,
            "hotkey_word": self._hk_word.value,
            "hotkey_window": self._hk_win.value,
            "hotkey_region_watch": self._hk_region.value,
            "window_watch_interval_ms": _monitor_interval_value(
                self._win_interval, self._win_interval_custom
            ),
            "window_watch_font_size": int(self._win_font_size.currentData() or 0),
            "window_watch_annotate": self._win_annotate.currentData(),
            "window_annotate_skip_target_lang": self._win_skip_target.isChecked(),
            "region_watch_interval_ms": _monitor_interval_value(
                self._reg_interval, self._reg_interval_custom
            ),
            "region_watch_font_size": int(self._reg_font_size.currentData() or 0),
            "region_watch_annotate": self._reg_annotate.currentData(),
            "region_annotate_skip_target_lang": self._reg_skip_target.isChecked(),
            "annotate_text_color": self._normalize_hex_color(self._ann_color.text()),
            "annotate_capture_visible": self._ann_capture_visible.isChecked(),
            "max_tokens": max_tokens,
            "llama_device": selected_device,
        }
        if selected_model:
            draft["model_path"] = to_portable_path(selected_model)
        conflicts = find_hotkey_conflicts({**self._cfg, **draft})
        if conflicts:
            topmost_message(
                "warning",
                self._tr("hk_conflict_title"),
                self._tr("hk_conflict_body", list="\n".join(conflicts)),
                parent=self,
            )
            return
        self._cfg.update(draft)
        self._cfg.pop("annotate_skip_target_lang", None)
        config.save(self._cfg)
        if self._on_saved:
            self._on_saved()
        geo = self.frameGeometry()
        toast_msg = self._tr(
            "saved_restart_toast" if model_changed or device_changed else "saved_toast"
        )
        self.hide()
        show_toast(toast_msg, at_rect=geo, msec=1500)

    def closeEvent(self, event):
        worker = getattr(self, "_model_copy_worker", None)
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait(500)
        super().closeEvent(event)


class HistoryWindow(_DraggableMixin, QWidget):
    """翻译历史（最多 50 条）。双击一行在翻译窗口打开。

    视觉与截图/划词翻译结果窗一致。
    """

    def __init__(self, storage, on_open=None, on_clear_cache=None):
        super().__init__()
        apply_frameless_float(self)
        self.resize(640, 420)
        self.setMinimumSize(400, 260)
        ensure_stays_on_top(self)
        self._storage = storage
        self._on_open = on_open
        self._on_clear_cache = on_clear_cache

        self._table = QTableWidget(0, 2)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 280)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.cellDoubleClicked.connect(self._on_double_click)

        self._tip = QLabel()
        self._search = QLineEdit()
        self._search.textChanged.connect(self._filter_history)
        self._btn_copy_src = QPushButton()
        self._btn_copy_src.clicked.connect(lambda: self._copy_selected(0))
        self._btn_copy_dst = QPushButton()
        self._btn_copy_dst.clicked.connect(lambda: self._copy_selected(1))
        self._btn_delete = QPushButton()
        self._btn_delete.clicked.connect(self._delete_selected)
        self._btn_clear = QPushButton()
        self._btn_clear.clicked.connect(self._clear_history)
        self._btn_clear_cache = QPushButton()
        self._btn_clear_cache.clicked.connect(self._clear_translation_cache)

        title_bar = QHBoxLayout()
        title_bar.setContentsMargins(4, 2, 2, 2)
        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet("font-size:14px;font-weight:600;")
        btn_close = QPushButton("×")
        btn_close.setFixedWidth(28)
        btn_close.clicked.connect(self.hide)
        title_bar.addWidget(self._title_lbl)
        title_bar.addStretch()
        title_bar.addWidget(self._btn_clear_cache)
        title_bar.addWidget(self._btn_clear)
        title_bar.addWidget(btn_close)

        container = QWidget()
        container.setObjectName("panel")
        inner = QVBoxLayout(container)
        inner.setContentsMargins(12, 10, 12, 10)
        inner.setSpacing(8)
        inner.addLayout(title_bar)
        tools = QHBoxLayout()
        tools.addWidget(self._search, stretch=1)
        tools.addWidget(self._btn_copy_src)
        tools.addWidget(self._btn_copy_dst)
        tools.addWidget(self._btn_delete)
        inner.addLayout(tools)
        inner.addWidget(self._table, stretch=1)
        inner.addWidget(self._tip)
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch()
        grip_row.addWidget(CornerSizeGrip(container))
        inner.addLayout(grip_row)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self.setStyleSheet(FLOAT_PANEL_STYLE)
        self.apply_ui_language()

    def apply_ui_language(self):
        self.setWindowTitle(_ti("hist_title"))
        self._title_lbl.setText(_ti("hist_title"))
        self._tip.setText(_ti("hist_tip"))
        self._btn_clear.setText(_ti("hist_clear"))
        self._btn_clear_cache.setText(_ti("hist_clear_cache"))
        self._search.setPlaceholderText(_ti("hist_search"))
        self._btn_copy_src.setText(_ti("hist_copy_src"))
        self._btn_copy_dst.setText(_ti("hist_copy_dst"))
        self._btn_delete.setText(_ti("hist_delete"))
        self._table.setHorizontalHeaderLabels([_ti("hist_src"), _ti("hist_dst")])

    def showEvent(self, event):
        self.apply_ui_language()
        rows = self._storage.recent_history_entries()
        self._table.setRowCount(len(rows))
        for r, (entry_id, _ts, src, dst, _mode) in enumerate(rows):
            src_item = QTableWidgetItem(src)
            src_item.setData(Qt.ItemDataRole.UserRole, entry_id)
            self._table.setItem(r, 0, src_item)
            self._table.setItem(r, 1, QTableWidgetItem(dst))
        self._filter_history(self._search.text())
        super().showEvent(event)

    def _filter_history(self, query: str):
        needle = (query or "").strip().casefold()
        for row in range(self._table.rowCount()):
            text = " ".join(
                self._table.item(row, col).text()
                for col in range(2) if self._table.item(row, col)
            ).casefold()
            self._table.setRowHidden(row, bool(needle and needle not in text))

    def _copy_selected(self, column: int):
        row = self._table.currentRow()
        item = self._table.item(row, column) if row >= 0 else None
        if item:
            QApplication.clipboard().setText(item.text())

    def _delete_selected(self):
        row = self._table.currentRow()
        item = self._table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        answer = QMessageBox.question(
            self, _ti("hist_delete"), _ti("hist_delete_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._storage.delete_history(item.data(Qt.ItemDataRole.UserRole))
        self._table.removeRow(row)

    def _clear_history(self):
        answer = QMessageBox.question(
            self,
            _ti("hist_clear"),
            _ti("hist_clear_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._storage.clear_history() is False:
            topmost_message(
                "warning", _ti("hist_clear"), _ti("hist_clear_failed"), parent=self
            )
            return
        self._table.setRowCount(0)

    def _clear_translation_cache(self):
        answer = QMessageBox.question(
            self,
            _ti("hist_clear_cache"),
            _ti("hist_clear_cache_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._on_clear_cache is not None:
            if self._on_clear_cache() is False:
                topmost_message(
                    "warning", _ti("hist_clear_cache"),
                    _ti("hist_clear_cache_busy"), parent=self,
                )
        else:
            self._storage.clear_translation_cache()

    def _on_double_click(self, row: int, _column: int):
        src_item = self._table.item(row, 0)
        dst_item = self._table.item(row, 1)
        if not src_item or not self._on_open:
            return
        source = src_item.text()
        translation = dst_item.text() if dst_item else ""
        self._on_open(source, translation)


class _TranslateWorker(QThread):
    """翻译面板的后台请求线程，避免阻塞 UI。"""

    done = Signal(str)
    failed = Signal(str)

    def __init__(self, translator, text: str, target: str, parent=None):
        super().__init__(parent)
        self._translator = translator
        self._text = text
        self._target = target

    def run(self):
        try:
            if self.isInterruptionRequested():
                return
            result = self._translator.translate(self._text, self._target)
            if not self.isInterruptionRequested():
                self.done.emit(result)
        except Exception as e:
            from ..applog import get_logger
            from ..workers import friendly_error

            get_logger("ui").exception("翻译面板失败")
            if not self.isInterruptionRequested():
                self.failed.emit(friendly_error(e))


class InputTranslateWindow(_DraggableMixin, QWidget):
    """统一翻译窗口：手动输入翻译，也承接划词/截屏的结果显示。

    源语言自动识别，目标语言窗口内直接切换（切换后自动重翻当前内容）。
    无边框半透明（与实时字幕同款），可拖动；点"固定"锁定位置防误拖。
    """

    def __init__(self, translator, cfg: dict, ensure_server=None, on_target_language_changed=None):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(460, 320)
        self.setMinimumSize(320, 220)
        self._translator = translator
        self._cfg = cfg
        self._ensure_server = ensure_server  # 可选 () -> bool
        self._on_target_language_changed = on_target_language_changed
        self._pinned = False
        self._worker: _TranslateWorker | None = None
        self._block_lang_signal = False

        self._input = QTextEdit()
        self._output = QTextEdit(readOnly=True)
        self._default_input_font = QFont(self._input.font())
        self._default_output_font = QFont(self._output.font())

        self._lang = QComboBox()
        self._lang.addItems(LANGUAGES)
        self._lang.setCurrentText(cfg.get("target_language", "简体中文"))
        self._lang.currentTextChanged.connect(self._on_lang_changed)

        self._btn_pin = QPushButton()
        self._btn_pin.setCheckable(True)
        self._btn_pin.toggled.connect(self._toggle_pin)
        self._btn_go = QPushButton()
        self._lab_source = QLabel()
        self._lab_translation = QLabel()
        self._btn_copy_source = QPushButton()
        self._btn_copy_translation = QPushButton()
        btn_close = QPushButton("×")
        btn_close.setFixedWidth(28)
        self._btn_go.clicked.connect(self._go)
        self._btn_copy_source.clicked.connect(self._copy_source)
        self._btn_copy_translation.clicked.connect(self._copy_translation)
        btn_close.clicked.connect(self.hide)

        bar = QHBoxLayout()
        self._lab_to = QLabel()
        bar.addWidget(self._lab_to)
        bar.addWidget(self._lang)
        bar.addStretch()
        bar.addWidget(self._btn_go)
        bar.addWidget(self._btn_pin)
        bar.addWidget(btn_close)

        source_bar = QHBoxLayout()
        source_bar.setContentsMargins(0, 0, 0, 0)
        source_bar.addWidget(self._lab_source)
        source_bar.addStretch()
        source_bar.addWidget(self._btn_copy_source)

        translation_bar = QHBoxLayout()
        translation_bar.setContentsMargins(0, 0, 0, 0)
        translation_bar.addWidget(self._lab_translation)
        translation_bar.addStretch()
        translation_bar.addWidget(self._btn_copy_translation)

        # 内容容器：与实时字幕条相同的半透明底
        container = QWidget()
        container.setObjectName("panel")
        inner = QVBoxLayout(container)
        inner.addLayout(bar)
        inner.addLayout(source_bar)
        inner.addWidget(self._input)
        inner.addLayout(translation_bar)
        inner.addWidget(self._output)
        # 右下角拖拽调整窗口大小
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch()
        grip_row.addWidget(QSizeGrip(container))
        inner.addLayout(grip_row)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)

        self.setStyleSheet(
            "#panel{background:rgba(0,0,0,175);border-radius:8px;}"
            "QLabel{color:#fff;}"
            "QTextEdit{background:rgba(255,255,255,28);color:#fff;"
            "border:1px solid rgba(255,255,255,60);border-radius:4px;}"
            "QComboBox{background:rgba(255,255,255,28);color:#fff;"
            "border:1px solid rgba(255,255,255,60);border-radius:4px;padding:2px 6px;}"
            "QComboBox QAbstractItemView{background:#2b2b2b;color:#eee;}"
            "QPushButton{background:rgba(255,255,255,40);color:#fff;"
            "border:none;border-radius:4px;padding:4px 10px;}"
            "QPushButton:checked{background:rgba(0,150,255,150);}"
        )
        self.apply_ui_language()
        self.sync_font_size_from_cfg()

    def apply_ui_language(self):
        self.setWindowTitle(_ti("tw_title"))
        self._lab_to.setText(_ti("tw_to"))
        self._input.setPlaceholderText(_ti("tw_placeholder"))
        self._output.setPlaceholderText(_ti("tw_out_ph"))
        self._lab_source.setText(_ti("tw_source"))
        self._lab_translation.setText(_ti("tw_translation"))
        self._btn_go.setText(_ti("tw_translate"))
        self._btn_copy_source.setText(_ti("tw_copy"))
        self._btn_copy_translation.setText(_ti("tw_copy"))
        self._btn_pin.setText(
            _ti("tw_pinned") if self._pinned else _ti("tw_pin")
        )

    def _toggle_pin(self, checked: bool):
        self._pinned = checked
        self._btn_pin.setText(_ti("tw_pinned") if checked else _ti("tw_pin"))

    def _on_lang_changed(self, lang: str):
        if self._block_lang_signal:
            return
        self._cfg["target_language"] = lang
        config.save(self._cfg)
        if callable(self._on_target_language_changed):
            try:
                self._on_target_language_changed(lang)
            except Exception:
                pass
        self._go()

    def sync_language_from_cfg(self):
        """设置页改语言后同步下拉（不触发重翻）。"""
        lang = self._cfg.get("target_language", "简体中文")
        if self._lang.currentText() == lang:
            return
        self._block_lang_signal = True
        self._lang.setCurrentText(lang)
        self._block_lang_signal = False

    def sync_font_size_from_cfg(self):
        """按配置更新输入和译文区域字号；0 表示保留系统默认字号。"""
        try:
            size = int(self._cfg.get("translate_window_font_size", 0))
        except (TypeError, ValueError):
            size = 0
        for widget, default_font in (
            (self._input, self._default_input_font),
            (self._output, self._default_output_font),
        ):
            font = QFont(default_font)
            if 12 <= size <= 20:
                font.setPixelSize(size)
            widget.setFont(font)

    def mouseMoveEvent(self, event):
        # 固定后不可拖动
        if not self._pinned:
            super().mouseMoveEvent(event)

    @property
    def target_language(self) -> str:
        return self._lang.currentText()

    def show_result(self, source: str, translation: str, near_x: int = None,
                    near_y: int = None):
        """划词/截屏结果送入本窗口。未固定时移动到触发位置附近（支持多屏）。"""
        self._input.setPlainText(source)
        self._output.setPlainText(translation)
        self._remembered_geometry = True
        if not self._pinned and near_x is not None and near_y is not None:
            from PySide6.QtCore import QPoint

            pt = QPoint(int(near_x), int(near_y))
            screen = QGuiApplication.screenAt(pt)
            if screen is None:
                screen = QGuiApplication.primaryScreen()
            geo = screen.availableGeometry()
            x = min(max(near_x, geo.left()), geo.right() - self.width())
            y = min(max(near_y, geo.top()), geo.bottom() - self.height())
            self.move(x, y)
        raise_to_front(self, activate=True)

    def _go(self):
        text = self._input.toPlainText().strip()
        if not text:
            return
        if self._worker and self._worker.isRunning():
            return  # 上一个请求还没回来，不叠加
        if self._ensure_server is not None and not self._ensure_server():
            self._output.setPlainText(_ti("tw_server_fail"))
            return
        self._output.setPlainText(_ti("tw_busy"))
        self._worker = _TranslateWorker(
            self._translator, text, self._lang.currentText(), parent=self
        )
        self._worker.done.connect(self._output.setPlainText)
        self._worker.failed.connect(
            lambda e: self._output.setPlainText(_ti("tw_fail", e=e))
        )
        self._worker.start()

    def active_worker(self) -> _TranslateWorker | None:
        """供应用退出流程等待，避免销毁仍在运行的 QThread。"""
        if self._worker is not None and self._worker.isRunning():
            return self._worker
        return None

    def _copy_source(self):
        self._copy_text(self._input.toPlainText(), "tw_copied_source")

    def _copy_translation(self):
        self._copy_text(self._output.toPlainText(), "tw_copied_translation")

    def _copy_text(self, text: str, message_key: str):
        if not text:
            return
        QApplication.clipboard().setText(text)
        show_toast(_ti(message_key), near=self, msec=1200)
