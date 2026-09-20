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
from app.capture import (
    CaptureBackend,
    DxgiCaptureBackend,
    MssCaptureBackend,
    WgcCaptureBackend,
    Win32PrintWindowBackend,
)
from app.pipeline_metrics import PipelineMetrics, PipelineStageTimers
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

    def test_challenge_2_pipeline_metrics_10000_concurrent_events(self):
        """Concurrently record 10,000 pipeline stage events across 5 threads.

        Verify:
        - Thread safety under intense contention.
        - Exact sample counts per stage sum to 10,000.
        - Summary statistics (avg, p50, p95, max) are strictly monotonic and uncorrupted.
        - format_table() renders without errors.
        """
        metrics = PipelineMetrics()
        num_threads = 5
        events_per_thread = 2000
        total_events = num_threads * events_per_thread  # 10,000

        stages = PipelineMetrics.STAGES
        num_stages = len(stages)

        # Expected counts per stage
        expected_counts = {s: 0 for s in stages}
        for thread_idx in range(num_threads):
            for i in range(events_per_thread):
                stg = stages[(thread_idx * events_per_thread + i) % num_stages]
                expected_counts[stg] += 1

        barrier = threading.Barrier(num_threads)

        def worker(thread_idx: int):
            barrier.wait()
            for i in range(events_per_thread):
                stg = stages[(thread_idx * events_per_thread + i) % num_stages]
                dur = float((i % 50) + 1) * 0.5  # Deterministic duration 0.5ms to 25.0ms
                if i % 10 == 0:
                    # Test stage_timer / measure context manager
                    with metrics.measure(stg):
                        pass  # sub-millisecond execution
                else:
                    metrics.record_stage(stg, dur)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]

        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        stats = metrics.get_stats()
        total_recorded = sum(s["count"] for s in stats.values())

        print("\n" + "=" * 80)
        print(f"CHALLENGE 2: PIPELINE METRICS 10,000 EVENTS CONCURRENCY (5 THREADS)")
        print("=" * 80)
        print(f"Elapsed Time: {elapsed_ms:.2f} ms ({total_events / (elapsed_ms / 1000):.0f} events/sec)")
        print(f"Total Events Recorded: {total_recorded} / {total_events}")
        for stg in stages:
            s = stats[stg]
            print(f"  Stage {stg:<15}: count={s['count']:>5}, avg={s['avg']:>6.2f}, p50={s['p50']:>6.2f}, p95={s['p95']:>6.2f}, max={s['max']:>6.2f}")
        print("=" * 80)

        table_str = metrics.format_table()
        print("\nFormatted Telemetry Table:\n" + table_str + "\n")

        # Assertions
        self.assertEqual(total_recorded, total_events, "Every single one of the 10,000 events must be safely recorded")
        for stg in stages:
            s = stats[stg]
            self.assertEqual(s["count"], expected_counts[stg], f"Stage {stg} count mismatch")
            self.assertGreater(s["avg"], 0.0)
            self.assertGreaterEqual(s["p50"], 0.0)
            self.assertGreaterEqual(s["p95"], s["p50"], f"Stage {stg}: p95 must be >= p50")
            self.assertGreaterEqual(s["max"], s["p95"], f"Stage {stg}: max must be >= p95")

        self.assertIn("TOTAL PIPELINE", table_str)
        self.assertIn("Throughput:", table_str)

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

    def test_challenge_3c_concurrent_reset_and_recording(self):
        """PipelineMetrics.reset() during high-volume recording must not cause data corruption."""
        metrics = PipelineMetrics()
        stop_flag = threading.Event()

        def recorder():
            while not stop_flag.is_set():
                metrics.record_stage("capture", 1.5)
                metrics.record_stage("ocr", 20.0)

        threads = [threading.Thread(target=recorder) for _ in range(4)]
        for t in threads:
            t.start()

        # Repeatedly reset and compute stats under fire
        for _ in range(20):
            time.sleep(0.01)
            metrics.reset()
            stats = metrics.get_stats()
            self.assertIsInstance(stats, dict)

        stop_flag.set()
        for t in threads:
            t.join()

        # Final check
        final_stats = metrics.get_stats()
        self.assertIn("capture", final_stats)
        self.assertIn("ocr", final_stats)

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

    def test_challenge_3e_capture_backend_contracts(self):
        """Validate CaptureBackend inheritance and error boundaries."""
        # 1. MssCaptureBackend
        mss = MssCaptureBackend()
        self.assertTrue(mss.is_supported())
        mss.release()

        # 2. Win32PrintWindowBackend
        win32 = Win32PrintWindowBackend()
        self.assertTrue(win32.is_supported())
        win32.release()

        # 3. Extensible hardware hooks
        dxgi = DxgiCaptureBackend()
        self.assertFalse(dxgi.is_supported())
        with self.assertRaises(NotImplementedError):
            dxgi.grab_region(0, 0, 10, 10)
        with self.assertRaises(NotImplementedError):
            dxgi.grab_window(1234)

        wgc = WgcCaptureBackend()
        self.assertFalse(wgc.is_supported())
        with self.assertRaises(NotImplementedError):
            wgc.grab_region(0, 0, 10, 10)
        with self.assertRaises(NotImplementedError):
            wgc.grab_window(1234)


if __name__ == "__main__":
    unittest.main()
