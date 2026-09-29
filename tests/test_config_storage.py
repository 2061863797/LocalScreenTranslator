import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import config
from app.storage import Storage


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.old_path = config.CONFIG_PATH
        self.tmp = tempfile.TemporaryDirectory()
        config.CONFIG_PATH = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        config.CONFIG_PATH = self.old_path
        self.tmp.cleanup()

    def test_non_object_json_falls_back_to_defaults(self):
        config.CONFIG_PATH.write_text("null", encoding="utf-8")
        loaded = config.load()
        self.assertEqual(loaded["model_path"], config.DEFAULTS["model_path"])

    def test_removed_nllb_settings_are_ignored_and_cleaned_on_save(self):
        config.CONFIG_PATH.write_text(
            json.dumps({
                "translation_mode": "fast",
                "nllb_model_path": "runtime/models/nllb",
                "nllb_device": "gpu",
                "window_watch_annotate": False,
                "source_language": "英语",
                "ocr_mode": "single",
                "ocr_language_tag": "en-US",
                "ocr_language_tags": ["en-US"],
                "annotate_skip_target_lang": True,
                "window_annotate_skip_target_lang": True,
                "region_annotate_skip_target_lang": True,
                "ocr_provider": "cpu",
                "ocr_device_id": 1,
                "ocr_score_min": 0.2,
            }),
            encoding="utf-8",
        )
        loaded = config.load()
        self.assertNotIn("translation_mode", loaded)
        self.assertNotIn("nllb_model_path", loaded)
        self.assertNotIn("nllb_device", loaded)
        self.assertNotIn("window_watch_annotate", loaded)
        self.assertNotIn("source_language", loaded)
        self.assertNotIn("ocr_mode", loaded)
        self.assertNotIn("ocr_language_tag", loaded)
        self.assertNotIn("ocr_language_tags", loaded)
        self.assertNotIn("annotate_skip_target_lang", loaded)
        self.assertNotIn("window_annotate_skip_target_lang", loaded)
        self.assertNotIn("region_annotate_skip_target_lang", loaded)
        self.assertEqual(loaded["ocr_provider"], "cpu")
        self.assertEqual(loaded["ocr_device_id"], 1)
        self.assertEqual(loaded["ocr_score_min"], 0.2)

        config.save(loaded)
        saved = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("translation_mode", saved)
        self.assertNotIn("nllb_model_path", saved)
        self.assertNotIn("nllb_device", saved)
        self.assertNotIn("window_watch_annotate", saved)
        self.assertNotIn("source_language", saved)
        self.assertNotIn("ocr_mode", saved)
        self.assertNotIn("ocr_language_tag", saved)
        self.assertNotIn("ocr_language_tags", saved)
        self.assertNotIn("annotate_skip_target_lang", saved)
        self.assertNotIn("window_annotate_skip_target_lang", saved)
        self.assertNotIn("region_annotate_skip_target_lang", saved)
        self.assertEqual(saved["ocr_provider"], "cpu")
        self.assertEqual(saved["ocr_device_id"], 1)
        self.assertEqual(saved["ocr_score_min"], 0.2)

    def test_invalid_known_types_fall_back(self):
        config.CONFIG_PATH.write_text(
            json.dumps({"server_port": "abc", "history_enabled": "false"}),
            encoding="utf-8",
        )
        loaded = config.load()
        self.assertNotIn("server_port", loaded)
        self.assertIs(loaded["history_enabled"], True)

    def test_font_sizes_accept_default_or_12_to_20_pixels(self):
        config.CONFIG_PATH.write_text(
            json.dumps({
                "translate_window_font_size": 11,
                "window_watch_font_size": 20,
                "region_watch_font_size": 21,
            }),
            encoding="utf-8",
        )
        loaded = config.load()
        self.assertEqual(loaded["translate_window_font_size"], 0)
        self.assertEqual(loaded["window_watch_font_size"], 20)
        self.assertEqual(loaded["region_watch_font_size"], 0)

    def test_invalid_max_tokens_falls_back_to_default(self):
        config.CONFIG_PATH.write_text(
            json.dumps({"max_tokens": 8193}),
            encoding="utf-8",
        )
        loaded = config.load()
        self.assertEqual(loaded["max_tokens"], 512)

    def test_removed_screenshot_ocr_hotkey_is_ignored(self):
        config.CONFIG_PATH.write_text(
            json.dumps({"hotkey_silent_ocr": "<alt>+s"}),
            encoding="utf-8",
        )
        loaded = config.load()
        self.assertNotIn("hotkey_silent_ocr", loaded)

    def test_save_is_atomic_and_readable(self):
        data = dict(config.DEFAULTS)
        data["target_language"] = "英语"
        config.save(data)
        self.assertFalse(config.CONFIG_PATH.with_name("config.json.tmp").exists())
        self.assertEqual(json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))["target_language"], "英语")


class StorageTests(unittest.TestCase):
    def test_legacy_table_is_preserved_and_history_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "data.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE vocabulary (word TEXT)")
            conn.execute("INSERT INTO vocabulary VALUES ('keep')")
            conn.commit()
            conn.close()

            storage = Storage(db)
            storage.add_history("a", "b", "test")
            storage.clear_history()
            self.assertEqual(storage.recent_history(), [])
            row = storage._conn.execute("SELECT word FROM vocabulary").fetchone()
            self.assertEqual(row, ("keep",))
            storage.close()

    def test_history_stays_bounded_and_newest_wins(self):
        """持续翻译每轮都写历史：必须只留最新 N 条，且最新一条排在最前。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "data.db"
            storage = Storage(db)
            try:
                for index in range(400):
                    storage.add_history(f"源{index}", f"译{index}", "window_subtitle")
                rows = storage.recent_history(1000)
                self.assertEqual(len(rows), 50)
                self.assertEqual(rows[0][1], "源399")
            finally:
                storage.close()

    def test_compaction_actually_shrinks_the_file_on_disk(self):
        """VACUUM 只写 WAL：不 checkpoint 的话页数降了、文件却不变小。

        空闲页要「先长大再删光」才积得起来：稳态下删一行腾出的页会被下一行
        立刻复用，本机那个 950KB 的库是长期大文本历史删空后留下的。
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "data.db"
            storage = Storage(db)
            try:
                for index in range(100):
                    storage.add_history("源" * 20000, "译" * 20000, "window_subtitle")
                storage.clear_history()
                idle_pages = storage._conn.execute("PRAGMA freelist_count").fetchone()[0]
                self.assertGreater(idle_pages, 0)
                before = db.stat().st_size
                storage._compact()
                self.assertEqual(
                    storage._conn.execute("PRAGMA freelist_count").fetchone()[0], 0
                )
                self.assertLess(db.stat().st_size, before)
            finally:
                storage.close()

    def test_compaction_keeps_history_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "data.db"
            storage = Storage(db)
            try:
                for index in range(400):
                    storage.add_history(f"源{index}", f"译{index}", "window_subtitle")
                before = storage.recent_history()
                storage._compact()
                self.assertEqual(storage.recent_history(), before)
            finally:
                storage.close()

    def test_under_limit_writes_do_not_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "data.db"
            storage = Storage(db)
            try:
                storage.add_history("a", "1", "test")
                statements = []
                storage._conn.set_trace_callback(statements.append)
                try:
                    storage.add_history("b", "2", "test")
                finally:
                    storage._conn.set_trace_callback(None)
                # 未超上限时不该出现清理语句，避免每条插入都制造页面垃圾
                self.assertFalse([sql for sql in statements if "NOT IN" in sql.upper()])
                self.assertEqual(len(storage.recent_history()), 2)
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
