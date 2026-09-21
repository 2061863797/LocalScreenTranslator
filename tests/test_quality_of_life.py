import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from app.config import DEFAULTS
from app.i18n import set_language
from app.storage import Storage
from app.ui.topmost import restore_window_geometry, window_geometry_value
from app.ui.windows import HistoryWindow, InputTranslateWindow, SettingsWindow
from app.window_watcher import WindowWatcher


class QualityOfLifeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def test_runtime_status_shows_failure_and_retry(self):
        retry = Mock()
        with patch("app.ui.windows.available_translation_models", return_value=[]):
            window = SettingsWindow(dict(DEFAULTS), on_retry_runtime=retry)
        try:
            window.set_runtime_status({
                "llama": ("ok", "CPU"),
                "ocr": ("fail", "OCR 资源缺失"),
            })
            self.assertIn("CPU", window._runtime_text.text())
            self.assertIn("OCR 资源缺失", window._runtime_text.text())
            self.assertFalse(window._runtime_retry.isHidden())
            window._runtime_retry.click()
            retry.assert_called_once_with()
        finally:
            window.close()
            window.deleteLater()

    def test_watcher_pause_state_can_resume_without_recreation(self):
        watcher = WindowWatcher(Mock(), Mock(), dict(DEFAULTS), hwnd=1)
        watcher.set_paused(True)
        self.assertTrue(watcher._paused.is_set())
        watcher.set_paused(False)
        self.assertFalse(watcher._paused.is_set())

    def test_window_follow_timer_moves_overlays_between_ocr_polls(self):
        """拖动目标窗时浮层由高频定时器平滑跟随，不再等 OCR 轮询瞬移。"""
        from app.main import App

        app = App.__new__(App)
        app._watch_hwnd = 42
        app._watch_region = None
        app._watch_rect = (0, 0, 100, 100)
        app._window_follow_timer = Mock()
        app.subtitle = Mock()
        app.subtitle.mode = "follow"
        app.subtitle.isVisible.return_value = True
        app.annotation = Mock()
        app.annotation.isVisible.return_value = True
        app.annotate_ctrl = Mock()
        app.annotate_ctrl.isVisible.return_value = True

        moved = (5, 6, 100, 100)
        with (
            patch("app.main.capture.get_window_rect", return_value=moved),
            patch.object(App, "_restack_watch_layer"),
        ):
            app._follow_target_window()
        self.assertEqual(app._watch_rect, moved)
        app.annotation.update_geometry.assert_called_once_with(moved)
        app.subtitle.attach_below.assert_called_once_with(moved, outside=True)
        app._window_follow_timer.stop.assert_not_called()

        # 位置没变不做任何事；目标消失时定时器自行停止
        app.annotation.update_geometry.reset_mock()
        with (
            patch("app.main.capture.get_window_rect", return_value=moved),
            patch.object(App, "_restack_watch_layer"),
        ):
            app._follow_target_window()
        app.annotation.update_geometry.assert_not_called()
        app._watch_hwnd = None
        app._follow_target_window()
        app._window_follow_timer.stop.assert_called_once_with()

    def test_history_entries_can_be_searched_and_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "data.db")
            storage.add_history("hello", "你好", "test")
            storage.add_history("world", "世界", "test")
            window = HistoryWindow(storage)
            try:
                window.show()
                self.qapp.processEvents()
                window._filter_history("hello")
                self.assertFalse(window._table.isRowHidden(1))
                self.assertTrue(window._table.isRowHidden(0))
                entry_id = storage.recent_history_entries()[0][0]
                storage.delete_history(entry_id)
                self.assertEqual(len(storage.recent_history_entries()), 1)
            finally:
                window.close()
                window.deleteLater()
                storage.close()

    def test_geometry_is_restored_only_on_an_existing_screen(self):
        widget = QWidget()
        try:
            screen = self.qapp.primaryScreen().availableGeometry()
            value = [screen.x() + 10, screen.y() + 10, 320, 220]
            self.assertTrue(restore_window_geometry(widget, value))
            self.assertEqual(window_geometry_value(widget), value)
            self.assertFalse(
                restore_window_geometry(widget, [100000, 100000, 320, 220])
            )
        finally:
            widget.deleteLater()

    def test_translate_window_copies_each_text_with_feedback(self):
        set_language("zh")
        window = InputTranslateWindow(Mock(), dict(DEFAULTS))
        try:
            window._input.setPlainText("原始文本")
            window._output.setPlainText("翻译文本")
            with patch("app.ui.windows.show_toast") as show_toast:
                window._btn_copy_source.click()
                self.assertEqual(
                    QApplication.clipboard().text(),
                    "原始文本",
                )
                show_toast.assert_called_once_with(
                    "原文已复制", near=window, msec=1200
                )

                show_toast.reset_mock()
                window._btn_copy_translation.click()
                self.assertEqual(
                    QApplication.clipboard().text(),
                    "翻译文本",
                )
                show_toast.assert_called_once_with(
                    "译文已复制", near=window, msec=1200
                )
        finally:
            window.close()
            window.deleteLater()
            self.qapp.processEvents()

    def test_monitor_intervals_offer_presets_and_preserve_custom_values(self):
        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 700
        cfg["region_watch_interval_ms"] = 1000
        with (
            patch("app.ui.windows.available_translation_models", return_value=[]),
            patch("app.ui.windows.config.save") as save_cfg,
            patch("app.ui.windows.show_toast"),
            patch("app.hotkeys.find_hotkey_conflicts", return_value=[]),
        ):
            window = SettingsWindow(cfg)
            try:
                presets = [
                    window._win_interval.itemData(index)
                    for index in range(window._win_interval.count())
                    if isinstance(window._win_interval.itemData(index), int)
                ]
                self.assertEqual(
                    presets,
                    [200, 500, 800, 1000, 1500, 2000, 3000, 5000],
                )
                self.assertEqual(window._win_interval.currentData(), "custom")
                self.assertEqual(window._win_interval_custom.value(), 700)
                self.assertFalse(window._win_interval_custom.isHidden())
                self.assertEqual(window._reg_interval.currentData(), 1000)
                self.assertTrue(window._reg_interval_custom.isHidden())

                window._win_interval.setCurrentIndex(
                    window._win_interval.findData(500)
                )
                window._reg_interval.setCurrentIndex(
                    window._reg_interval.findData("custom")
                )
                window._reg_interval_custom.setValue(1700)
                window._save()
            finally:
                window.close()
                window.deleteLater()
                self.qapp.processEvents()

        self.assertEqual(cfg["window_watch_interval_ms"], 500)
        self.assertEqual(cfg["region_watch_interval_ms"], 1700)
        save_cfg.assert_called_once_with(cfg)

    def test_region_watch_subtitle_matches_region_size(self):
        """区域翻译下字幕条尺寸直接匹配区域尺寸，并贴在正下方。"""
        from app.ui.overlays import SubtitleBar

        bar = SubtitleBar()
        try:
            # 预设一个旧的记忆尺寸
            bar._user_size = (500, 200)
            region_rect = (100, 150, 320, 60)
            bar.attach_below(region_rect, outside=True, match_target_size=True)
            self.assertEqual(bar.width(), 320)
            self.assertEqual(bar.height(), 60)
            self.assertEqual(bar.x(), 100)
            self.assertEqual(bar.y(), 150 + 60 + 4)
            self.assertIsNone(bar._user_size)

            # 缩放区域后再次同步（当区域高度低于 _MIN_H 时，保底为 _MIN_H 以保证正文物理可见）
            smaller_region = (50, 80, 160, 40)
            bar.attach_below(smaller_region, outside=True, match_target_size=True)
            self.assertEqual(bar.width(), 160)
            self.assertEqual(bar.height(), bar._MIN_H)
            self.assertEqual(bar.x(), 50)
            self.assertEqual(bar.y(), 80 + 40 + 4)
        finally:
            bar.close()
            bar.deleteLater()

    def test_subtitle_ctrl_drag_auto_switches_to_free_mode(self):
        """跟随模式下拖动控制条把手时自动切换为自由模式。"""
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QMouseEvent
        from app.ui.overlays import SubtitleBar

        bar = SubtitleBar()
        try:
            bar.attach_below((100, 100, 200, 80), outside=True)
            self.assertEqual(bar.mode, "follow")
            ctrl = bar._ctrl

            # 模拟按下把手
            ctrl._drag_offset = QPoint(10, 10)
            # 模拟移动事件
            event = QMouseEvent(
                QMouseEvent.Type.MouseMove,
                QPoint(30, 40),
                QPoint(130, 140),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            ctrl.mouseMoveEvent(event)
            self.assertEqual(bar.mode, "free")
            self.assertTrue(ctrl._btns["free"].isChecked())
            self.assertFalse(ctrl._btns["follow"].isChecked())
        finally:
            bar.close()
            bar.deleteLater()


if __name__ == "__main__":
    unittest.main()
