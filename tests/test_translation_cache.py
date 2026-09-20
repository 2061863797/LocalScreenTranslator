# -*- coding: utf-8 -*-
"""End-to-end & component test suite for Subtitle Incremental Translation & 2-Tier Caching.

Covers:
- Feature 7: Subtitle Incremental Translation (PR 7)
- Feature 8: Persistent 2-Tier Caching (PR 8)
Tiers 1-4: Line-diff reuse, L1 latency (<1ms), L2 SQLite latency (<10ms),
composite key invalidation (prompt_ver, model_id, NFC unicode), 50k LRU eviction,
and real-world rolling subtitle workloads.
"""

import collections
import hashlib
import sqlite3
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from typing import Callable

from app.translation_cache import TranslationCache
from app.translation_manager import SubtitleIncrementalTranslator
from app.translator import Translator
from app.storage import Storage, MAX_CACHE_ENTRIES


# Factory helpers directly instantiating production classes
def create_translation_cache(db_path=None, l1_cap=5000, l2_max=50000):
    return TranslationCache(db_path=db_path, l1_capacity=l1_cap, l2_max_entries=l2_max)


def create_incremental_translator(cache, mock_fn=None):
    return SubtitleIncrementalTranslator(cache=cache, mock_translate_fn=mock_fn)


