# -*- coding: utf-8 -*-
"""回归验证：关闭时的写入、推理线程、模型替换与持久化缓存。"""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from app.config import DEFAULTS
from app.storage import Storage
from app.translator import Translator
from app.ui.windows import ModelCopyWorker
from app.window_watcher import WindowWatcher


class AuditRegressionTests(unittest.TestCase):
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

    def test_clear_history_waits_for_queued_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "history.db", batch_size=1)
            entered = threading.Event()
            release = threading.Event()
            cleared = threading.Event()
            results = []
            original_commit = storage._commit_history_batch

            def slow_commit(batch):
                entered.set()
                release.wait(10)
                original_commit(batch)

            def clear():
                results.append(storage.clear_history())
                cleared.set()

            storage._commit_history_batch = slow_commit
            thread = threading.Thread(target=clear)
            try:
                storage.add_history_async("queued", "translated", "test")
                self.assertTrue(entered.wait(2))
                thread.start()
                self.assertFalse(cleared.wait(0.1))
            finally:
                release.set()
                if thread.ident is not None:
                    thread.join(3)
            self.assertEqual(results, [True])
            self.assertEqual(storage.count_records(), 0)
            self.assertTrue(storage.close())

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

    def test_clear_cache_removes_memory_and_sqlite_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "cache.db")
            translator = Translator(
                base_url="http://127.0.0.1:8080",
                cfg={"translation_cache_enabled": True},
                storage=storage,
            )
            try:
                translator._cache_put("private text", "en", "translated")
                storage.add_history("history source", "history translation", "test")
                self.assertEqual(storage.count_translation_cache(), 1)
                self.assertTrue(translator._line_cache)
                held = threading.Event()
                release = threading.Event()

                def hold_translation_lock():
                    with translator._lock:
                        held.set()
                        release.wait(3)

                owner = threading.Thread(target=hold_translation_lock)
                owner.start()
                try:
                    self.assertTrue(held.wait(1))
                    self.assertFalse(translator.clear_cache())
                    self.assertEqual(storage.count_translation_cache(), 1)
                finally:
                    release.set()
                    owner.join(3)
                self.assertTrue(translator.clear_cache())
                self.assertEqual(storage.count_translation_cache(), 0)
                self.assertEqual(storage.count_records(), 1)
                self.assertFalse(translator._line_cache)
            finally:
                translator.close()
                storage.close()


if __name__ == "__main__":
    unittest.main()