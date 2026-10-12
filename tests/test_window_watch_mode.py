"""窗口持续翻译的启动显示模式回归测试。"""

import unittest
from unittest.mock import Mock, patch

from app import main as app_main


class WindowWatchModeTests(unittest.TestCase):
    def test_new_window_watch_session_always_starts_in_annotation_mode(self):
        app = app_main.App.__new__(app_main.App)
        app._quitting = False
        app._stopping_watch = False
        app._is_continuous_active = Mock(return_value=False)
        app._is_continuous_selecting = Mock(return_value=False)
        app._ensure_translation_ready = Mock(return_value=True)
        app.qapp = Mock()
        app.qapp.screens.return_value = []
        app.cfg = {"window_watch_annotate": False}
        app._launch_watcher = Mock()

        picker = Mock()
        picker.exec.return_value = 1
        picker.selected_hwnd = 42

        with (
            patch.object(app_main, "WindowPicker", return_value=picker),
            patch.object(app_main.capture, "configure_qt_screens"),
            patch.object(
                app_main.capture, "get_window_rect", return_value=(10, 20, 300, 200)
            ),
        ):
            app._toggle_window_watch()

        app._launch_watcher.assert_called_once_with(
            hwnd=42,
            rect=(10, 20, 300, 200),
            annotate=True,
            profile="window",
        )

    def test_window_watch_ignores_runtime_switch_to_subtitle(self):
        app = app_main.App.__new__(app_main.App)
        app._stopping_watch = False
        app._watcher = Mock()
        app._watcher.isRunning.return_value = True
        app._watch_profile = "window"
        app._watch_region = None
        app._watch_annotate = True
        app.log = Mock()

    def test_window_picker_instantiation_and_filtering(self):
        from PySide6.QtWidgets import QApplication
        from app.ui.window_picker import WindowPicker
        _app = QApplication.instance() or QApplication([])

        fake_windows = [(101, "Google Chrome"), (102, "Visual Studio Code"), (103, "Notepad")]
        with patch("app.capture.list_windows", return_value=fake_windows):
            picker = WindowPicker()
            try:
                self.assertEqual(picker._list.count(), 3)
                picker._search.setText("chrome")
                self.assertFalse(picker._list.item(0).isHidden())
                self.assertTrue(picker._list.item(1).isHidden())
                self.assertTrue(picker._list.item(2).isHidden())

                picker._search.setText("Code")
                picker._confirm()
                self.assertEqual(picker.selected_hwnd, 102)
            finally:
                picker.close()
                picker.deleteLater()


class RegionFrameMoveTests(unittest.TestCase):
    def _app(self, paused: bool):
        app = app_main.App.__new__(app_main.App)
        app._watch_region = (0, 0, 100, 50)
        app._watch_annotate = True
        app._watch_paused = paused
        app._watcher = Mock()
        app._watcher.isRunning.return_value = True
        app.annotation = Mock()
        app.subtitle = Mock(mode="free")
        app.annotate_ctrl = Mock()
        app.annotate_ctrl.isVisible.return_value = False
        app.log = Mock()
        return app

    def test_paused_annotation_survives_region_drag(self):
        app = self._app(paused=True)
        app._on_region_frame_moved(30, 40, 100, 50)
        app.annotation.set_items.assert_not_called()
        app.annotation.update_geometry.assert_called_once_with((30, 40, 100, 50))
        app._watcher.set_region.assert_called_once_with((30, 40, 100, 50))

    def test_running_annotation_is_cleared_on_region_drag(self):
        app = self._app(paused=False)
        app._on_region_frame_moved(30, 40, 100, 50)
        app.annotation.set_items.assert_called_once_with([])


if __name__ == "__main__":
    unittest.main()
