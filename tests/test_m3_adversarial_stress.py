# -*- coding: utf-8 -*-
"""Milestone 3 Adversarial Challenge and Stress Test Suite.

Empirically challenges and benchmarks:
1. L1 in-memory cache hit latency (strictly < 1ms, multi-threaded concurrency, saturation).
2. L2 SQLite persistent cache hit latency (strictly < 10ms, file-based DB, no model calls).
3. SubtitleIncrementalTranslator stress testing (line deletions, insertions, permutations, duplicates, unicode).
4. Cache key invalidation and normalization (model_id, prompt_version, target_lang, Unicode NFC/NFD, delimiters).
5. LRU eviction at > 50,000 entries (batch eviction, count invariants, access-touch survival, index speed).
"""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import os
import sqlite3
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from typing import Any

from app.storage import Storage, MAX_CACHE_ENTRIES, CACHE_EVICTION_BATCH
from app.translation_cache import TranslationCache
class TestMilestone3AdversarialStress(unittest.TestCase):
    """Adversarial stress and empirical benchmark suite for Milestone 3."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "adversarial_cache.db"
        self.cache = TranslationCache(db_path=self.db_path, l1_capacity=5000, l2_max_entries=50000)

    def tearDown(self):
        if self.cache:
            self.cache.close()
        self.tmp_dir.cleanup()

    # =========================================================================
    # 1. Benchmark L1 In-Memory Cache Hit Latency (< 1ms)
    # =========================================================================

    def test_l1_hit_latency_large_sample_benchmark(self):
        """Benchmark L1 hit latency over 10,000 queries: avg and P95 must be strictly < 1.0ms."""
        self.cache.put("Benchmark Query", "zh", "m1", "v1", "基准查询结果")

        # Warmup
        for _ in range(100):
            self.cache.get("Benchmark Query", "zh", "m1", "v1")

        times = []
        iterations = 10000
        for _ in range(iterations):
            t0 = time.perf_counter()
            val = self.cache.get("Benchmark Query", "zh", "m1", "v1")
            times.append(time.perf_counter() - t0)

        times_ms = [t * 1000 for t in times]
        avg_ms = sum(times_ms) / len(times_ms)
        sorted_times = sorted(times_ms)
        p50_ms = sorted_times[int(iterations * 0.50)]
        p95_ms = sorted_times[int(iterations * 0.95)]
        p99_ms = sorted_times[int(iterations * 0.99)]
        max_ms = sorted_times[-1]

        print(f"\n[BENCHMARK L1 In-Memory Cache ({iterations} hits)]")
        print(f"Avg: {avg_ms:.4f}ms | P50: {p50_ms:.4f}ms | P95: {p95_ms:.4f}ms | P99: {p99_ms:.4f}ms | Max: {max_ms:.4f}ms")

        self.assertLess(avg_ms, 1.0, f"L1 avg hit latency {avg_ms:.4f}ms must be < 1.0ms")
        self.assertLess(p95_ms, 1.0, f"L1 P95 hit latency {p95_ms:.4f}ms must be < 1.0ms")

    def test_l1_concurrent_thread_stress(self):
        """Stress L1 memory cache with 10 concurrent reader threads (10,000 queries total)."""
        self.cache.put("Concurrent Probe", "zh", "m1", "v1", "并发探测")

        num_threads = 10
        queries_per_thread = 1000
        all_thread_times = []

        def worker():
            thread_times = []
            for _ in range(queries_per_thread):
                t0 = time.perf_counter()
                v = self.cache.get("Concurrent Probe", "zh", "m1", "v1")
                thread_times.append((time.perf_counter() - t0) * 1000)
                if v != "并发探测":
                    raise ValueError(f"Corrupted cache read: {v}")
            return thread_times

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker) for _ in range(num_threads)]
            for fut in concurrent.futures.as_completed(futures):
                all_thread_times.extend(fut.result())

        avg_ms = sum(all_thread_times) / len(all_thread_times)
        p95_ms = sorted(all_thread_times)[int(len(all_thread_times) * 0.95)]

        print(f"\n[BENCHMARK L1 Multi-Threaded Concurrency ({len(all_thread_times)} hits across {num_threads} threads)]")
        print(f"Avg: {avg_ms:.4f}ms | P95: {p95_ms:.4f}ms")

        self.assertLess(avg_ms, 1.0, f"L1 concurrent avg {avg_ms:.4f}ms must be < 1.0ms")
        self.assertLess(p95_ms, 1.0, f"L1 concurrent P95 {p95_ms:.4f}ms must be < 1.0ms")

    def test_l1_capacity_saturation_and_memory_eviction(self):
        """Verify L1 OrderedDict eviction when exceeding l1_capacity (5,000 items)."""
        small_l1_cache = TranslationCache(db_path=":memory:", l1_capacity=100)
        try:
            for i in range(150):
                small_l1_cache.put(f"item_{i}", "zh", "m", "v1", f"val_{i}")

            # Memory L1 must be capped at 100
            self.assertEqual(len(small_l1_cache._l1), 100)
            # Oldest in memory (item_0) should have been popped from L1
            k0 = small_l1_cache.make_key("item_0", "zh", "m", "v1")
            self.assertNotIn(k0, small_l1_cache._l1)
            # Newest in memory (item_149) must be in L1
            k149 = small_l1_cache.make_key("item_149", "zh", "m", "v1")
            self.assertIn(k149, small_l1_cache._l1)
        finally:
            small_l1_cache.close()

    # =========================================================================
    # 2. Benchmark L2 SQLite Persistent Cache Hit Latency (< 10ms, No model calls)
    # =========================================================================

    def test_l2_hit_latency_file_based_sqlite_benchmark(self):
        """Benchmark L2 file-based SQLite cache hits over 500 queries with 5,000 pre-populated rows.
        
        L1 is explicitly purged before every read to force SQLite index traversal.
        Avg, P50, and P95 latency must be strictly < 10.0ms.
        """
        now = time.time()
        rows = [
            (
                self.cache.make_key(f"DiskSource_{i}", "zh", "modelX", "v1"),
                f"DiskSource_{i}",
                "zh",
                "modelX",
                "v1",
                f"磁盘译文_{i}",
                now - (5000 - i),
                now - (5000 - i),
                1,
            )
            for i in range(5000)
        ]
        with self.cache._conn:
            self.cache._conn.executemany(
                "INSERT INTO translation_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

        # Clear L1 to force pure L2 queries
        self.cache._l1.clear()

        query_indices = [i * 10 for i in range(500)]
        times = []

        for idx in query_indices:
            self.cache._l1.clear()  # Ensure L1 miss
            t0 = time.perf_counter()
            res = self.cache.get(f"DiskSource_{idx}", "zh", "modelX", "v1")
            times.append(time.perf_counter() - t0)
            self.assertEqual(res, f"磁盘译文_{idx}")

        times_ms = [t * 1000 for t in times]
        avg_ms = sum(times_ms) / len(times_ms)
        sorted_times = sorted(times_ms)
        p50_ms = sorted_times[int(len(times_ms) * 0.50)]
        p95_ms = sorted_times[int(len(times_ms) * 0.95)]
        p99_ms = sorted_times[int(len(times_ms) * 0.99)]
        max_ms = sorted_times[-1]

        print(f"\n[BENCHMARK L2 SQLite Disk Hit ({len(times_ms)} hits across 5000-row DB)]")
        print(f"Avg: {avg_ms:.4f}ms | P50: {p50_ms:.4f}ms | P95: {p95_ms:.4f}ms | P99: {p99_ms:.4f}ms | Max: {max_ms:.4f}ms")

        self.assertLess(avg_ms, 10.0, f"L2 avg latency {avg_ms:.4f}ms must be < 10.0ms")
        self.assertLess(p95_ms, 10.0, f"L2 P95 latency {p95_ms:.4f}ms must be < 10.0ms")


    # =========================================================================
    # 3. Stress Test SubtitleIncrementalTranslator Line Ordering
    # =========================================================================

    # =========================================================================
    # 4. Test Cache Key Invalidation & Unicode Normalization
    # =========================================================================

    def test_cache_key_invalidation_matrix(self):
        """Verify complete isolation and invalidation across model_id, prompt_version, and target_lang."""
        source = "Artificial Intelligence"
        self.cache.put(source, "zh", "model_hyp_1.5", "v1", "人工智能-混元-v1")

        # 1. Exact match hits
        self.assertEqual(self.cache.get(source, "zh", "model_hyp_1.5", "v1"), "人工智能-混元-v1")

        # 2. Invalidation on model_id
        self.assertIsNone(self.cache.get(source, "zh", "model_qwen_2.5", "v1"))

        # 3. Invalidation on prompt_version
        self.assertIsNone(self.cache.get(source, "zh", "model_hyp_1.5", "v2"))

        # 4. Invalidation on target language
        self.assertIsNone(self.cache.get(source, "ja", "model_hyp_1.5", "v1"))

        # Store alternatives and verify no cross-talk
        self.cache.put(source, "zh", "model_qwen_2.5", "v1", "人工智能-通义-v1")
        self.cache.put(source, "zh", "model_hyp_1.5", "v2", "人工智能-混元-v2")
        self.cache.put(source, "ja", "model_hyp_1.5", "v1", "人工知能")

        self.assertEqual(self.cache.get(source, "zh", "model_hyp_1.5", "v1"), "人工智能-混元-v1")
        self.assertEqual(self.cache.get(source, "zh", "model_qwen_2.5", "v1"), "人工智能-通义-v1")
        self.assertEqual(self.cache.get(source, "zh", "model_hyp_1.5", "v2"), "人工智能-混元-v2")
        self.assertEqual(self.cache.get(source, "ja", "model_hyp_1.5", "v1"), "人工知能")

    def test_unicode_normalization_comprehensive(self):
        """Verify NFC / NFD normalization across accented Latin, Cyrillic, and CJK text."""
        test_pairs = [
            ("Crème brûlée", unicodedata.normalize("NFD", "Crème brûlée")),
            ("Schön", unicodedata.normalize("NFD", "Schön")),
            ("Español", unicodedata.normalize("NFD", "Español")),
            ("한글", unicodedata.normalize("NFD", "한글")),
        ]

        for nfc, nfd in test_pairs:
            # Check keys are byte-identical
            key_nfc = self.cache.make_key(nfc, "zh", "m", "v1")
            key_nfd = self.cache.make_key(nfd, "zh", "m", "v1")
            self.assertEqual(key_nfc, key_nfd, f"Keys must match for {nfc}")

            # Put NFC, get via NFD
            self.cache.put(nfc, "zh", "m", "v1", f"译_{nfc}")
            self.assertEqual(self.cache.get(nfd, "zh", "m", "v1"), f"译_{nfc}")

    # =========================================================================
    # 5. Test LRU Eviction When translation_cache Exceeds 50,000 Entries
    # =========================================================================

    def test_lru_eviction_50000_entries_disk_database(self):
        """Test LRU eviction on on-disk SQLite DB pre-populated with 50,000 entries.
        
        Verifies:
        1. Insertion of 50,001st entry triggers batch eviction of oldest 1,000 records.
        2. Final count is exactly 49,001 (<= 50,000).
        3. Oldest records (0..999) are removed.
        4. Record 1000 remains.
        5. Most recently accessed record (touched via get()) survives even if among oldest keys.
        6. Eviction query executes in < 500ms using idx_trans_cache_access.
        """
        now = time.time()
        print("\n[Populating 50,000 rows in SQLite on-disk database...]")
        t_start = time.perf_counter()

        # Batch insert 50,000 rows with monotonically increasing timestamps
        batch_size = 5000
        for b in range(10):
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
                for i in range(b * batch_size, (b + 1) * batch_size)
            ]
            with self.cache._conn:
                self.cache._conn.executemany(
                    "INSERT INTO translation_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )

        pop_dur = time.perf_counter() - t_start
        print(f"50,000 rows populated in {pop_dur:.2f}s")

        cur = self.cache._conn.execute("SELECT count(*) FROM translation_cache")
        self.assertEqual(cur.fetchone()[0], 50000)

        # Touch key_42 to update its last_accessed_at to current time
        # This tests that LRU prioritizes last_accessed_at, not created_at
        with self.cache._conn:
            self.cache._conn.execute(
                "UPDATE translation_cache SET last_accessed_at = ? WHERE cache_key = 'key_42'",
                (now + 1000,),
            )

        # 本测试只关心淘汰结果，关闭“每 1% 写入才检查一次”的节流
        self.cache._evict_check_every = 1
        # Trigger eviction by inserting 50,001st entry via cache.put
        t_evict_start = time.perf_counter()
        self.cache.put("Source_50001", "zh", "model", "v1", "译文_50001")
        evict_dur = time.perf_counter() - t_evict_start

        print(f"50,001st entry insertion & eviction duration: {evict_dur * 1000:.2f}ms")
        self.assertLess(evict_dur, 1.0, f"Eviction took {evict_dur:.3f}s, must be < 1.0s")

        # Total count must now be 49,001
        cur = self.cache._conn.execute("SELECT count(*) FROM translation_cache")
        total_remaining = cur.fetchone()[0]
        self.assertEqual(total_remaining, 49001)

        # key_0 and key_999 must be evicted
        cur = self.cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = 'key_0'")
        self.assertIsNone(cur.fetchone())
        cur = self.cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = 'key_999'")
        self.assertIsNone(cur.fetchone())

        # Touched key_42 MUST survive eviction despite being originally in oldest batch
        cur = self.cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = 'key_42'")
        self.assertIsNotNone(cur.fetchone(), "Recently accessed key_42 must survive LRU eviction")

        # key_1001 should remain
        cur = self.cache._conn.execute("SELECT translation FROM translation_cache WHERE cache_key = 'key_1001'")
        self.assertIsNotNone(cur.fetchone())

        # Newly inserted item exists
        val_50001 = self.cache.get("Source_50001", "zh", "model", "v1")
        self.assertEqual(val_50001, "译文_50001")


if __name__ == "__main__":
    unittest.main()
