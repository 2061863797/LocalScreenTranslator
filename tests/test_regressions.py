# -*- coding: utf-8 -*-
"""从历次批次测试中保留下来的独有回归用例（按被测模块分组）。

这些用例覆盖的代码行在其它测试中没有覆盖，其余重复的批次测试已删除。
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from difflib import SequenceMatcher
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from app.config import DEFAULTS
from app.ocr_stabilizer import levenshtein_distance
from app.storage import AsyncHistoryStorage, Storage
from app.text_change_detector import TextChangeDetector
from app.ui.subtitle_bar import SubtitleBar
from app.ui.windows import ModelCopyWorker
from app.window_watcher import WindowWatcher


class StorageRegressionTests(unittest.TestCase):
    """历史记录写入队列在关闭与 flush 时的行为。"""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_history_close_timeout_keeps_connection_until_batch_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "history.db"
            storage = Storage(db, batch_size=1)
            entered = threading.Event()
            release = threading.Event()
            original_commit = storage._commit_history_batch

            def slow_commit(batch):
                entered.set()
                release.wait(10)
                original_commit(batch)

            storage._commit_history_batch = slow_commit
            try:
                storage.add_history_async("source", "translation", "test")
                self.assertTrue(entered.wait(2))
                self.assertFalse(storage.flush_and_close(timeout=0.01))
                self.assertIsNotNone(storage._conn)
                rejected = []
                storage.add_history_async("late", "late", "test", rejected.append)
                self.assertEqual(rejected, [False])
            finally:
                release.set()
                self.assertTrue(storage.flush_and_close(timeout=3))

            conn = sqlite3.connect(db)
            try:
                rows = conn.execute("SELECT source FROM history").fetchall()
            finally:
                conn.close()
            self.assertEqual(rows, [("source",)])

    def test_challenge_3a_flush_barrier_and_callbacks(self):
        """Stress synchronous flush barrier with 200 completion callbacks."""
        db_path = self.db_dir / "flush_callbacks.db"
        store = AsyncHistoryStorage(db_path, batch_size=10, flush_interval=0.5)

        callback_counts = [0]
        cb_lock = threading.Lock()

        def on_item_saved():
            with cb_lock:
                callback_counts[0] += 1

        for i in range(200):
            store.add_history_async(f"Src {i}", f"Tr {i}", "test", on_complete=on_item_saved)

        # Synchronous flush must block until all queued batches are in SQLite
        store.flush(timeout=5.0)

        with cb_lock:
            final_cb_count = callback_counts[0]
        self.assertEqual(final_cb_count, 200, "All 200 on_complete callbacks must be invoked after flush()")
        self.assertGreaterEqual(store.count_records(), 1)
        store.flush_and_close()


class WatcherRegressionTests(unittest.TestCase):
    """监视线程的资源释放与目标窗口关闭。"""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def test_watcher_keeps_resources_until_inference_thread_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        watcher = WindowWatcher(MagicMock(), MagicMock(), dict(DEFAULTS), hwnd=1)
        frame = np.ones((20, 20, 3), dtype=np.uint8)
        watcher._capture_service.grab = MagicMock(
            return_value=((0, 0, 20, 20), frame)
        )
        watcher._capture_service.release = MagicMock()

        def slow_ocr(*_args, **_kwargs):
            entered.set()
            release.wait(10)
            return []

        watcher._ocr_service.recognize_frame = slow_ocr
        try:
            watcher.start()
            self.assertTrue(entered.wait(2))
            watcher.stop()
            self.assertFalse(watcher.wait(4500))
            watcher._capture_service.release.assert_not_called()
        finally:
            release.set()
            watcher.stop()
            self.assertTrue(watcher.wait(3000))
        watcher._capture_service.release.assert_called_once()

    def test_exception_isolation_target_window_closed_clean_halt(self):
        """Empirical: Target window close (Win32 Error 1400) triggers clean stop signal, not crash."""
        ocr_mock = MagicMock()
        trans_mock = MagicMock()
        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 20
        watcher = WindowWatcher(ocr_mock, trans_mock, cfg, hwnd=1)

        class PyWin32Error(Exception):
            def __init__(self):
                self.winerror = 1400

        watcher._capture_service.grab = MagicMock(side_effect=PyWin32Error())

        stopped_reasons = []
        watcher.stopped.connect(stopped_reasons.append)

        watcher.start()
        watcher.wait(1000)
        self.qapp.processEvents()

        self.assertFalse(watcher.isRunning(), "Watcher must halt when target window closes")
        self.assertEqual(len(stopped_reasons), 1)
        self.assertIn("目标窗口已关闭", stopped_reasons[0])


class TextChangeRegressionTests(unittest.TestCase):
    """文字变化判定与 OCR 防抖的边界条件。"""

    def test_first_subtitle_immediate_dispatch_without_delay(self):
        """实证 2: 字幕模式下首条文本以及新字幕必须立即触发 change，不进入两帧候选确认。"""
        detector = TextChangeDetector(candidate_confirm_frames=2)
        # 首帧文本 "Hello World"，在字幕模式下调用
        event, text = detector.observe("Hello World", threshold=1.0, exact_change=True)
        self.assertEqual(event, "change", "首条字幕必须立即触发 change，禁止进入 candidate 延迟")
        self.assertEqual(text, "Hello World")
        self.assertEqual(detector.candidate_count, 0)

        # 相同文本：返回 none
        event, text = detector.observe("Hello World", threshold=1.0, exact_change=True)
        self.assertEqual(event, "none")

        # 换行字幕直接更新：立即 change
        event, text = detector.observe("Next Subtitle Line", threshold=1.0, exact_change=True)
        self.assertEqual(event, "change", "字幕更新必须立即触发 change")
        self.assertEqual(text, "Next Subtitle Line")
        self.assertEqual(detector.candidate_count, 0)

    def test_levenshtein_distance_stress(self):
        """Test Levenshtein distance on corner cases, unicode, and long strings."""
        self.assertEqual(levenshtein_distance("", ""), 0)
        self.assertEqual(levenshtein_distance("abc", ""), 3)
        self.assertEqual(levenshtein_distance("", "def"), 3)
        self.assertEqual(levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(levenshtein_distance("你好世界", "你好视界"), 1)
        self.assertEqual(levenshtein_distance("Hello 1", "Hello l"), 1)
        self.assertEqual(levenshtein_distance("a" * 1000, "a" * 1000), 0)
        self.assertEqual(levenshtein_distance("a" * 1000, "a" * 998 + "bb"), 2)

    def test_mathematical_bound_exact_boundary_conditions(self):
        """Test exact boundary transitions of the mathematical theoretical bound:

        Let ratio_bound = 2.0 * min(l1, l2) / (l1 + l2).
        1. When ratio_bound < threshold: skips SequenceMatcher entirely.
        2. When ratio_bound >= threshold: invokes SequenceMatcher.
        """
        detector = TextChangeDetector()
        detector.last_text = "X" * 1000
        threshold = 0.5

        # Case 1: l2 = 3001 -> bound = 2 * 1000 / 4001 = 0.499875 < 0.5 -> SKIP DP
        with patch("app.text_change_detector.SequenceMatcher") as mock_matcher:
            event, text = detector.observe("Y" * 3001, threshold=0.5)
            mock_matcher.assert_not_called()
            self.assertEqual(event, "change")

        # Case 2: detector has "Y" * 3001. Now l2 = 2000.
        # bound = 2 * 2000 / 5001 = 4000 / 5001 = 0.7998 >= 0.5 -> CANNOT SKIP, must call SequenceMatcher
        with patch("app.text_change_detector.SequenceMatcher", wraps=SequenceMatcher) as mock_matcher:
            event, text = detector.observe("Z" * 2000, threshold=0.5)
            mock_matcher.assert_called_once()
            self.assertEqual(event, "change")


class SubtitleBarRegressionTests(unittest.TestCase):
    """字幕条的空白文本、极端几何与超长文本。"""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        self.topmost_patcher = patch("app.ui.topmost._set_window_pos", return_value=True)
        self.owner_patcher = patch("app.ui.topmost._set_window_owner", return_value=True)
        self.topmost_patcher.start()
        self.owner_patcher.start()
        self.bar = SubtitleBar()

    def tearDown(self):
        self.bar.close()
        self.bar.deleteLater()
        self.topmost_patcher.stop()
        self.owner_patcher.stop()
        self.qapp.processEvents()

    def test_subtitle_bar_hidden_at_startup_and_whitespace_rejection(self):
        """Verify SubtitleBar is strictly hidden at startup and empty/whitespace never reveals it."""
        self.assertFalse(self.bar.isVisible(), "SubtitleBar must start invisible")
        self.assertFalse(self.bar._layout_calculated_before_show)

        # Feed empty, whitespace, and None
        for empty_val in ["", "   ", "\n\t  \r", None]:
            self.bar.set_text(empty_val)
            self.assertFalse(self.bar.isVisible(), f"SubtitleBar showed on empty input: {empty_val!r}")

        # Manipulations while hidden must not accidentally show the bar
        self.bar.attach_below((100, 100, 800, 600))
        self.assertFalse(self.bar.isVisible(), "attach_below should not show window before text")
        self.bar.move_to(200, 300)
        self.assertFalse(self.bar.isVisible(), "move_to should not show window before text")
        self.bar.resize_to(500, 200)
        self.assertFalse(self.bar.isVisible(), "resize_to should not show window before text")

    def test_unusual_and_extreme_geometries(self):
        """Unusual coordinates and sizes (0x0, negative coords, huge bounding boxes)."""
        # Negative coordinates
        self.bar.attach_below((-500, -300, 400, 200), outside=True)
        self.bar.set_text("Visible at negative coords")
        self.assertTrue(self.bar.isVisible())
        g = self.bar.geometry()
        self.assertEqual(g.x(), -500)
        self.assertEqual(g.y(), -300 + 200 + 4)

        # 0x0 target rectangle
        self.bar.attach_below((0, 0, 0, 0), outside=False)
        self.assertGreaterEqual(self.bar.width(), self.bar._MIN_W)
        self.assertGreaterEqual(self.bar.content_height(), self.bar._MIN_H)

        # Negative width/height in attach_below
        self.bar.attach_below((100, 100, -50, -50), outside=True)
        self.assertGreaterEqual(self.bar.width(), self.bar._MIN_W)
        self.assertGreaterEqual(self.bar.content_height(), self.bar._MIN_H)

        # Negative resize_to
        self.bar.resize_to(-100, -100)
        self.assertEqual(self.bar.panel_width(), self.bar._MIN_W)
        self.assertGreaterEqual(self.bar.width(), self.bar._MIN_W)
        self.assertEqual(self.bar.content_height(), self.bar._MIN_H)

        # Huge dimensions
        self.bar.resize_to(5000, 3000)
        self.assertEqual(self.bar.width(), 5000)
        self.assertEqual(self.bar.content_height(), 3000)

    def test_huge_text_stress(self):
        """Stress-test with huge text (100k chars, 2000 lines) without hanging or crashing."""
        lines = [f"This is line {i} of translated text with various words." for i in range(2000)]
        huge_text = "\n".join(lines)
        self.assertGreater(len(huge_text), 100000)

        t0 = time.time()
        self.bar.set_text(huge_text)
        elapsed = time.time() - t0

        self.assertTrue(self.bar.isVisible())
        self.assertLess(elapsed, 2.0, "Reflow of 2000 lines must finish in under 2 seconds")
        self.assertGreater(self.bar._content_h, self.bar.content_height())
        # Scroll bar must be visible and enabled
        self.assertTrue(self.bar._vscroll.isVisible())
        self.assertTrue(self.bar._vscroll._bar_widget.isEnabled())

        # Scroll to middle and end without errors
        self.bar.set_scroll(self.bar._content_h // 2)
        self.assertEqual(self.bar._scroll, self.bar._content_h // 2)
        self.bar.set_scroll(self.bar._content_h * 2)  # Should clamp to max_scroll
        max_scroll = self.bar._content_h - self.bar._text_rect_size().height()
        self.assertEqual(self.bar._scroll, max_scroll)


class ModelCopyRegressionTests(unittest.TestCase):
    """设置中替换模型失败时保留原模型。"""

    def test_model_replace_error_preserves_existing_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "source.gguf"
            dest = root / "existing.gguf"
            src.write_bytes(b"GGUFnew")
            dest.write_bytes(b"GGUFold")

            worker = ModelCopyWorker(src, dest)
            with patch("app.ui.windows.os.replace", side_effect=OSError("replace failed")):
                worker.run()

            self.assertEqual(dest.read_bytes(), b"GGUFold")
            self.assertFalse(dest.with_name("existing.importing.gguf").exists())

            ModelCopyWorker(src, dest).run()
            self.assertEqual(dest.read_bytes(), b"GGUFnew")


if __name__ == "__main__":
    unittest.main()
