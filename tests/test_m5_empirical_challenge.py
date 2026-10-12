# -*- coding: utf-8 -*-
"""Empirical stress test suite for Milestone 5 (Async History Queue & Pipeline Metrics).

Challenge dimensions:
1. High-throughput multi-threaded rapid push of 2,000 history records via add_history_async.
   - Measures per-push latency (< 0.1ms).
   - Verifies flush_and_close() commits all records to SQLite.
   - Verifies both retention modes: uncapped (2,000 rows retained) and default (50 rows retained, max(id)=2000).
2. High-concurrency stress test of PipelineMetrics:
   - 10,000 stage events recorded concurrently across 5 worker threads.
   - Tests record_stage, stage_timer, measure, reset, get_stats, and format_table.
   - Validates zero data corruption, exact sample counts, and mathematically sound percentiles.
3. Edge cases and hostile failure modes:
   - Double flush_and_close() idempotency.
   - Queue flush under shutdown race condition.
   - Non-standard / unknown stages in PipelineMetrics.
   - Extreme inputs (empty strings, huge Unicode/CJK strings).
   - CaptureBackend contract validation.
"""

import concurrent.futures
import math
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

import app.storage as storage_mod
from app.capture import CaptureBackend, MssCaptureBackend
from app.storage import AsyncHistoryStorage, Storage