class TestTranslationCacheTiers(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp_dir.name) / "test_cache.db"
        self.cache = create_translation_cache(db_path=self.db_file)
        self.translator = create_incremental_translator(self.cache)

    def tearDown(self):
        if self.cache:
            self.cache.close()
        self.tmp_dir.cleanup()

    # ==========================================
    # Tier 1: Primary Feature Behavior
    # ==========================================

    def test_subtitle_incremental_line_diff(self):
        """Tier 1: Incremental line-diff translates only new lines, reusing cached lines."""
        initial_lines = ["Line Alpha", "Line Beta"]
        sub1, used_cache1 = self.translator.translate_subtitle_lines(initial_lines, "zh")
        self.assertIn("[zh]Line Alpha", sub1)
        self.assertIn("[zh]Line Beta", sub1)
        self.assertEqual(self.translator.lines_translated_count, 2)

        # Second cycle: append Line Gamma
        updated_lines = ["Line Alpha", "Line Beta", "Line Gamma"]
        sub2, used_cache2 = self.translator.translate_subtitle_lines(updated_lines, "zh")
        self.assertTrue(used_cache2)
        # Exactly 1 new line translated
        self.assertEqual(self.translator.lines_translated_count, 3)
        self.assertIn("[zh]Line Gamma", sub2)

    def test_subtitle_line_order_preserved(self):
        """Tier 1: Reconstructed subtitle preserves strict sequential line order."""
        lines = ["Third", "First", "Second"]
        # Pre-seed "First" in cache
        self.cache.put("First", "zh", "default_model", "v1", "第一")

        sub, _ = self.translator.translate_subtitle_lines(lines, "zh")
        expected_ordered = "[zh]Third\n第一\n[zh]Second"
        self.assertEqual(sub, expected_ordered)

    def test_translation_cache_put_and_get(self):
        """Tier 1: Values stored in cache are retrievable with matching parameters."""
        self.cache.put("Hello world", "zh", "qwen", "v1", "你好，世界")
        result = self.cache.get("Hello world", "zh", "qwen", "v1")
        self.assertEqual(result, "你好，世界")

    def test_l1_in_memory_cache_hit(self):
        """Tier 1: L1 cache returns stored value directly from memory."""
        self.cache.put("Fast test", "zh", "model", "v1", "快速测试")
        # Direct L1 verification
        key = self.cache.make_key("Fast test", "zh", "model", "v1")
        self.assertIn(key, self.cache._l1)
        self.assertEqual(self.cache.get("Fast test", "zh", "model", "v1"), "快速测试")

    def test_l2_sqlite_persistent_cache_hit_across_instances(self):
        """Tier 1: Cache entries survive in SQLite and load into new cache instance."""
        self.cache.put("Persistent item", "zh", "model1", "v1", "持久条目")
        self.cache.close()

        # Reopen second instance with same SQLite file
        new_cache = create_translation_cache(db_path=self.db_file)
        try:
            # L1 is empty in new instance
            key = new_cache.make_key("Persistent item", "zh", "model1", "v1")
            self.assertNotIn(key, new_cache._l1)

            # Lookup queries SQLite (L2) and populates L1
            result = new_cache.get("Persistent item", "zh", "model1", "v1")
            self.assertEqual(result, "持久条目")
            self.assertIn(key, new_cache._l1)
        finally:
            new_cache.close()

    # ==========================================
    # Tier 2: Boundary & Corner Cases
    # ==========================================

    def test_l1_hit_latency_strictly_under_1ms(self):
        """Tier 2: L1 in-memory cache hit resolves in strictly < 1.0ms."""
        self.cache.put("Latency Probe", "zh", "m", "v1", "延迟探针")

        # Warmup
        self.cache.get("Latency Probe", "zh", "m", "v1")

        times = []
        for _ in range(50):
            start = time.perf_counter()
            self.cache.get("Latency Probe", "zh", "m", "v1")
            times.append(time.perf_counter() - start)

        avg_ms = (sum(times) / len(times)) * 1000
        p95_ms = sorted(times)[int(len(times) * 0.95)] * 1000

        self.assertLess(avg_ms, 1.0, f"L1 average hit latency {avg_ms:.3f}ms exceeds 1.0ms")
        self.assertLess(p95_ms, 1.0, f"L1 P95 hit latency {p95_ms:.3f}ms exceeds 1.0ms")

    def test_l2_hit_latency_strictly_under_10ms(self):
        """Tier 2: L2 SQLite cache hit resolves in strictly < 10.0ms without server call."""
        # Insert 100 items directly to SQLite
        with self.cache._conn:
            for i in range(100):
                key = self.cache.make_key(f"Source {i}", "zh", "m", "v1")
                self.cache._conn.execute(
                    "INSERT INTO translation_cache (cache_key, source_text, target_lang, model_id, prompt_version, translation, created_at, last_accessed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, f"Source {i}", "zh", "m", "v1", f"翻译 {i}", time.time(), time.time()),
                )

        # Clear L1 to force L2 hits
        self.cache._l1.clear()

        times = []
        for i in range(20):
            self.cache._l1.clear()  # ensure L2 is queried each time
            start = time.perf_counter()
            val = self.cache.get(f"Source {i}", "zh", "m", "v1")
            times.append(time.perf_counter() - start)
            self.assertEqual(val, f"翻译 {i}")

        avg_ms = (sum(times) / len(times)) * 1000
        p95_ms = sorted(times)[int(len(times) * 0.95)] * 1000

        self.assertLess(avg_ms, 10.0, f"L2 average hit latency {avg_ms:.3f}ms exceeds 10.0ms")
        self.assertLess(p95_ms, 10.0, f"L2 P95 hit latency {p95_ms:.3f}ms exceeds 10.0ms")

    def test_composite_cache_key_invalidation_on_prompt_version(self):
        """Tier 2: Changing prompt_version causes cache miss."""
        self.cache.put("Hello", "zh", "modelA", "prompt_v1", "你好V1")
        self.assertEqual(self.cache.get("Hello", "zh", "modelA", "prompt_v1"), "你好V1")

        # Query with prompt_v2
        self.assertIsNone(self.cache.get("Hello", "zh", "modelA", "prompt_v2"))

    def test_composite_cache_key_invalidation_on_model_id(self):
        """Tier 2: Changing model_id causes cache miss."""
        self.cache.put("Hello", "zh", "llama3", "prompt_v1", "你好Llama")
        self.assertEqual(self.cache.get("Hello", "zh", "llama3", "prompt_v1"), "你好Llama")

        # Query with qwen2
        self.assertIsNone(self.cache.get("Hello", "zh", "qwen2", "prompt_v1"))

    def test_composite_cache_key_unicode_normalization(self):
        """Tier 2: Precomposed NFC and decomposed NFD unicode source texts yield identical cache key."""
        # 'é' as precomposed (NFC) vs decomposed (NFD: 'e' + combining acute)
        nfc_str = "café"
        nfd_str = unicodedata.normalize("NFD", nfc_str)
        self.assertNotEqual(nfc_str, nfd_str)  # underlying bytes differ

        k1 = self.cache.make_key(nfc_str, "zh", "m", "v1")
        k2 = self.cache.make_key(nfd_str, "zh", "m", "v1")
        self.assertEqual(k1, k2, "Composite key must normalize unicode using NFC")

        self.cache.put(nfc_str, "zh", "m", "v1", "咖啡馆")
        self.assertEqual(self.cache.get(nfd_str, "zh", "m", "v1"), "咖啡馆")

    def test_lru_eviction_capping_table_size(self):
        """Tier 2: Table size is capped by evicting oldest records based on last_accessed_at."""
        # Use small capacity cache to test boundary eviction
        small_cache = create_translation_cache(db_path=":memory:", l1_cap=10, l2_max=10)
        try:
            # Insert 10 items
            for i in range(10):
                small_cache.put(f"Word_{i}", "zh", "m", "v1", f"字_{i}")
                time.sleep(0.001)

            cur = small_cache._conn.execute("SELECT count(*) FROM translation_cache")
            self.assertEqual(cur.fetchone()[0], 10)

            # Insert 11th item: exceeds capacity 10 -> triggers eviction
            small_cache.put("Word_10", "zh", "m", "v1", "字_10")
            cur = small_cache._conn.execute("SELECT count(*) FROM translation_cache")
            self.assertLessEqual(cur.fetchone()[0], 10)

            # Oldest item (Word_0) should be evicted
            self.assertIsNone(small_cache.get("Word_0", "zh", "m", "v1"))
            # Newest item exists
            self.assertEqual(small_cache.get("Word_10", "zh", "m", "v1"), "字_10")
        finally:
            small_cache.close()

    # ==========================================
    # Tier 3: Pairwise & Component Interactions
    # ==========================================

    def test_incremental_translator_with_partial_hits(self):
        """Tier 3: Multi-line batch with mixed L1 hits, L2 hits, and misses."""
        # Seed Line 1 into L1
        self.cache.put("Line 1", "zh", "default_model", "v1", "第一行")

        # Seed Line 2 directly into L2 (bypass L1)
        k2 = self.cache.make_key("Line 2", "zh", "default_model", "v1")
        with self.cache._conn:
            self.cache._conn.execute(
                "INSERT INTO translation_cache (cache_key, source_text, target_lang, model_id, prompt_version, translation, created_at, last_accessed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (k2, "Line 2", "zh", "default_model", "v1", "第二行", time.time(), time.time()),
            )

        # Translate 3 lines: Line 1 (L1 hit), Line 2 (L2 hit), Line 3 (miss)
        res, used_cache = self.translator.translate_subtitle_lines(["Line 1", "Line 2", "Line 3"], "zh")
        self.assertTrue(used_cache)
        # Exactly 1 LLM translation call for Line 3
        self.assertEqual(self.translator.lines_translated_count, 1)
        self.assertEqual(res, "第一行\n第二行\n[zh]Line 3")

    def test_cache_miss_populates_both_tiers(self):
        """Tier 3: A translation miss translates and populates both L1 and L2."""
        res, used_cache = self.translator.translate_subtitle_lines(["New line to cache"], "zh")
        self.assertFalse(used_cache)

        key = self.cache.make_key("New line to cache", "zh", "default_model", "v1")
        # Present in L1
        self.assertIn(key, self.cache._l1)
        # Present in L2
        cur = self.cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = ?", (key,))
        self.assertIsNotNone(cur.fetchone())

    # ==========================================
    # Tier 4: Real-World Scenarios
    # ==========================================

    def test_scenario_repeated_identical_phrases(self):
        """Tier 4 Scenario 4: Repeated phrases (game dialogs, UI labels) resolve 100% via cache."""
        common_phrases = ["Press Start", "Continue Game", "Options", "Game Over", "Save"]
        # First pass: translates all 5
        self.translator.translate_subtitle_lines(common_phrases, "zh")
        self.assertEqual(self.translator.lines_translated_count, 5)

        initial_llm_calls = self.translator.llm_calls_count

        # Subsequent 10 cycles of the same phrases
        for _ in range(10):
            res, used_cache = self.translator.translate_subtitle_lines(common_phrases, "zh")
            self.assertTrue(used_cache)

        # Zero additional LLM calls
        self.assertEqual(self.translator.llm_calls_count, initial_llm_calls)
        self.assertEqual(self.translator.lines_translated_count, 5)

    def test_scenario_rolling_subtitles_stream(self):
        """Tier 4 Scenario 1: Rolling subtitle window (3 lines visible, shifts down 1 line per frame)."""
        # Window 1: Lines A, B, C
        self.translator.translate_subtitle_lines(["Sub A", "Sub B", "Sub C"], "zh")
        self.assertEqual(self.translator.lines_translated_count, 3)

        # Window 2: Lines B, C, D (A dropped, D added)
        sub2, used2 = self.translator.translate_subtitle_lines(["Sub B", "Sub C", "Sub D"], "zh")
        self.assertTrue(used2)
        # Only Sub D was translated (+1)
        self.assertEqual(self.translator.lines_translated_count, 4)

        # Window 3: Lines C, D, E (B dropped, E added)
        sub3, used3 = self.translator.translate_subtitle_lines(["Sub C", "Sub D", "Sub E"], "zh")
        self.assertTrue(used3)
        # Only Sub E was translated (+1)
        self.assertEqual(self.translator.lines_translated_count, 5)

    def test_l2_cache_eviction_at_50000(self):
        """Tier 2: When SQLite cache reaches 50,001 entries, LRU eviction deletes oldest 1,000 records."""
        # Using an in-memory cache with standard 50,000 max entries
        cache = create_translation_cache(db_path=":memory:", l1_cap=100, l2_max=50000)
        try:
            now = time.time()
            # Fast batch insert 50,000 rows into SQLite
            rows = [
                (
                    f"key_{i}",
                    f"source_{i}",
                    "zh",
                    "model",
                    "v1",
                    f"trans_{i}",
                    now - (50000 - i),
                    now - (50000 - i),
                    1,
                )
                for i in range(50000)
            ]
            with cache._conn:
                cache._conn.executemany(
                    "INSERT INTO translation_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )

            cur = cache._conn.execute("SELECT count(*) FROM translation_cache")
            self.assertEqual(cur.fetchone()[0], 50000)

            # Inserting 50,001st entry triggers eviction of oldest 1,000 entries
            cache.put("Word_50001", "zh", "model", "v1", "字_50001")

            cur = cache._conn.execute("SELECT count(*) FROM translation_cache")
            total = cur.fetchone()[0]
            # Total should now be 50,001 - 1,000 = 49,001 (capped <= 50,000)
            self.assertEqual(total, 49001)

            # Oldest entry (key_0) and 1,000th oldest entry (key_999) must be evicted
            cur = cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = 'key_0'")
            self.assertIsNone(cur.fetchone())
            cur = cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = 'key_999'")
            self.assertIsNone(cur.fetchone())

            # key_1000 was not in the oldest 1,000 and should remain
            cur = cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = 'key_1000'")
            self.assertIsNotNone(cur.fetchone())

            # Newly inserted entry exists
            self.assertEqual(cache.get("Word_50001", "zh", "model", "v1"), "字_50001")
        finally:
            cache.close()

    def test_translator_checks_cache_before_http_calls(self):
        """Tier 3: Translator queries TranslationCache before issuing HTTP calls to llama-server."""
        trans = Translator("http://127.0.0.1:9", {"ctx_size": 2048, "max_tokens": 512}, cache=self.cache)
        try:
            # Seed cache with known translation
            self.cache.put("Cached Source", "简体中文", trans.model_id, trans.prompt_version, "已缓存译文")

            chat_called = False

            def fake_chat(*args, **kwargs):
                nonlocal chat_called
                chat_called = True
                return "从网络翻译"

            trans._chat = fake_chat
            result = trans.translate("Cached Source", "简体中文")
            self.assertEqual(result, "已缓存译文")
            self.assertFalse(chat_called, "Cache hit must not invoke _chat / network")
        finally:
            trans.close()

    def test_translator_caches_and_retrieves_translations(self):
        """Tier 3: Translator stores new translations in TranslationCache for reuse."""
        trans = Translator("http://127.0.0.1:9", {"ctx_size": 2048, "max_tokens": 512}, cache=self.cache)
        try:
            call_count = 0

            def fake_chat(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                return "新网络译文"

            trans._chat = fake_chat
            r1 = trans.translate("Uncached Sentence", "简体中文")
            self.assertEqual(r1, "新网络译文")
            self.assertEqual(call_count, 1)

            # Second call must hit cache
            r2 = trans.translate("Uncached Sentence", "简体中文")
            self.assertEqual(r2, "新网络译文")
            self.assertEqual(call_count, 1)

            # Verified in TranslationCache L1 and L2
            cached_val = self.cache.get("Uncached Sentence", "简体中文", trans.model_id, trans.prompt_version)
            self.assertEqual(cached_val, "新网络译文")
        finally:
            trans.close()


if __name__ == "__main__":
    unittest.main()
