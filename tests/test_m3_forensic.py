# -*- coding: utf-8 -*-
"""Milestone 3 Forensic Audit & Adversarial Integrity Test Suite.

Independently tests and verifies:
1. SQLite schema and indexing on translation_cache table.
2. Cryptographic SHA256 composite keys and Unicode NFC normalization.
3. 2-Tier caching behavior (L1 hit zero-SQL, L2 hit repopulating L1, eviction).
4. Subtitle incremental translation ordering, diff detection, and cache reuse.
5. Translator component cache integration without network calls on hits.
6. Thread safety and concurrent load robustness.
7. Empirical latency contracts (< 1ms L1, < 10ms L2).
"""

import collections
import hashlib
import sqlite3
import tempfile
import threading
import time
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.storage import Storage, MAX_CACHE_ENTRIES, CACHE_EVICTION_BATCH
from app.translation_cache import TranslationCache
from app.translation_manager import SubtitleIncrementalTranslator, TranslationManager
from app.translator import Translator


class TestMilestone3Forensics(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "forensic_cache.db"
        self.storage = Storage(db_path=self.db_path)
        self.cache = TranslationCache(storage=self.storage, l1_capacity=50, l2_max_entries=200)

    def tearDown(self):
        try:
            self.cache.close()
            self.storage.close()
        except Exception:
            pass
        self.tmp_dir.cleanup()

    # =========================================================================
    # Phase 1: SQLite Schema & Storage Integrity Verification
    # =========================================================================

    def test_schema_translation_cache_table_and_columns(self):
        """Forensic check: Table exists with exact columns, types, and primary key."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(translation_cache)")
            columns = {row[1]: {"type": row[2].upper(), "pk": bool(row[5])} for row in cur.fetchall()}

            expected_cols = {
                "cache_key": {"type": "TEXT", "pk": True},
                "source_text": {"type": "TEXT", "pk": False},
                "target_lang": {"type": "TEXT", "pk": False},
                "model_id": {"type": "TEXT", "pk": False},
                "prompt_version": {"type": "TEXT", "pk": False},
                "translation": {"type": "TEXT", "pk": False},
                "created_at": {"type": "REAL", "pk": False},
                "last_accessed_at": {"type": "REAL", "pk": False},
                "access_count": {"type": "INTEGER", "pk": False},
            }

            for col_name, expected_meta in expected_cols.items():
                self.assertIn(col_name, columns, f"Column '{col_name}' missing from translation_cache schema")
                self.assertEqual(
                    columns[col_name]["type"],
                    expected_meta["type"],
                    f"Column '{col_name}' type mismatch",
                )
                self.assertEqual(
                    columns[col_name]["pk"],
                    expected_meta["pk"],
                    f"Column '{col_name}' primary key mismatch",
                )
        finally:
            conn.close()

    def test_schema_index_on_last_accessed_at(self):
        """Forensic check: Index idx_trans_cache_access exists on last_accessed_at."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA index_list(translation_cache)")
            indexes = [row[1] for row in cur.fetchall()]
            self.assertIn("idx_trans_cache_access", indexes, "Index 'idx_trans_cache_access' missing from table")

            cur.execute("PRAGMA index_info(idx_trans_cache_access)")
            indexed_columns = [row[2] for row in cur.fetchall()]
            self.assertIn("last_accessed_at", indexed_columns, "Index does not index last_accessed_at")
        finally:
            conn.close()

    # =========================================================================
    # Phase 2: Cryptographic Composite Key & Unicode Normalization
    # =========================================================================

    def test_crypto_composite_key_exact_hash(self):
        """Forensic check: make_key computes uncompromised SHA256 with exact null-byte framing."""
        source = "  Hello World  "
        target = "fr"
        model_id = "test-model-42"
        prompt_version = "v3.1"

        expected_normalized = "Hello World"
        expected_raw = f"{expected_normalized}\0{target}\0{model_id}\0{prompt_version}"
        expected_hash = hashlib.sha256(expected_raw.encode("utf-8")).hexdigest()

        computed_key = self.cache.make_key(source, target, model_id, prompt_version)
        self.assertEqual(computed_key, expected_hash, "make_key must match genuine SHA256 of raw null-separated key")

    def test_crypto_composite_key_unicode_nfc_normalization(self):
        """Forensic check: Decomposed Unicode characters (NFD) normalize to NFC in cache key."""
        test_pairs = [
            # Precomposed vs Decomposed 'e with acute'
            ("caf\u00e9", "cafe\u0301"),
            # Precomposed vs Decomposed German Umlaut 'u with diaeresis'
            ("\u00fcber", "u\u0308ber"),
            # Precomposed vs Decomposed Korean syllable 'Ga'
            ("\uac00", "\u1100\u1161"),
            # Precomposed vs Decomposed Japanese Dakuten
            ("\u304c", "\u304b\u3099"),
        ]

        for precomposed, decomposed in test_pairs:
            self.assertNotEqual(precomposed.encode("utf-8"), decomposed.encode("utf-8"), "Inputs must differ in raw byte form")
            k1 = self.cache.make_key(precomposed, "zh")
            k2 = self.cache.make_key(decomposed, "zh")
            self.assertEqual(k1, k2, f"Failed NFC normalization equivalence for {precomposed!r} vs {decomposed!r}")

    def test_crypto_composite_key_invalidation_matrix(self):
        """Forensic check: Cache keys differentiate across all four tuple components."""
        base_args = ("Test string", "zh", "model_1", "v1")
        base_key = self.cache.make_key(*base_args)

        # Invalidate source
        k_src = self.cache.make_key("Test string modified", "zh", "model_1", "v1")
        self.assertNotEqual(base_key, k_src)

        # Invalidate target
        k_tgt = self.cache.make_key("Test string", "ja", "model_1", "v1")
        self.assertNotEqual(base_key, k_tgt)

        # Invalidate model
        k_mod = self.cache.make_key("Test string", "zh", "model_2", "v1")
        self.assertNotEqual(base_key, k_mod)

        # Invalidate prompt version
        k_pver = self.cache.make_key("Test string", "zh", "model_1", "v2")
        self.assertNotEqual(base_key, k_pver)

    # =========================================================================
    # Phase 3: 2-Tier Caching Flow & Empirical Eviction
    # =========================================================================

    def test_2tier_l1_hit_bypasses_sqlite(self):
        """Forensic check: An L1 hit returns immediately without touching SQLite connection."""
        self.cache.put("L1 bypass test", "zh", "m", "v1", "L1译文")

        original_conn = self.cache._conn
        execute_calls = 0

        class ConnSpy:
            def __getattr__(self, name):
                return getattr(original_conn, name)

            def execute(self, *args, **kwargs):
                nonlocal execute_calls
                execute_calls += 1
                return original_conn.execute(*args, **kwargs)

        self.cache._conn = ConnSpy()
        try:
            val = self.cache.get("L1 bypass test", "zh", "m", "v1")
            self.assertEqual(val, "L1译文")
            self.assertEqual(execute_calls, 0, "L1 hit MUST NOT execute any SQLite statements")
        finally:
            self.cache._conn = original_conn

    def test_2tier_l2_hit_repopulates_l1(self):
        """Forensic check: An L2 hit updates access statistics and repopulates L1."""
        self.cache.put("L2 repopulate test", "zh", "m", "v1", "L2译文")

        # Clear L1 to force L2 resolution
        self.cache._l1.clear()
        key = self.cache.make_key("L2 repopulate test", "zh", "m", "v1")
        self.assertNotIn(key, self.cache._l1)

        # Before L2 hit: access_count was 1 from put
        cur = self.cache._conn.execute("SELECT access_count, last_accessed_at FROM translation_cache WHERE cache_key = ?", (key,))
        row1 = cur.fetchone()
        self.assertEqual(row1[0], 1)
        t_insert = row1[1]

        time.sleep(0.01)
        val = self.cache.get("L2 repopulate test", "zh", "m", "v1")
        self.assertEqual(val, "L2译文")

        # L1 is repopulated
        self.assertIn(key, self.cache._l1)
        self.assertEqual(self.cache._l1[key], "L2译文")

        # SQLite access_count incremented to 2, last_accessed_at updated
        cur = self.cache._conn.execute("SELECT access_count, last_accessed_at FROM translation_cache WHERE cache_key = ?", (key,))
        row2 = cur.fetchone()
        self.assertEqual(row2[0], 2)
        self.assertGreater(row2[1], t_insert)

    def test_2tier_l1_eviction_boundary(self):
        """Forensic check: Exceeding l1_capacity evicts least-recently-used in memory, but retains in L2."""
        small_cache = TranslationCache(db_path=Path(self.tmp_dir.name) / "l1_test.db", l1_capacity=3, l2_max_entries=100)
        try:
            small_cache.put("item1", "zh", "m", "v1", "t1")
            small_cache.put("item2", "zh", "m", "v1", "t2")
            small_cache.put("item3", "zh", "m", "v1", "t3")

            # All 3 in L1
            self.assertEqual(len(small_cache._l1), 3)

            # Insert 4th item -> item1 should be evicted from L1
            small_cache.put("item4", "zh", "m", "v1", "t4")
            self.assertEqual(len(small_cache._l1), 3)

            k1 = small_cache.make_key("item1", "zh", "m", "v1")
            self.assertNotIn(k1, small_cache._l1, "Oldest item1 must be evicted from L1")

            # But item1 is still retrieved from L2 SQLite!
            res1 = small_cache.get("item1", "zh", "m", "v1")
            self.assertEqual(res1, "t1")
            # And now it's back in L1!
            self.assertIn(k1, small_cache._l1)
        finally:
            small_cache.close()

    def test_2tier_l2_eviction_boundary(self):
        """Forensic check: When L2 capacity is exceeded, oldest entries by last_accessed_at are deleted."""
        micro_cache = TranslationCache(db_path=Path(self.tmp_dir.name) / "l2_test.db", l1_capacity=10, l2_max_entries=5)
        try:
            for i in range(5):
                micro_cache.put(f"item_{i}", "zh", "m", "v1", f"trans_{i}")
                time.sleep(0.002)

            cur = micro_cache._conn.execute("SELECT count(*) FROM translation_cache")
            self.assertEqual(cur.fetchone()[0], 5)

            # Insert 6th item -> total exceeds 5 -> evicts oldest item_0
            micro_cache.put("item_5", "zh", "m", "v1", "trans_5")
            cur = micro_cache._conn.execute("SELECT count(*) FROM translation_cache")
            self.assertLessEqual(cur.fetchone()[0], 5)

            # item_0 must be gone from L2
            k0 = micro_cache.make_key("item_0", "zh", "m", "v1")
            cur = micro_cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = ?", (k0,))
            self.assertIsNone(cur.fetchone())

            # item_5 must be present
            k5 = micro_cache.make_key("item_5", "zh", "m", "v1")
            cur = micro_cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = ?", (k5,))
            self.assertIsNotNone(cur.fetchone())
        finally:
            micro_cache.close()

    # =========================================================================
    # Phase 4: Subtitle Incremental Translation Ordering & Line-Diff
    # =========================================================================

    def test_incremental_translation_preserves_strict_multiline_order(self):
        """Forensic check: Interleaved cached and uncached lines are assembled in exact original order."""
        translator = SubtitleIncrementalTranslator(cache=self.cache)

        # Prepopulate cache for even numbered lines
        for i in range(0, 10, 2):
            self.cache.put(f"Line {i}", "zh", translator.model_id, translator.prompt_version, f"第{i}行(缓存)")

        input_lines = [f"Line {i}" for i in range(10)]
        result_sub, used_cache = translator.translate_subtitle_lines(input_lines, "zh")

        self.assertTrue(used_cache)
        # Only odd lines (5 lines) were translated
        self.assertEqual(translator.lines_translated_count, 5)

        output_lines = result_sub.split("\n")
        self.assertEqual(len(output_lines), 10)

        for i in range(10):
            if i % 2 == 0:
                self.assertEqual(output_lines[i], f"第{i}行(缓存)")
            else:
                self.assertEqual(output_lines[i], f"[zh]Line {i}")

    def test_incremental_translation_empty_and_whitespace_lines(self):
        """Forensic check: Pure whitespace lines are sanitized without breaking line processing."""
        translator = SubtitleIncrementalTranslator(cache=self.cache)
        sub, used = translator.translate_subtitle_lines(["   ", "\t\t", ""], "zh")
        self.assertEqual(sub, "")
        self.assertFalse(used)
        self.assertEqual(translator.llm_calls_count, 0)

    # =========================================================================
    # Phase 5: Translator Component Integration
    # =========================================================================

    def test_translator_skips_http_on_cached_input(self):
        """Forensic check: Translator.translate checks TranslationCache and skips network calls on hit."""
        trans = Translator("http://127.0.0.1:9", {"ctx_size": 2048, "max_tokens": 512}, cache=self.cache)
        try:
            self.cache.put("Hit Sentence", "简体中文", trans.model_id, trans.prompt_version, "命中文本")

            network_called = False

            def mock_chat(*args, **kwargs):
                nonlocal network_called
                network_called = True
                return "网络翻译"

            trans._chat = mock_chat
            out = trans.translate("Hit Sentence", "简体中文")
            self.assertEqual(out, "命中文本")
            self.assertFalse(network_called, "Cached hit must not invoke _chat")
        finally:
            trans.close()

    def test_translator_populates_cache_after_successful_inference(self):
        """Forensic check: Translator stores translated text in TranslationCache upon completion."""
        trans = Translator("http://127.0.0.1:9", {"ctx_size": 2048, "max_tokens": 512}, cache=self.cache)
        try:
            trans._chat = lambda prompt, max_tok: "真网络译文"
            res = trans.translate("Uncached text string", "简体中文")
            self.assertEqual(res, "真网络译文")

            # Check cache directly
            cached = self.cache.get("Uncached text string", "简体中文", trans.model_id, trans.prompt_version)
            self.assertEqual(cached, "真网络译文")
        finally:
            trans.close()

    # =========================================================================
    # Phase 6: Concurrency & Thread-Safety Robustness
    # =========================================================================

    def test_concurrent_access_stress(self):
        """Forensic check: Multiple concurrent threads perform gets and puts without deadlocks or corruption."""
        num_threads = 8
        ops_per_thread = 50
        errors = []

        def worker(thread_idx: int):
            try:
                for i in range(ops_per_thread):
                    word = f"thread_{thread_idx}_word_{i % 10}"
                    self.cache.put(word, "zh", "m", "v1", f"trans_{thread_idx}_{i}")
                    val = self.cache.get(word, "zh", "m", "v1")
                    if val is None:
                        errors.append(f"Unexpected None for {word}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [], f"Concurrent thread errors encountered: {errors}")

    # =========================================================================
    # Phase 7: Latency Benchmark Contracts
    # =========================================================================

    def test_benchmark_l1_and_l2_latency_strict(self):
        """Forensic check: Empirical validation of < 1ms L1 and < 10ms L2 latency requirements."""
        # Warmup and insert 50 entries
        for i in range(50):
            self.cache.put(f"bench_key_{i}", "zh", "m", "v1", f"bench_val_{i}")

        # L1 Benchmarks (100 runs)
        l1_times = []
        for i in range(100):
            t0 = time.perf_counter()
            _ = self.cache.get(f"bench_key_{i % 50}", "zh", "m", "v1")
            l1_times.append(time.perf_counter() - t0)

        l1_avg_ms = (sum(l1_times) / len(l1_times)) * 1000
        l1_p95_ms = sorted(l1_times)[int(len(l1_times) * 0.95)] * 1000
        self.assertLess(l1_avg_ms, 1.0, f"L1 avg {l1_avg_ms:.3f}ms exceeds 1.0ms contract")
        self.assertLess(l1_p95_ms, 1.0, f"L1 p95 {l1_p95_ms:.3f}ms exceeds 1.0ms contract")

        # L2 Benchmarks (50 runs with cleared L1)
        l2_times = []
        for i in range(50):
            self.cache._l1.clear()
            t0 = time.perf_counter()
            val = self.cache.get(f"bench_key_{i}", "zh", "m", "v1")
            l2_times.append(time.perf_counter() - t0)
            self.assertEqual(val, f"bench_val_{i}")

        l2_avg_ms = (sum(l2_times) / len(l2_times)) * 1000
        l2_p95_ms = sorted(l2_times)[int(len(l2_times) * 0.95)] * 1000
        self.assertLess(l2_avg_ms, 10.0, f"L2 avg {l2_avg_ms:.3f}ms exceeds 10.0ms contract")
        self.assertLess(l2_p95_ms, 10.0, f"L2 p95 {l2_p95_ms:.3f}ms exceeds 10.0ms contract")


if __name__ == "__main__":
    unittest.main()
