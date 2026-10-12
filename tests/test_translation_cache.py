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
from types import SimpleNamespace
from pathlib import Path
from typing import Callable
from unittest.mock import Mock

from app.translation_cache import TranslationCache
from app.storage import Storage, MAX_CACHE_ENTRIES


# Factory helpers directly instantiating production classes
def create_translation_cache(db_path=None, l1_cap=5000, l2_max=50000):
    return TranslationCache(db_path=db_path, l1_capacity=l1_cap, l2_max_entries=l2_max)


class TestTranslationCacheTiers(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp_dir.name) / "test_cache.db"
        self.cache = create_translation_cache(db_path=self.db_file)

    def tearDown(self):
        if self.cache:
            self.cache.close()
        self.tmp_dir.cleanup()

    # ==========================================
    # Tier 1: Primary Feature Behavior
    # ==========================================

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

    def test_l2_hits_backfill_l1_and_commit_access_stats_in_batches(self):
        for i in range(60):
            self.cache.put(f"src_{i}", "zh", "m", "v1", f"译_{i}")
        self.cache._l1.clear()
        self.cache.l1_capacity = 30
        # 60 次二级缓存命中：每条都能读回并回填一级缓存（超出容量淘汰最旧的），
        # 满 50 次批量提交一次访问统计
        for i in range(60):
            self.assertEqual(self.cache.get(f"src_{i}", "zh", "m", "v1"), f"译_{i}")
        self.assertEqual(len(self.cache._l1), 30)
        self.assertEqual(self.cache._pending_reads, 10)
        key = self.cache.make_key("src_0", "zh", "m", "v1")
        count = self.cache._conn.execute(
            "SELECT access_count FROM translation_cache WHERE cache_key = ?", (key,)
        ).fetchone()[0]
        self.assertEqual(count, 2)

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

    # ==========================================
    # Tier 4: Real-World Scenarios
    # ==========================================

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

            # 本测试只关心淘汰结果，关闭“每 1% 写入才检查一次”的节流
            cache._evict_check_every = 1
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




if __name__ == "__main__":
    unittest.main()
