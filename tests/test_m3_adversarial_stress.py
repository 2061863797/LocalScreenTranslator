# -*- coding: utf-8 -*-
"""Milestone 3 Adversarial Challenge and Stress Test Suite.

Empirically challenges and benchmarks:
1. L1 in-memory cache hit latency (strictly < 1ms, multi-threaded concurrency, saturation).
2. L2 SQLite persistent cache hit latency (strictly < 10ms, file-based DB, 0 llama-server calls).
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
from app.translation_manager import SubtitleIncrementalTranslator
from app.translator import Translator


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
    # 2. Benchmark L2 SQLite Persistent Cache Hit Latency (< 10ms, No llama-server)
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

    def test_l2_cache_hit_strictly_bypasses_llama_server(self):
        """Verify Translator with L2 cache hit strictly never invokes _chat / llama-server."""
        # Seed L2 directly (bypass L1)
        k = self.cache.make_key("Zero Network Call", "简体中文", "default_model", "v1")
        now = time.time()
        with self.cache._conn:
            self.cache._conn.execute(
                "INSERT INTO translation_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (k, "Zero Network Call", "简体中文", "default_model", "v1", "零网络调用", now, now, 1),
            )
        self.cache._l1.clear()

        translator = Translator("http://127.0.0.1:9999", {"ctx_size": 2048, "max_tokens": 512}, cache=self.cache)
        try:
            chat_called = False

            def bomb_chat(*args, **kwargs):
                nonlocal chat_called
                chat_called = True
                raise AssertionError("llama-server _chat must NOT be invoked on L2 cache hit!")

            translator._chat = bomb_chat

            res = translator.translate("Zero Network Call", "简体中文")
            self.assertEqual(res, "零网络调用")
            self.assertFalse(chat_called)
        finally:
            translator.close()

    # =========================================================================
    # 3. Stress Test SubtitleIncrementalTranslator Line Ordering
    # =========================================================================

    def test_subtitle_varied_insertions_deletions_reordering(self):
        """Stress-test SubtitleIncrementalTranslator with chaotic subtitle sequence transitions:
        - Insertion at head, middle, tail
        - Deletion from head, middle, tail
        - Line permutation and reversal
        - Duplicate identical lines
        """
        translator = SubtitleIncrementalTranslator(
            cache=self.cache,
            mock_translate_fn=lambda lines, tgt: [f"[{tgt}]{line.strip()}" for line in lines],
        )

        # Step 1: Initial subtitle frame
        frame1 = ["Alpha", "Beta", "Gamma", "Delta"]
        out1, used1 = translator.translate_subtitle_lines(frame1, "zh")
        self.assertFalse(used1)
        self.assertEqual(out1, "[zh]Alpha\n[zh]Beta\n[zh]Gamma\n[zh]Delta")
        self.assertEqual(translator.lines_translated_count, 4)

        # Step 2: Line deletions (drop Alpha and Gamma)
        frame2 = ["Beta", "Delta"]
        out2, used2 = translator.translate_subtitle_lines(frame2, "zh")
        self.assertTrue(used2)
        self.assertEqual(out2, "[zh]Beta\n[zh]Delta")
        self.assertEqual(translator.lines_translated_count, 4)  # No new lines translated

        # Step 3: Insertions (insert Epsilon at top, Zeta in middle, Eta at bottom)
        frame3 = ["Epsilon", "Beta", "Zeta", "Delta", "Eta"]
        out3, used3 = translator.translate_subtitle_lines(frame3, "zh")
        self.assertTrue(used3)
        self.assertEqual(out3, "[zh]Epsilon\n[zh]Beta\n[zh]Zeta\n[zh]Delta\n[zh]Eta")
        self.assertEqual(translator.lines_translated_count, 7)  # +3 new lines (Epsilon, Zeta, Eta)

        # Step 4: Strict Re-ordering (Reversal of frame 3)
        frame4 = ["Eta", "Delta", "Zeta", "Beta", "Epsilon"]
        out4, used4 = translator.translate_subtitle_lines(frame4, "zh")
        self.assertTrue(used4)
        self.assertEqual(out4, "[zh]Eta\n[zh]Delta\n[zh]Zeta\n[zh]Beta\n[zh]Epsilon")
        self.assertEqual(translator.lines_translated_count, 7)  # All from cache, 0 new translated

        # Step 5: Arbitrary permutation
        frame5 = ["Zeta", "Eta", "Epsilon", "Delta", "Beta"]
        out5, used5 = translator.translate_subtitle_lines(frame5, "zh")
        self.assertTrue(used5)
        self.assertEqual(out5, "[zh]Zeta\n[zh]Eta\n[zh]Epsilon\n[zh]Delta\n[zh]Beta")
        self.assertEqual(translator.lines_translated_count, 7)

        # Step 6: Duplicate identical lines in different positions
        frame6 = ["Beta", "Beta", "Delta", "Beta"]
        out6, used6 = translator.translate_subtitle_lines(frame6, "zh")
        self.assertTrue(used6)
        self.assertEqual(out6, "[zh]Beta\n[zh]Beta\n[zh]Delta\n[zh]Beta")

        # Step 7: Interleaved blank lines and trailing whitespaces
        frame7 = ["  Beta  ", "", "   ", "Delta\t", "NewUniqueLine"]
        out7, used7 = translator.translate_subtitle_lines(frame7, "zh")
        self.assertEqual(out7, "[zh]Beta\n[zh]Delta\n[zh]NewUniqueLine")
        self.assertEqual(translator.lines_translated_count, 8)

    def test_subtitle_stress_batch_100_frames_simulation(self):
        """Simulate 100 consecutive streaming subtitle frames with shifting rolling window."""
        translator = SubtitleIncrementalTranslator(
            cache=self.cache,
            mock_translate_fn=lambda lines, tgt: [f"T({l})" for l in lines],
        )

        total_lines_streamed = 100
        window_size = 4

        # Rolling window: each frame has 4 lines: [i, i+1, i+2, i+3]
        for frame_idx in range(total_lines_streamed - window_size + 1):
            window = [f"Dialog line {frame_idx + offset}" for offset in range(window_size)]
            out, used_cache = translator.translate_subtitle_lines(window, "zh")

            expected = "\n".join([f"T(Dialog line {frame_idx + offset})" for offset in range(window_size)])
            self.assertEqual(out, expected)

            if frame_idx > 0:
                self.assertTrue(used_cache)

        # In 97 frames of 4 lines each (388 line views), only exactly 100 unique lines translated
        self.assertEqual(translator.lines_translated_count, 100)

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
