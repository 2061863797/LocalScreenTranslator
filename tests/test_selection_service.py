# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication

from app.selection import WordSelectionService, clone_mime_data


class TestWordSelectionService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_clone_mime_data_preserves_formats(self):
        source = QMimeData()
        source.setText("hello world")
        source.setData("application/x-custom", b"custom_bytes")

        cloned = clone_mime_data(source)
        self.assertEqual(cloned.text(), "hello world")
        self.assertEqual(bytes(cloned.data("application/x-custom")), b"custom_bytes")

    def test_clone_mime_data_none_safe(self):
        cloned = clone_mime_data(None)
        self.assertIsInstance(cloned, QMimeData)
        self.assertEqual(cloned.formats(), [])

    def test_fetch_selection_busy_guard(self):
        service = WordSelectionService()
        service.is_busy = True
        started = service.fetch_selection()
        self.assertFalse(started)

    def test_quitting_guard_and_restore(self):
        service = WordSelectionService()
        service.set_quitting(True)
        started = service.fetch_selection()
        self.assertFalse(started)

    def test_init_with_non_qobject_parent_is_safe(self):
        class DummyApp:
            pass

        service = WordSelectionService(DummyApp())
        self.assertIsNone(service.parent())

    def test_successful_fetch_lifecycle(self):
        service = WordSelectionService()
        captured = []
        service.text_ready.connect(lambda t, x, y: captured.append((t, x, y)))

        # 启动抓取并指定坐标
        with patch.object(service, "_fire_phase") as mock_fire, \
             patch.object(service, "_poll_clipboard") as mock_poll:
            started = service.fetch_selection(pos=(100, 200))
            self.assertTrue(started)
            self.assertTrue(service.is_busy)
            mock_fire.assert_called_once()
            mock_poll.assert_called_once_with(attempts=12)

        # 模拟获取到文本
        service._finish_with_text("Selected sentence")
        self.assertFalse(service.is_busy)
        self.assertEqual(captured, [("Selected sentence", 100, 200)])

    def test_failed_fetch_emits_failed(self):
        service = WordSelectionService()
        failed = []
        service.failed.connect(lambda: failed.append(True))

        with patch.object(service, "_fire_phase"), \
             patch.object(service, "_poll_clipboard"):
            service.fetch_selection(pos=(50, 50))

        # 模拟未能获取到任何文本
        service._finish_with_text("")
        self.assertFalse(service.is_busy)
        self.assertEqual(failed, [True])


if __name__ == "__main__":
    unittest.main()
