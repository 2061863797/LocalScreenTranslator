"""开机自启动：注册表读写（以内存替身代替 winreg）、延迟加载模型与设置界面开关。"""

import os
import sys
import types
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app import autostart, config
from app import main as app_main


def fake_winreg(fail: bool = False):
    """只在内存中模拟 HKCU\\...\\Run，绝不触碰真实注册表。"""
    store: dict[str, str] = {}
    reg = types.SimpleNamespace(HKEY_CURRENT_USER=object(), KEY_SET_VALUE=2, REG_SZ=1, store=store)

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, path, reserved=0, access=0):
        if fail:
            raise PermissionError("拒绝访问")
        assert path == autostart.RUN_KEY
        return Key()

    def query(key, name):
        if name not in store:
            raise FileNotFoundError(name)
        return store[name], reg.REG_SZ

    def delete(key, name):
        if name not in store:
            raise FileNotFoundError(name)
        del store[name]

    reg.OpenKey = open_key
    reg.QueryValueEx = query
    reg.SetValueEx = lambda key, name, reserved, kind, value: store.__setitem__(name, value)
    reg.DeleteValue = delete
    return reg


class AutostartRegistryTests(unittest.TestCase):
    def test_enable_writes_launch_command_and_disable_removes_it(self):
        reg = fake_winreg()
        with patch.dict(sys.modules, {"winreg": reg}):
            self.assertFalse(autostart.is_enabled())
            self.assertTrue(autostart.set_enabled(True))
            self.assertTrue(autostart.is_enabled())
            command = reg.store[autostart.VALUE_NAME]
            self.assertIn("run.py", command)
            self.assertTrue(command.endswith(autostart.AUTOSTART_FLAG))
            self.assertTrue(autostart.set_enabled(False))
            self.assertFalse(autostart.is_enabled())
            # 重复关闭不报错
            self.assertTrue(autostart.set_enabled(False))

    def test_registry_failure_is_reported_not_raised(self):
        with patch.dict(sys.modules, {"winreg": fake_winreg(fail=True)}):
            self.assertFalse(autostart.set_enabled(True))
            self.assertFalse(autostart.is_enabled())

    def test_frozen_build_launches_its_own_exe(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", r"C:\Program Files\本地屏译\LocalScreenTranslator.exe"):
            self.assertEqual(
                autostart.launch_command(),
                r'"C:\Program Files\本地屏译\LocalScreenTranslator.exe" --autostart',
            )

    def test_startup_flag_detection(self):
        self.assertTrue(autostart.launched_at_startup(["run.py", "--autostart"]))
        self.assertFalse(autostart.launched_at_startup(["run.py"]))


class DelayedPreloadTests(unittest.TestCase):
    def _app(self):
        app = app_main.App.__new__(app_main.App)
        app.log = Mock()
        app._preload_models = Mock()
        return app

    def test_manual_start_preloads_immediately(self):
        app = self._app()
        with patch.object(autostart, "launched_at_startup", return_value=False):
            app._schedule_preload()
        app._preload_models.assert_called_once_with()

    def test_autostart_defers_preload(self):
        app = self._app()
        with patch.object(autostart, "launched_at_startup", return_value=True), \
                patch.object(app_main.QTimer, "singleShot") as timer:
            app._schedule_preload()
        app._preload_models.assert_not_called()
        timer.assert_called_once_with(app._AUTOSTART_PRELOAD_DELAY_MS, app._preload_models)

    def test_using_translation_before_delay_starts_loading_now(self):
        app = self._app()
        app._quitting = False
        app.translator = Mock()
        app.translator.is_ready.return_value = False
        app._preload_status = {"llama": "pending"}
        app.tray = Mock()
        self.assertFalse(app._ensure_translation_ready())
        app._preload_models.assert_called_once_with()


class SettingsAutostartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def _window(self, enabled: bool):
        from app.ui.windows import SettingsWindow

        with patch.object(autostart, "is_enabled", return_value=enabled):
            win = SettingsWindow(config.load(), on_saved=Mock(), hotkey_manager=Mock(),
                                 on_retry_runtime=Mock())
        self.addCleanup(win.deleteLater)
        return win

    def test_checkbox_reflects_registry_and_save_toggles_it(self):
        win = self._window(enabled=False)
        self.assertFalse(win._autostart.isChecked())
        win._autostart.setChecked(True)
        with patch.object(autostart, "is_enabled", return_value=False), \
                patch.object(autostart, "set_enabled", return_value=True) as set_enabled:
            win._save()
        set_enabled.assert_called_once_with(True)

    def test_unchanged_checkbox_does_not_touch_registry(self):
        win = self._window(enabled=True)
        with patch.object(autostart, "is_enabled", return_value=True), \
                patch.object(autostart, "set_enabled") as set_enabled:
            win._save()
        set_enabled.assert_not_called()

    def test_watch_pages_are_merged(self):
        win = self._window(enabled=False)
        self.assertEqual(win._stack.count(), 5)
        self.assertEqual(len(win._nav_btns), 5)


if __name__ == "__main__":
    unittest.main()
