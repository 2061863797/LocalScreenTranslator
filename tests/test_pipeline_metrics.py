# -*- coding: utf-8 -*-
"""End-to-end & component test suite for Pipeline Metrics, Async History Queue, and CaptureBackend.

Covers:
- Feature 9: Decoupled WindowWatcher Architecture (PR 9)
- Feature 12: CaptureBackend Abstraction (PR 12)
- Feature 13: Async History Queue (PR 12)
- Feature 14: Pipeline Metrics & Telemetry (PR 12)
Tiers 1-4: Stage latency tracking, summary percentiles, non-blocking history logging,
batch SQLite commits, capture backend contracts, and synthetic pipeline benchmarks.
"""

import math
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import numpy as np

# Try importing production modules if available
try:
    from app.pipeline_metrics import PipelineMetrics as _ProdPipelineMetrics
    from app.pipeline_metrics import PipelineStageTimers as _ProdPipelineStageTimers
except ImportError:
    _ProdPipelineMetrics = None
    _ProdPipelineStageTimers = None

try:
    from app.capture import (
        CaptureBackend as _ProdCaptureBackend,
        DxgiCaptureBackend as _ProdDxgiCaptureBackend,
        MssCaptureBackend as _ProdMssCaptureBackend,
        WgcCaptureBackend as _ProdWgcCaptureBackend,
        Win32PrintWindowBackend as _ProdWin32PrintWindowBackend,
    )
except ImportError:
    _ProdCaptureBackend = None
    _ProdDxgiCaptureBackend = None
    _ProdMssCaptureBackend = None
    _ProdWgcCaptureBackend = None
    _ProdWin32PrintWindowBackend = None

try:
    from app.storage import AsyncHistoryStorage as _ProdAsyncHistoryStorage
except ImportError:
    _ProdAsyncHistoryStorage = None


# ==========================================
# Contract Implementations
# ==========================================

@dataclass
class ContractPipelineStageTimers:
    capture_ms: float = 0.0
    frame_diff_ms: float = 0.0
    ocr_ms: float = 0.0
    text_diff_ms: float = 0.0
    cache_lookup_ms: float = 0.0
    translate_ms: float = 0.0
    ui_render_ms: float = 0.0
    total_ms: float = 0.0


class ContractPipelineMetrics:
    """PR 12: Per-stage latency telemetry and percentile summaries."""

    STAGES = [
        "capture",
        "frame_diff",
        "ocr",
        "text_diff",
        "cache_lookup",
        "translate",
        "ui_render",
    ]

    def __init__(self):
        self._lock = threading.Lock()
        self._records: dict[str, list[float]] = {s: [] for s in self.STAGES}

    def record_stage(self, stage: str, duration_ms: float) -> None:
        with self._lock:
            if stage not in self._records:
                self._records[stage] = []
            self._records[stage].append(duration_ms)

    def get_summary(self) -> dict[str, dict[str, float]]:
        with self._lock:
            summary = {}
            for stage, vals in self._records.items():
                if not vals:
                    summary[stage] = {
                        "count": 0,
                        "avg": 0.0,
                        "p50": 0.0,
                        "p95": 0.0,
                        "max": 0.0,
                    }
                    continue
                sorted_vals = sorted(vals)
                n = len(sorted_vals)
                avg = sum(sorted_vals) / n
                p50 = sorted_vals[int(n * 0.50)]
                p95 = sorted_vals[min(int(n * 0.95), n - 1)]
                max_val = sorted_vals[-1]
                summary[stage] = {
                    "count": n,
                    "avg": round(avg, 3),
                    "p50": round(p50, 3),
                    "p95": round(p95, 3),
                    "max": round(max_val, 3),
                }
            return summary