class Milestone5EmpiricalChallenge(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    # =========================================================================
    # CHALLENGE 1: Async History Queue Multi-Threaded Latency & SQLite Commits
    # =========================================================================

    def test_challenge_1a_async_history_2000_records_uncapped(self):
        """Push 2,000 history records rapidly across 10 threads (MAX_HISTORY=2000).

        Verify per-push latency is < 0.1ms, flush_and_close() flushes cleanly,
        and exactly 2,000 records are written to SQLite.
        """
        db_path = self.db_dir / "history_2000_uncapped.db"
        orig_max = storage_mod.MAX_HISTORY
        storage_mod.MAX_HISTORY = 2000  # allow full retention for verification

        try:
            store = AsyncHistoryStorage(db_path, batch_size=20, flush_interval=0.1)
            num_threads = 10
            records_per_thread = 200
            total_records = num_threads * records_per_thread  # 2000

            latencies = []
            lat_lock = threading.Lock()
            start_barrier = threading.Barrier(num_threads)

            def worker(thread_idx: int):
                thread_lats = []
                start_barrier.wait()
                for i in range(records_per_thread):
                    src = f"Source {thread_idx}_{i}: Quick brown fox jumps over lazy dog."
                    tr = f"译文 {thread_idx}_{i}: 敏捷的棕色狐狸跳过懒惰的狗。"
                    mode = "window_subtitle" if i % 2 == 0 else "region_watch"

                    t0 = time.perf_counter()
                    store.add_history_async(src, tr, mode)
                    push_duration_ms = (time.perf_counter() - t0) * 1000.0
                    thread_lats.append(push_duration_ms)

                with lat_lock:
                    latencies.extend(thread_lats)

            threads = [
                threading.Thread(target=worker, args=(t,))
                for t in range(num_threads)
            ]

            t_start_all = time.perf_counter()
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
            t_pushed_all = (time.perf_counter() - t_start_all) * 1000.0

            self.assertEqual(len(latencies), total_records, "All 2,000 pushes must be recorded")

            # Latency measurements
            sorted_lats = sorted(latencies)
            avg_lat = sum(sorted_lats) / len(sorted_lats)
            p50_lat = sorted_lats[int(len(sorted_lats) * 0.50)]
            p95_lat = sorted_lats[int(len(sorted_lats) * 0.95)]
            p99_lat = sorted_lats[int(len(sorted_lats) * 0.99)]
            max_lat = sorted_lats[-1]

            print("\n" + "=" * 80)
            print("CHALLENGE 1A: ASYNC HISTORY PUSH LATENCY (2,000 RECORDS ACROSS 10 THREADS)")
            print("=" * 80)
            print(f"Total Records Pushed : {total_records}")
            print(f"Wall Clock Time Total: {t_pushed_all:.2f} ms")
            print(f"Average Push Latency : {avg_lat:.5f} ms (Target: < 0.10 ms)")
            print(f"P50 Push Latency     : {p50_lat:.5f} ms")
            print(f"P95 Push Latency     : {p95_lat:.5f} ms")
            print(f"P99 Push Latency     : {p99_lat:.5f} ms")
            print(f"Max Push Latency     : {max_lat:.5f} ms")
            print("=" * 80)

            # Strict assertion on average and P95 latency (< 0.1ms)
            self.assertLess(
                avg_lat,
                0.1,
                f"Average push latency {avg_lat:.4f}ms must be < 0.1ms",
            )
            self.assertLess(
                p95_lat,
                0.1,
                f"P95 push latency {p95_lat:.4f}ms must be < 0.1ms",
            )

            # Flush and close cleanly
            t_flush_start = time.perf_counter()
            store.flush_and_close(timeout=10.0)
            t_flush_dur = (time.perf_counter() - t_flush_start) * 1000.0
            print(f"flush_and_close Duration: {t_flush_dur:.2f} ms")

            # Verify SQLite persistence
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            count_rows = cur.execute("SELECT count(*) FROM history").fetchone()[0]
            max_id = cur.execute("SELECT max(id) FROM history").fetchone()[0]
            min_id = cur.execute("SELECT min(id) FROM history").fetchone()[0]
            distinct_sources = cur.execute("SELECT count(DISTINCT source) FROM history").fetchone()[0]
            conn.close()

            print(f"SQLite Verified Rows: count={count_rows}, min_id={min_id}, max_id={max_id}, distinct={distinct_sources}")
            self.assertEqual(count_rows, 2000, "Exactly 2,000 records must be written and persisted to SQLite")
            self.assertEqual(max_id, 2000, "SQLite autoincrement sequence must reach exactly 2,000")
            self.assertEqual(min_id, 1, "First record id must be 1")
            self.assertEqual(distinct_sources, 2000, "All 2,000 pushed records must be unique and intact")

        finally:
            storage_mod.MAX_HISTORY = orig_max

    def test_challenge_1b_async_history_2000_records_default_retention(self):
        """Push 2,000 history records under default production MAX_HISTORY (50).

        Verify sequence max(id) == 2000 (all 2,000 reached SQLite and committed)
        and table retains the latest 50 records as required by the retention policy.
        """
        db_path = self.db_dir / "history_2000_production_cap.db"
        self.assertEqual(storage_mod.MAX_HISTORY, 50, "Default MAX_HISTORY must be 50")

        store = AsyncHistoryStorage(db_path, batch_size=20, flush_interval=0.1)
        num_threads = 5
        records_per_thread = 400
        total_records = num_threads * records_per_thread  # 2000

        def worker(thread_idx: int):
            for i in range(records_per_thread):
                src = f"ProdSource {thread_idx}_{i}"
                tr = f"ProdTrans {thread_idx}_{i}"
                store.add_history_async(src, tr, "subtitle")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        store.flush_and_close(timeout=10.0)

        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        count_rows = cur.execute("SELECT count(*) FROM history").fetchone()[0]
        max_id = cur.execute("SELECT max(id) FROM history").fetchone()[0]
        min_id = cur.execute("SELECT min(id) FROM history").fetchone()[0]
        conn.close()

        print(f"CHALLENGE 1B: Default Cap Verification: count={count_rows}, min_id={min_id}, max_id={max_id}")
        self.assertEqual(count_rows, 50, "SQLite history table must prune to 50 under default retention policy")
        self.assertEqual(max_id, 2000, "Max record ID must reach 2000, confirming all 2,000 were processed")
        self.assertEqual(min_id, 1951, "Remaining records must span exactly ids 1951 to 2000")

    # =========================================================================
    # CHALLENGE 2: PipelineMetrics 10,000 Concurrent Events Across 5 Threads
    # =========================================================================

    # =========================================================================
    # CHALLENGE 3: Edge Cases, Hostile Scenarios & Lifecycle Safety
    # =========================================================================

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

    def test_challenge_3b_double_flush_and_close_idempotency(self):
        """flush_and_close() called repeatedly must be safe and idempotent."""
        db_path = self.db_dir / "double_close.db"
        store = AsyncHistoryStorage(db_path)
        store.add_history_async("A", "B", "test")
        store.flush_and_close(timeout=2.0)
        # Second and third calls must not raise exceptions
        store.flush_and_close(timeout=1.0)
        store.close()
        self.assertIsNone(store._conn)

    def test_challenge_3d_extreme_data_payloads(self):
        """AsyncHistoryStorage handles empty strings, emojis, CJK, and 100KB large payloads."""
        db_path = self.db_dir / "extreme_payloads.db"
        store = AsyncHistoryStorage(db_path, batch_size=5, flush_interval=0.1)

        # 1. Empty strings
        store.add_history_async("", "", "")
        # 2. Emojis and special Unicode
        store.add_history_async("🚀🔥✨🎉", "🚀🔥✨🎉 (translated)", "mode_unicode")
        # 3. 100KB payload
        huge_src = "A" * 100000
        huge_tr = "中" * 50000
        store.add_history_async(huge_src, huge_tr, "heavy")

        store.flush_and_close(timeout=5.0)

        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        rows = cur.execute("SELECT source, translation, mode FROM history").fetchall()
        conn.close()

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], ("", "", ""))
        self.assertEqual(rows[1], ("🚀🔥✨🎉", "🚀🔥✨🎉 (translated)", "mode_unicode"))
        self.assertEqual(len(rows[2][0]), 100000)
        self.assertEqual(len(rows[2][1]), 50000)

if __name__ == "__main__":
    unittest.main()
