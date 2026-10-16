import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QColorDialog, QDialogButtonBox

from app.config import DEFAULTS
from app.i18n import set_language
from app.ui.windows import SettingsWindow


class UiI18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def tearDown(self):
        set_language("zh")

    def test_qt_color_dialog_uses_chinese_standard_buttons(self):
        set_language("zh")
        dialog = QColorDialog()
        dialog.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        try:
            buttons = dialog.findChild(QDialogButtonBox)
            self.assertIsNotNone(buttons)
            self.assertIn(
                "确定",
                buttons.button(QDialogButtonBox.StandardButton.Ok).text(),
            )
            self.assertIn(
                "取消",
                buttons.button(QDialogButtonBox.StandardButton.Cancel).text(),
            )
        finally:
            dialog.close()
            dialog.deleteLater()
            self.qapp.processEvents()

    def test_settings_show_local_product_name_in_both_languages(self):
        cfg = dict(DEFAULTS)
        with patch("app.ui.windows.available_translation_models", return_value=[]):
            window = SettingsWindow(cfg)
            try:
                self.assertEqual(window.windowTitle(), "本地屏译 - 设置")
                self.assertEqual(window._title_lbl.text(), "本地屏译设置")
                self.assertIn("本机运行", window._sub_lbl.text())

                window._ui_lang.setCurrentIndex(window._ui_lang.findData("en"))
                self.qapp.processEvents()
                self.assertEqual(
                    window.windowTitle(),
                    "LocalScreen Translator - Settings",
                )
                self.assertEqual(
                    window._title_lbl.text(),
                    "LocalScreen Translator Settings",
                )
                self.assertIn("run locally", window._sub_lbl.text())
            finally:
                window.close()
                window.deleteLater()
                self.qapp.processEvents()


if __name__ == "__main__":
    unittest.main()