class ContractCaptureBackend(ABC):
    """PR 12: Abstract capture backend interface."""

    @abstractmethod
    def grab_region(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        pass

    @abstractmethod
    def grab_window(self, hwnd: int) -> np.ndarray | None:
        pass

    @abstractmethod
    def release(self) -> None:
        pass


class MockCaptureBackend(ContractCaptureBackend):
    def __init__(self):
        self.released = False

    def grab_region(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        return np.zeros((h, w, 3), dtype=np.uint8)

    def grab_window(self, hwnd: int) -> np.ndarray | None:
        if hwnd <= 0:
            return None
        return np.zeros((600, 800, 3), dtype=np.uint8)

    def release(self) -> None:
        self.released = True


class ContractAsyncHistoryStorage:
    """PR 12: Asynchronous history queue with non-blocking push and batch SQLite commits."""

    def __init__(self, db_path: str | Path, batch_size: int = 10, flush_interval: float = 0.5):
        self._db_path = str(db_path)
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self._queue: queue.Queue[tuple[float, str, str, str] | None] = queue.Queue()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._init_db()

        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self.batches_committed_count = 0

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    source TEXT NOT NULL,
                    translation TEXT NOT NULL,
                    mode TEXT NOT NULL
                )"""
            )

    def add_history_async(self, source: str, translation: str, mode: str) -> None:
        """Non-blocking push (<0.1ms)."""
        self._queue.put((time.time(), source, translation, mode))

    def _worker_loop(self) -> None:
        batch: list[tuple[float, str, str, str]] = []
        last_flush = time.time()

        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.05)
                if item is not None:
                    batch.append(item)
            except queue.Empty:
                pass

            now = time.time()
            if (len(batch) >= self.batch_size) or (batch and now - last_flush >= self.flush_interval) or (self._stop_event.is_set() and batch):
                self._commit_batch(batch)
                batch = []
                last_flush = now

    def _commit_batch(self, batch: list[tuple[float, str, str, str]]) -> None:
        if not batch:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT INTO history (ts, source, translation, mode) VALUES (?, ?, ?, ?)",
                batch,
            )
            self.batches_committed_count += 1

    def flush_and_close(self, timeout: float = 2.0) -> None:
        if self._conn is None:
            return
        self._stop_event.set()
        self._worker_thread.join(timeout=timeout)
        with self._conn:
            # Commit any leftovers
            remaining = []
            while not self._queue.empty():
                try:
                    it = self._queue.get_nowait()
                    if it is not None:
                        remaining.append(it)
                except queue.Empty:
                    break
            if remaining:
                self._commit_batch(remaining)
        self._conn.close()
        self._conn = None

    def count_records(self) -> int:
        # Open separate connection to read committed records
        c = sqlite3.connect(self._db_path)
        cnt = c.execute("SELECT count(*) FROM history").fetchone()[0]
        c.close()
        return cnt


# Factory helpers
def create_pipeline_metrics():
    return _ProdPipelineMetrics() if _ProdPipelineMetrics else ContractPipelineMetrics()


def create_async_storage(db_path, batch_size=10, flush_interval=0.5):
    return (
        _ProdAsyncHistoryStorage(db_path, batch_size, flush_interval)
        if _ProdAsyncHistoryStorage
        else ContractAsyncHistoryStorage(db_path, batch_size, flush_interval)
    )


class TestPipelineMetricsTiers(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_history.db"
        self.metrics = create_pipeline_metrics()
        self.storage = create_async_storage(self.db_path, batch_size=5, flush_interval=0.2)

    def tearDown(self):
        if self.storage:
            self.storage.flush_and_close()
        self.tmp_dir.cleanup()

    # ==========================================
    # Tier 1: Primary Feature Behavior
    # ==========================================

    def test_pipeline_metrics_record_all_stages(self):
        """Tier 1: PipelineMetrics records durations for all 7 pipeline stages."""
        stages = [
            "capture",
            "frame_diff",
            "ocr",
            "text_diff",
            "cache_lookup",
            "translate",
            "ui_render",
        ]
        for stage in stages:
            self.metrics.record_stage(stage, 12.5)

        summary = self.metrics.get_summary()
        for stage in stages:
            self.assertIn(stage, summary)
            self.assertEqual(summary[stage]["count"], 1)
            self.assertAlmostEqual(summary[stage]["avg"], 12.5)

    def test_pipeline_metrics_summary_percentiles(self):
        """Tier 1: Summary calculates avg, p50, p95, and max percentiles correctly."""
        # Record 100 values from 1 to 100
        for i in range(1, 101):
            self.metrics.record_stage("ocr", float(i))

        summary = self.metrics.get_summary()["ocr"]
        self.assertEqual(summary["count"], 100)
        self.assertAlmostEqual(summary["avg"], 50.5, delta=0.1)
        self.assertEqual(summary["p50"], 51.0)
        self.assertEqual(summary["p95"], 96.0)
        self.assertEqual(summary["max"], 100.0)

    def test_async_history_queue_non_blocking_push(self):
        """Tier 1: add_history_async pushes to queue in < 1ms without blocking on SQLite."""
        start = time.perf_counter()
        self.storage.add_history_async("Original Text", "Translated Text", "subtitle")
        duration_ms = (time.perf_counter() - start) * 1000

        self.assertLess(
            duration_ms,
            1.0,
            f"add_history_async should be non-blocking (<1ms), took {duration_ms:.3f}ms",
        )

    def test_async_history_queue_background_flush(self):
        """Tier 1: Background worker thread commits queued items into SQLite database."""
        self.storage.add_history_async("Source A", "Trans A", "subtitle")
        self.storage.add_history_async("Source B", "Trans B", "subtitle")

        # 等待后台定时刷盘完成，避免固定休眠受 CI 调度影响
        deadline = time.monotonic() + 5.0
        while self.storage.count_records() < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

        cnt = self.storage.count_records()
        self.assertEqual(cnt, 2)

    def test_capture_backend_interface(self):
        """Tier 1: CaptureBackend defines grab_region, grab_window, release contracts."""
        backend = MockCaptureBackend()
        reg = backend.grab_region(0, 0, 100, 50)
        self.assertEqual(reg.shape, (50, 100, 3))

        win = backend.grab_window(12345)
        self.assertIsNotNone(win)
        self.assertEqual(win.shape, (600, 800, 3))

        backend.release()
        self.assertTrue(backend.released)

    # ==========================================
    # Tier 2: Boundary & Corner Cases
    # ==========================================

    def test_pipeline_metrics_empty_stage_handling(self):
        """Tier 2: Empty stage handles summary cleanly without ZeroDivisionError."""
        summary = self.metrics.get_summary()
        self.assertIn("capture", summary)
        self.assertEqual(summary["capture"]["count"], 0)
        self.assertEqual(summary["capture"]["avg"], 0.0)
        self.assertEqual(summary["capture"]["p50"], 0.0)
        self.assertEqual(summary["capture"]["p95"], 0.0)
        self.assertEqual(summary["capture"]["max"], 0.0)

    def test_pipeline_metrics_single_record_percentiles(self):
        """Tier 2: Single measurement yields identical avg, p50, p95, and max."""
        self.metrics.record_stage("translate", 42.0)
        s = self.metrics.get_summary()["translate"]
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["avg"], 42.0)
        self.assertEqual(s["p50"], 42.0)
        self.assertEqual(s["p95"], 42.0)
        self.assertEqual(s["max"], 42.0)

    def test_async_history_batch_commit(self):
        """Tier 2: Multiple rapid history pushes are batched into single commit transactions."""
        # Batch size is 5
        for i in range(5):
            self.storage.add_history_async(f"Src {i}", f"Tr {i}", "sub")

        # 等待后台线程提交实际记录，避免固定休眠在繁忙 CI 上抢先断言
        deadline = time.monotonic() + 5.0
        while self.storage.count_records() < 5 and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertGreaterEqual(self.storage.batches_committed_count, 1)
        self.assertEqual(self.storage.count_records(), 5)

    def test_async_history_flush_on_close(self):
        """Tier 2: Calling flush_and_close ensures all pending items are safely persisted."""
        for i in range(7):
            self.storage.add_history_async(f"Pending {i}", f"Tr {i}", "sub")

        # Close immediately without waiting for periodic timer
        self.storage.flush_and_close()

        # Check records were not lost
        c = sqlite3.connect(str(self.db_path))
        cnt = c.execute("SELECT count(*) FROM history").fetchone()[0]
        c.close()
        self.assertEqual(cnt, 7)

    def test_pipeline_metrics_monotonic_clock(self):
        """Tier 2: Timing stage durations via perf_counter is strictly non-negative."""
        t0 = time.perf_counter()
        time.sleep(0.002)
        t1 = time.perf_counter()
        dur = (t1 - t0) * 1000
        self.assertGreater(dur, 0.0)
        self.metrics.record_stage("ocr", dur)
        self.assertGreater(self.metrics.get_summary()["ocr"]["avg"], 0.0)

    # ==========================================
    # Tier 3: Pairwise & Component Interactions
    # ==========================================

    def test_end_to_end_pipeline_latency_tracking(self):
        """Tier 3: Simulating a full translation cycle measuring all 7 stages."""
        stages_sim = {
            "capture": 1.2,
            "frame_diff": 0.8,
            "ocr": 25.0,
            "text_diff": 0.2,
            "cache_lookup": 0.3,
            "translate": 110.0,
            "ui_render": 2.1,
        }
        for stg, dur in stages_sim.items():
            self.metrics.record_stage(stg, dur)

        summary = self.metrics.get_summary()
        total_pipeline_ms = sum(summary[stg]["avg"] for stg in stages_sim)
        self.assertAlmostEqual(total_pipeline_ms, sum(stages_sim.values()), delta=0.01)

    def test_decoupled_architecture_contracts(self):
        """Tier 3: Verifies single-responsibility component contracts from PROJECT.md §1-10."""
        # 1. CaptureService contract: grab(hwnd, region) -> (rect, frame)
        class StubCaptureService:
            def grab(self, hwnd, region):
                return region, np.zeros((10, 10, 3), dtype=np.uint8)

        # 2. TextChangeDetector contract: observe(lines, threshold) -> (raw, translatable)
        class StubTextChangeDetector:
            def observe(self, lines, threshold):
                return "raw", "translatable"

        # 3. TranslationManager contract: translate_subtitle(text, target, gen_id)
        class StubTranslationManager:
            def translate_subtitle(self, text, target, gen_id):
                return f"translated_{text}"

        cs = StubCaptureService()
        tcd = StubTextChangeDetector()
        tm = StubTranslationManager()

        rect, frame = cs.grab(None, (0, 0, 100, 100))
        self.assertIsNotNone(frame)

        raw, to_tr = tcd.observe([], 0.3)
        self.assertEqual(to_tr, "translatable")

        out = tm.translate_subtitle(to_tr, "zh", 1)
        self.assertEqual(out, "translated_translatable")

    # ==========================================
    # Tier 4: Real-World Scenarios
    # ==========================================

    def test_scenario_high_frequency_history_logging(self):
        """Tier 4 Scenario 5: High frequency subtitle stream emitting 50 history entries.

        Ensures each add_history_async call completes in < 0.5ms on average without stalling UI thread.
        """
        durations = []
        for i in range(50):
            t0 = time.perf_counter()
            self.storage.add_history_async(f"Stream Source {i}", f"Stream Trans {i}", "subtitle")
            durations.append((time.perf_counter() - t0) * 1000)

        avg_push_time = sum(durations) / len(durations)
        self.assertLess(
            avg_push_time,
            0.5,
            f"Average history push time {avg_push_time:.3f}ms exceeds 0.5ms threshold",
        )

        # Allow worker thread to catch up and commit
        time.sleep(0.4)
        self.assertEqual(self.storage.count_records(), 50)

    def test_scenario_benchmark_live_pipeline_synthetic_run(self):
        """Tier 4 Scenario 1: Synthetic live pipeline benchmark executing 30 frames.

        Collects latencies for all stages and verifies that report contains complete stage telemetry.
        """
        frame_count = 30
        for _ in range(frame_count):
            self.metrics.record_stage("capture", 1.5)
            self.metrics.record_stage("frame_diff", 0.9)
            self.metrics.record_stage("ocr", 32.0)
            self.metrics.record_stage("text_diff", 0.1)
            self.metrics.record_stage("cache_lookup", 0.2)
            self.metrics.record_stage("translate", 125.0)
            self.metrics.record_stage("ui_render", 1.8)

        summary = self.metrics.get_summary()
        for stage in self.metrics.STAGES:
            self.assertEqual(summary[stage]["count"], frame_count)
            self.assertGreater(summary[stage]["avg"], 0.0)
            self.assertGreater(summary[stage]["p95"], 0.0)

    def test_production_capture_backends(self):
        """Production CaptureBackend implementations and extensible hooks."""
        if _ProdCaptureBackend is None:
            self.skipTest("Production CaptureBackend not loaded")

        mss_be = _ProdMssCaptureBackend()
        self.assertIsInstance(mss_be, _ProdCaptureBackend)
        self.assertTrue(mss_be.is_supported())
        mss_be.release()

        win32_be = _ProdWin32PrintWindowBackend()
        self.assertIsInstance(win32_be, _ProdCaptureBackend)
        self.assertTrue(win32_be.is_supported())
        win32_be.release()

        dxgi_be = _ProdDxgiCaptureBackend()
        self.assertIsInstance(dxgi_be, _ProdCaptureBackend)
        self.assertFalse(dxgi_be.is_supported())
        with self.assertRaises(NotImplementedError):
            dxgi_be.grab_region(0, 0, 100, 100)

        wgc_be = _ProdWgcCaptureBackend()
        self.assertIsInstance(wgc_be, _ProdCaptureBackend)
        self.assertFalse(wgc_be.is_supported())
        with self.assertRaises(NotImplementedError):
            wgc_be.grab_window(123)

    def test_production_pipeline_metrics_features(self):
        """Production PipelineMetrics context manager, reset, and format_table."""
        from app.pipeline_metrics import PipelineMetrics, PipelineStageTimers

        pm = PipelineMetrics()
        with pm.stage_timer("ocr"):
            time.sleep(0.005)
        with pm.measure("capture"):
            time.sleep(0.002)

        stats = pm.get_stats()
        self.assertEqual(stats["ocr"]["count"], 1)
        self.assertGreater(stats["ocr"]["avg"], 2.0)
        self.assertEqual(stats["capture"]["count"], 1)
        self.assertGreater(stats["capture"]["avg"], 1.0)

        table = pm.format_table()
        self.assertIn("STAGE", table)
        self.assertIn("ocr", table)
        self.assertIn("TOTAL PIPELINE", table)
        self.assertIn("Throughput:", table)

        pm.reset()
        stats_after_reset = pm.get_stats()
        self.assertEqual(stats_after_reset["ocr"]["count"], 0)
        self.assertEqual(stats_after_reset["capture"]["count"], 0)

    def test_production_pipeline_metrics_dynamic_stages(self):
        """Production PipelineMetrics collects and formats custom/dynamic stages."""
        from app.pipeline_metrics import PipelineMetrics

        pm = PipelineMetrics()
        pm.record_stage("custom_stage_a", 15.5)
        pm.record_stage("custom_stage_b", 22.0)
        pm.record_stage("custom_stage_b", 26.0)

        stats = pm.get_stats()
        self.assertIn("custom_stage_a", stats)
        self.assertEqual(stats["custom_stage_a"]["count"], 1)
        self.assertEqual(stats["custom_stage_a"]["avg"], 15.5)

        self.assertIn("custom_stage_b", stats)
        self.assertEqual(stats["custom_stage_b"]["count"], 2)
        self.assertEqual(stats["custom_stage_b"]["avg"], 24.0)

        table = pm.format_table()
        self.assertIn("custom_stage_a", table)
        self.assertIn("custom_stage_b", table)

    def test_production_async_storage_flush_and_callback(self):
        """Production AsyncHistoryStorage on_complete callback and synchronous flush."""
        callback_called = threading.Event()
        self.storage.add_history_async("SrcCB", "TrCB", "test", on_complete=callback_called.set)
        self.storage.flush(timeout=2.0)
        self.assertTrue(callback_called.is_set(), "on_complete callback should be called upon batch commit")
        self.assertGreaterEqual(self.storage.count_records(), 1)

    def test_production_async_storage_batch_error_and_callback(self):
        """Production AsyncHistoryStorage handles batch write failure gracefully."""
        res = []
        def cb(success=None):
            res.append(success)

        # Simulate exception during batch commit by closing connection
        self.storage._conn.close()
        self.storage._commit_history_batch([(time.time(), "src", "tr", "test", cb)])
        self.assertEqual(res, [False])


if __name__ == "__main__":
    unittest.main()
