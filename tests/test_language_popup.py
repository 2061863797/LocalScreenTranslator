# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from app.ui.overlays import (
    AnnotateCtrl,
    LanguagePairButton,
    LanguageSelectPopup,
    RegionWatchFrame,
    SubtitleBar,
)
from app.translation_runtime.languages import LANGUAGES
from app.ui.topmost import set_overlay_layer


class TestLanguageSelectPopup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_popup_creation_and_languages(self):
        popup = LanguageSelectPopup("英语")
        try:
            self.assertEqual(popup.width(), 330)
            self.assertEqual(popup.height(), 286)
            btns = popup.findChildren(QPushButton)
            self.assertEqual(len(btns), len(LANGUAGES))

            eng_btn = next((b for b in btns if "英语" in b.text()), None)
            zh_btn = next((b for b in btns if "简体中文" in b.text()), None)
            self.assertIsNotNone(eng_btn)
            self.assertIsNotNone(zh_btn)
            self.assertTrue(eng_btn.text().startswith("✔"))
            self.assertFalse(zh_btn.text().startswith("✔"))
        finally:
            popup.close()

    def test_popup_selection_signal(self):
        popup = LanguageSelectPopup("简体中文")
        selected_lang = []
        popup.language_selected.connect(selected_lang.append)
        try:
            btns = popup.findChildren(QPushButton)
            ja_btn = next((b for b in btns if "日语" in b.text()), None)
            self.assertIsNotNone(ja_btn)
            ja_btn.click()
            self.assertEqual(selected_lang, ["日语"])
            self.assertFalse(popup.isVisible())
        finally:
            popup.close()

    def test_language_pair_button_popup_direction(self):
        # 1. 备注控制栏中（置于识别框上方），空间充足时优先向上弹出
        ann_ctrl = AnnotateCtrl()
        btn = ann_ctrl._language_button
        btn.resize(60, 26)
        btn.move(100, 300)
        ann_ctrl.resize(300, 30)
        ann_ctrl.move(100, 300)
        ann_ctrl.show()

        btn._open_menu()
        popup = btn._popup
        self.assertIsNotNone(popup)
        try:
            # 向上弹：popup.y() 应小于按钮的 y 坐标 (300)
            self.assertLess(popup.y(), 300)
        finally:
            popup.close()
            ann_ctrl.close()

    def test_active_popup_restack_guard(self):
        # 当存在活跃 Popup 时，restack_layer 和 set_overlay_layer 应安全返回，不抢置顶
        mock_popup = QWidget()
        with patch.object(QApplication, "activePopupWidget", return_value=mock_popup):
            with patch("app.ui.topmost._set_window_pos") as mock_swp:
                bar = SubtitleBar()
                bar.show()
                bar.restack_layer()
                mock_swp.assert_not_called()
                bar.close()

            with patch("app.ui.topmost._set_window_pos") as mock_swp:
                frame = RegionWatchFrame()
                frame.show_region((100, 100, 200, 200))
                frame.restack_layer()
                mock_swp.assert_not_called()
                frame.close()

            with patch("app.ui.topmost._set_window_pos") as mock_swp:
                ctrl = AnnotateCtrl()
                ctrl.show()
                ctrl.restack_layer()
                mock_swp.assert_not_called()
                ctrl.close()

            with patch("app.ui.topmost._set_window_pos") as mock_swp:
                dummy = QWidget()
                set_overlay_layer(dummy, None)
                mock_swp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
