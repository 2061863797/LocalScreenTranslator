import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.config import DEFAULTS
from app.main import App
from app.ui.overlays import AnnotationOverlay, SubtitleBar
from app.ui.windows import InputTranslateWindow, SettingsWindow


class FontSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def test_settings_save_three_independent_font_sizes(self):
        cfg = dict(DEFAULTS)
        on_saved = Mock()
        with (
            patch("app.ui.windows.available_translation_models", return_value=[]),
            patch("app.ui.windows.config.save") as save_cfg,
            patch("app.ui.windows.show_toast"),
            patch("app.hotkeys.find_hotkey_conflicts", return_value=[]),
        ):
            window = SettingsWindow(cfg, on_saved=on_saved)
            try:
                window._translation_font_size.setCurrentIndex(
                    window._translation_font_size.findData(15)
                )
                window._win_font_size.setCurrentIndex(
                    window._win_font_size.findData(20)
                )
                window._reg_font_size.setCurrentIndex(
                    window._reg_font_size.findData(17)
                )
                window._save()
            finally:
                window.close()
                window.deleteLater()
                self.qapp.processEvents()

        self.assertEqual(cfg["translate_window_font_size"], 15)
        self.assertEqual(cfg["window_watch_font_size"], 20)
        self.assertEqual(cfg["region_watch_font_size"], 17)
        save_cfg.assert_called_once_with(cfg)
        on_saved.assert_called_once_with()

    def test_settings_only_offer_12_to_20_pixels(self):
        from app.ui.windows import _font_size_combo

        widget = _font_size_combo()
        try:
            self.assertEqual(widget.findData(11), -1)
            self.assertGreaterEqual(widget.findData(12), 0)
            self.assertGreaterEqual(widget.findData(20), 0)
            self.assertEqual(widget.findData(21), -1)
        finally:
            widget.deleteLater()

    def test_translate_window_applies_and_restores_font_size(self):
        cfg = dict(DEFAULTS)
        cfg["translate_window_font_size"] = 19
        window = InputTranslateWindow(Mock(), cfg)
        try:
            self.assertEqual(window._input.font().pixelSize(), 19)
            self.assertEqual(window._output.font().pixelSize(), 19)

            cfg["translate_window_font_size"] = 0
            window.sync_font_size_from_cfg()
            self.assertEqual(
                window._input.font().toString(),
                window._default_input_font.toString(),
            )
            self.assertEqual(
                window._output.font().toString(),
                window._default_output_font.toString(),
            )
        finally:
            window.close()
            window.deleteLater()
            self.qapp.processEvents()

    def test_watch_overlays_apply_requested_and_default_sizes(self):
        subtitle = SubtitleBar()
        annotation = AnnotationOverlay()
        try:
            subtitle.set_font_size(20)
            annotation.set_font_size(20)
            self.assertEqual(subtitle._font.pixelSize(), 20)
            self.assertEqual(annotation._font_size, 20)

            subtitle.set_font_size(0)
            annotation.set_font_size(0)
            self.assertEqual(subtitle._font.pixelSize(), 16)
            self.assertEqual(annotation._font_size, 13)
        finally:
            subtitle.close()
            annotation.close()
            subtitle.deleteLater()
            annotation.deleteLater()
            self.qapp.processEvents()

    def test_window_and_region_profiles_do_not_share_font_size(self):
        app = App.__new__(App)
        app.cfg = {
            "window_watch_font_size": 18,
            "region_watch_font_size": 20,
        }
        app._watch_profile = None
        app.subtitle = Mock()
        app.annotation = Mock()

        app._apply_watch_font_size("window")
        app.subtitle.set_font_size.assert_called_once_with(18)
        app.annotation.set_font_size.assert_called_once_with(18)

        app.subtitle.reset_mock()
        app.annotation.reset_mock()
        app._apply_watch_font_size("region")
        app.subtitle.set_font_size.assert_called_once_with(20)
        app.annotation.set_font_size.assert_called_once_with(20)

    def test_saving_settings_updates_active_translation_displays_immediately(self):
        app = App.__new__(App)
        app.cfg = {
            "annotate_text_color": "#00F0FF",
            "window_watch_font_size": 20,
        }
        app.hotkeys = Mock()
        app.hotkeys.start.return_value = []
        app.apply_ui_language = Mock()
        app.translate_win = Mock()
        app.annotation = Mock()
        app.subtitle = Mock()
        app.annotate_ctrl = Mock()
        app._watcher = Mock()
        app._watcher.isRunning.return_value = True
        app._watch_rect = (0, 0, 320, 180)
        app._watch_region = None
        app._watch_profile = "window"
        app._watch_annotate = False
        app._switch_watch_display = Mock()

        app._on_settings_saved()

        app.translate_win.sync_font_size_from_cfg.assert_called_once_with()
        app.subtitle.set_font_size.assert_called_once_with(20)
        app.annotation.set_font_size.assert_called_once_with(20)
        app._switch_watch_display.assert_not_called()


if __name__ == "__main__":
    unittest.main()
