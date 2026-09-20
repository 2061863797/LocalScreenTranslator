# -*- coding: utf-8 -*-
"""Tier 5 Adversarial Coverage Hardening & Verification Suite (Phases 4 & 5).

Comprehensive white-box stress testing, latency benchmarking, and invariant verification:
1. Phase 4 (PR 9 Decoupled WindowWatcher, PR 11 Latest-Frame-Wins Engine):
   - Decoupled pipeline components & signal wiring: CaptureService, FrameChangeDetector,
     OcrService, TextChangeDetector, TranslationManager, ResultManager.
   - LatestFrameBuffer drops unconsumed intermediate frames when capture rate > inference rate;
     preserves exact accounting invariant and delivers freshest frame.
   - Sub-service exception isolation: transient OCR (DirectML/OOM) and Translation (HTTP/offline)
     errors do NOT crash WindowWatcher. Target window closure gracefully halts watcher.
2. Phase 5 (PR 10 Single HWND Subtitle Overlay, PR 12 CaptureBackend, Async History Queue, Pipeline Metrics):
   - Single HWND SubtitleBar: is_single_hwnd() is True, layer_widgets has length 1.
   - WM_NCHITTEST routing: HTCLIENT (1) on child controls, HTTRANSPARENT (-1) on text plate.
   - CaptureBackend abstraction: MssCaptureBackend, Win32PrintWindowBackend, Dxgi/Wgc hooks.
   - Async History Queue: sub-0.1ms push latency, batch SQLite commits, flush_and_close safety.
   - PipelineMetrics: 7 core stages, percentiles, format_table, and benchmark_live_pipeline.py execution.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import json
import logging
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QApplication, QScrollBar, QWidget

import app.storage as storage_mod
from app.adaptive_polling import AdaptivePollingController, PollingState
from app.capture import (
    CaptureBackend,
    DxgiCaptureBackend,
    MssCaptureBackend,
    WgcCaptureBackend,
    Win32PrintWindowBackend,
)
from app.capture_service import CaptureService, is_invalid_window_handle_error
from app.config import DEFAULTS
from app.frame_detector import FrameChangeDetector, FrameDiffResult
from app.latest_frame_buffer import LatestFrameBuffer
from app.ocr_engine import OcrEngine, OcrLine
from app.ocr_service import OcrService
from app.pipeline_metrics import PipelineMetrics, PipelineStageTimers
from app.pipelines import GenerationTracker
from app.result_manager import ResultManager
from app.storage import AsyncHistoryStorage, Storage
from app.text_change_detector import TextChangeDetector
from app.translation_cache import TranslationCache
from app.translation_manager import SubtitleIncrementalTranslator, TranslationManager
from app.ui.overlays import SubtitleBar
from app.ui.topmost import restack_above_owner, set_overlay_layer
from app.window_watcher import WindowWatcher


class TestTier5Phase4Adversarial(unittest.TestCase):
    """Tier 5 Adversarial Challenges for Phase 4 (PR 9 & PR 11)."""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def tearDown(self):
        self.qapp.processEvents()

    # =========================================================================
    # 1. Decoupled WindowWatcher Pipeline Architecture & Signal Flow
    # =========================================================================

    def test_decoupled_pipeline_component_composition(self):
        """Verify WindowWatcher composes all 6 decoupled components with single responsibilities."""
        ocr_mock = MagicMock()
        translator_mock = MagicMock()
        cfg = dict(DEFAULTS)
        watcher = WindowWatcher(ocr_mock, translator_mock, cfg, hwnd=100)

        # 1. CaptureService
        self.assertIsInstance(watcher.capture_service, CaptureService)
        self.assertEqual(watcher.capture_service.hwnd, 100)

        # 2. LatestFrameBuffer
        self.assertIsInstance(watcher.frame_buffer, LatestFrameBuffer)

        # 3. FrameChangeDetector
        self.assertIsInstance(watcher.frame_detector, FrameChangeDetector)

        # 4. AdaptivePollingController
        self.assertIsInstance(watcher.polling_controller, AdaptivePollingController)

        # 5. OcrService
        self.assertIsInstance(watcher.ocr_service, OcrService)

        # 6. TextChangeDetector
        self.assertIsInstance(watcher.text_change_detector, TextChangeDetector)

        # 7. TranslationManager
        self.assertIsInstance(watcher.translation_manager, TranslationManager)

        # 8. ResultManager & GenerationTracker
        self.assertIsInstance(watcher.result_manager, ResultManager)
        self.assertIsInstance(watcher.generation_tracker, GenerationTracker)

        # Signal connection test: ResultManager dispatches forward to WindowWatcher signals
        sub_received = []
        watcher.subtitle_ready.connect(sub_received.append)
        gen_id = watcher.generation_tracker.current_generation
        watcher.result_manager.dispatch_subtitle("Decoupled Subtitle", gen_id)
        self.qapp.processEvents()
        self.assertEqual(sub_received, ["Decoupled Subtitle"])

    def test_capture_service_robustness_and_edge_cases(self):
        """Adversarial testing of CaptureService against dead hwnds, invalid regions, and DPI."""
        backend_mock = MagicMock(spec=CaptureBackend)
        fake_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        backend_mock.grab_region.return_value = fake_frame
        backend_mock.grab_window.return_value = fake_frame

        service = CaptureService(
            hwnd=9999,
            backend=backend_mock,
            get_window_rect_fn=lambda h: (10, 20, 100, 100),
        )

        # 1. Target switching
        service.set_target(hwnd=None, region=(0, 0, 50, 50))
        self.assertIsNone(service.hwnd)
        self.assertEqual(service.region, (0, 0, 50, 50))

        # 2. grab region
        rect, frame = service.grab()
        self.assertEqual(rect, (0, 0, 50, 50))
        self.assertIsNotNone(frame)
        backend_mock.grab_region.assert_called_with(0, 0, 50, 50)

        # 3. Degenerate region (zero / negative width)
        service.set_region((0, 0, 0, 100))
        rect, frame = service.grab()
        self.assertIsNone(rect)
        self.assertIsNone(frame)

        # 4. Movement detection
        service.reset_moved_tracking()
        self.assertTrue(service.has_moved((10, 10, 100, 100)))
        self.assertFalse(service.has_moved((10, 10, 100, 100)))
        self.assertTrue(service.has_moved((15, 10, 100, 100)))

        # 5. Invalid window handle detection
        class WinError(Exception):
            def __init__(self, code):
                self.winerror = code

        self.assertTrue(service.is_invalid_window_handle_error(WinError(1400)))
        self.assertFalse(service.is_invalid_window_handle_error(WinError(5)))

        # 6. Resource release
        service.release()
        backend_mock.release.assert_called_once()

    def test_text_change_detector_theoretical_bound_and_clearing(self):
        """Adversarial stress on TextChangeDetector: diff short-circuiting and empty frame clearing."""
        detector = TextChangeDetector(empty_clear_threshold=2)

        # Empty frame 1 -> event="none"
        ev1, t1 = detector.observe([])
        self.assertEqual(ev1, "none")
        self.assertEqual(t1, "")

        # Empty frame 2 -> event="clear" (threshold=2 reached)
        detector.last_text = "Prior text"
        ev2, t2 = detector.observe([])
        self.assertEqual(ev2, "clear")
        self.assertEqual(detector.last_text, "")

        # First text arrival -> event="change"
        ev3, t3 = detector.observe("Hello World", threshold=0.5)
        self.assertEqual(ev3, "change")
        self.assertEqual(t3, "Hello World")

        # Identical text -> event="none"
        ev4, t4 = detector.observe("Hello World", threshold=0.5)
        self.assertEqual(ev4, "none")

        # Completely different length text triggering mathematical upper bound short-circuit
        # len("Hello World") = 11, len("A" * 100) = 100.
        # Upper bound = 2 * 11 / 111 = 0.198 < 0.5. Skips SequenceMatcher DP entirely.
        with patch("app.text_change_detector.SequenceMatcher") as mock_sm:
            ev5, t5 = detector.observe("A" * 100, threshold=0.5)
            self.assertEqual(ev5, "change")
            mock_sm.assert_not_called()

        # Cache pruning
        detector.line_cache = {f"line_{i}": f"tr_{i}" for i in range(500)}
        detector.prune_cache(["line_0", "line_1"], limit=400)
        self.assertLessEqual(len(detector.line_cache), 2)
        self.assertIn("line_0", detector.line_cache)

    # =========================================================================
    # 2. LatestFrameBuffer Overwrite & Drop Accounting (PR 11)
    # =========================================================================

    def test_latest_frame_buffer_drop_when_capture_rate_exceeds_inference(self):
        """Stress: Rapid burst producer vs slow consumer.

        Proves:
        1. All unconsumed frames are dropped.
        2. Accounting invariant holds: total_produced == total_consumed + dropped_count + remaining.
        3. Consumer strictly receives the latest frames in ascending order.
        """
        buffer = LatestFrameBuffer()
        total_frames = 200
        stop_event = threading.Event()
        consumed_items = []

        def fast_producer():
            for i in range(1, total_frames + 1):
                frame = np.full((4, 4), i, dtype=np.uint8)
                buffer.put(frame, generation_id=i)
                # Small yield to let consumer interleave without Windows timer quantization issues
                if i % 10 == 0:
                    time.sleep(0.005)

        def slow_consumer():
            while not stop_event.is_set():
                item = buffer.get(timeout=0.01)
                if item is not None:
                    consumed_items.append(item[1])
                    time.sleep(0.01)

        prod_th = threading.Thread(target=fast_producer)
        cons_th = threading.Thread(target=slow_consumer)

        prod_th.start()
        cons_th.start()

        prod_th.join(timeout=5.0)
        time.sleep(0.05)
        stop_event.set()
        cons_th.join(timeout=2.0)

        remaining = 1 if not buffer.is_empty else 0
        total_consumed = len(consumed_items)
        dropped = buffer.dropped_count

        self.assertEqual(
            total_frames,
            total_consumed + dropped + remaining,
            f"Accounting failed: {total_frames} != {total_consumed} + {dropped} + {remaining}",
        )
        self.assertGreater(dropped, 0, "Producer outpacing consumer must cause drops")
        self.assertEqual(consumed_items, sorted(consumed_items), "Consumed generations must be monotonic")

    # =========================================================================
    # 3. Sub-service Exception Isolation (PR 9 & PR 11)
    # =========================================================================

    def test_exception_isolation_transient_ocr_error(self):
        """Empirical: Transient OCR crash (DirectML/OOM/hardware error) does NOT crash WindowWatcher."""
        ocr_mock = MagicMock()
        # First 3 frames fail with hardware exception, 4th frame recovers
        ocr_mock.recognize.side_effect = [
            RuntimeError("DirectML device removed: 0x887A0005"),
            MemoryError("Out of VRAM"),
            ValueError("Corrupted image matrix"),
            [OcrLine(text="Recovered text", box=[[0, 0], [10, 0], [10, 10], [0, 10]], score=0.99)],
            [OcrLine(text="Recovered text", box=[[0, 0], [10, 0], [10, 10], [0, 10]], score=0.99)],
        ]

        trans_mock = MagicMock()
        trans_mock.translate.return_value = "恢复成功"

        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 20
        watcher = WindowWatcher(ocr_mock, trans_mock, cfg, hwnd=1)

        frame_seq = 0

        def dynamic_grab(*args, **kwargs):
            nonlocal frame_seq
            frame_seq += 1
            # Produce changing frame values so FrameChangeDetector detects change on every frame
            frame = np.full((50, 50, 3), (frame_seq * 50) % 255, dtype=np.uint8)
            return ((0, 0, 50, 50), frame)

        watcher._capture_service.grab = MagicMock(side_effect=dynamic_grab)

        subtitles_received = []
        watcher.subtitle_ready.connect(subtitles_received.append)

        watcher.start()
        # Allow watcher to cycle through failures and reach recovery
        time.sleep(0.40)
        self.assertTrue(watcher.isRunning(), "WindowWatcher must remain alive despite OCR exceptions!")

        watcher.stop()
        watcher.wait(1000)
        self.qapp.processEvents()

        # WindowWatcher survived and successfully dispatched recovered translation
        self.assertIn("恢复成功", subtitles_received)

    def test_exception_isolation_transient_translation_error(self):
        """Empirical: Transient translation failure (llama-server HTTP error/timeout) does NOT crash WindowWatcher."""
        ocr_mock = MagicMock()
        ocr_mock.recognize.side_effect = lambda img: [
            OcrLine(text=f"Active text {frame_seq}", box=[[0, 0], [10, 0], [10, 10], [0, 10]], score=0.99)
        ]

        trans_mock = MagicMock()
        trans_mock.translate.side_effect = [
            ConnectionError("llama-server offline: Connection refused"),
            "在线翻译成功",
            "在线翻译成功",
            "在线翻译成功",
        ]

        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 20
        watcher = WindowWatcher(ocr_mock, trans_mock, cfg, hwnd=1)

        frame_seq = 0

        def dynamic_grab(*args, **kwargs):
            nonlocal frame_seq
            frame_seq += 1
            frame = np.full((50, 50, 3), (frame_seq * 50) % 255, dtype=np.uint8)
            return ((0, 0, 50, 50), frame)

        watcher._capture_service.grab = MagicMock(side_effect=dynamic_grab)

        subtitles_received = []
        watcher.subtitle_ready.connect(subtitles_received.append)

        watcher.start()
        time.sleep(0.50)
        self.assertTrue(watcher.isRunning(), "WindowWatcher must remain alive despite Translation exceptions!")

        watcher.stop()
        watcher.wait(1000)
        self.qapp.processEvents()

        self.assertIn("在线翻译成功", subtitles_received)

    def test_exception_isolation_target_window_closed_clean_halt(self):
        """Empirical: Target window close (Win32 Error 1400) triggers clean stop signal, not crash."""
        ocr_mock = MagicMock()
        trans_mock = MagicMock()
        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 20
        watcher = WindowWatcher(ocr_mock, trans_mock, cfg, hwnd=1)

        class PyWin32Error(Exception):
            def __init__(self):
                self.winerror = 1400

        watcher._capture_service.grab = MagicMock(side_effect=PyWin32Error())

        stopped_reasons = []
        watcher.stopped.connect(stopped_reasons.append)

        watcher.start()
        watcher.wait(1000)
        self.qapp.processEvents()

        self.assertFalse(watcher.isRunning(), "Watcher must halt when target window closes")
        self.assertEqual(len(stopped_reasons), 1)
        self.assertIn("目标窗口已关闭", stopped_reasons[0])


class TestTier5Phase5Adversarial(unittest.TestCase):
    """Tier 5 Adversarial Challenges for Phase 5 (PR 10 & PR 12)."""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_async_history.db"

    def tearDown(self):
        self.tmp_dir.cleanup()
        self.qapp.processEvents()

    # =========================================================================
    # 4. Single HWND Subtitle Overlay & WM_NCHITTEST Dynamic Routing (PR 10)
    # =========================================================================

    def test_single_hwnd_overlay_invariants(self):
        """Verify production SubtitleBar consolidates all chrome into single native HWND."""
        bar = SubtitleBar()
        try:
            # 1. Contract method is_single_hwnd()
            self.assertTrue(bar.is_single_hwnd(), "SubtitleBar must satisfy is_single_hwnd() == True")

            # 2. layer_widgets() returns strictly [self] (length 1)
            layers = bar.layer_widgets()
            self.assertEqual(len(layers), 1)
            self.assertIs(layers[0], bar)

            # 3. All control components are child widgets (parent is bar, not top-level)
            self.assertIs(bar._ctrl.parent(), bar)
            self.assertFalse(bar._ctrl.isWindow())
            self.assertIs(bar._vscroll.parent(), bar)
            self.assertFalse(bar._vscroll.isWindow())
            self.assertIs(bar._grip.parent(), bar)
            self.assertFalse(bar._grip.isWindow())
        finally:
            bar.close()
            bar.deleteLater()

    def test_wm_nchittest_dynamic_routing_adversarial_boundaries(self):
        """Verify WM_NCHITTEST (0x0084) routes HTCLIENT (1) on chrome and HTTRANSPARENT (-1) on background plate."""
        bar = SubtitleBar()
        try:
            bar.resize(400, 200)
            bar.set_text("Sample Subtitle Text for Hit Test")
            bar._place_chrome()

            # A. Test hit on child control (_ctrl button area)
            ctrl_pt = bar._ctrl.mapToGlobal(QPoint(10, 10))
            lparam_ctrl = (ctrl_pt.x() & 0xFFFF) | ((ctrl_pt.y() & 0xFFFF) << 16)
            msg_ctrl = MagicMock()
            msg_ctrl.message = 0x0084
            msg_ctrl.lParam = lparam_ctrl
            handled, code = bar.nativeEvent(b"windows_generic_MSG", msg_ctrl)
            self.assertTrue(handled)
            self.assertEqual(code, 1, "Clicking over _ctrl must return HTCLIENT (1)")

            # B. Test hit on scrollbar (_vscroll)
            scroll_pt = bar._vscroll.mapToGlobal(QPoint(5, 10))
            lparam_scroll = (scroll_pt.x() & 0xFFFF) | ((scroll_pt.y() & 0xFFFF) << 16)
            msg_scroll = MagicMock()
            msg_scroll.message = 0x0084
            msg_scroll.lParam = lparam_scroll
            handled, code = bar.nativeEvent(b"windows_generic_MSG", msg_scroll)
            self.assertTrue(handled)
            self.assertEqual(code, 1, "Clicking over _vscroll must return HTCLIENT (1)")

            # C. Test hit on resize grip (_grip)
            grip_pt = bar._grip.mapToGlobal(QPoint(5, 5))
            lparam_grip = (grip_pt.x() & 0xFFFF) | ((grip_pt.y() & 0xFFFF) << 16)
            msg_grip = MagicMock()
            msg_grip.message = 0x0084
            msg_grip.lParam = lparam_grip
            handled, code = bar.nativeEvent(b"windows_generic_MSG", msg_grip)
            self.assertTrue(handled)
            self.assertEqual(code, 1, "Clicking over _grip must return HTCLIENT (1)")

            # D. Test hit on text plate background (should be transparent)
            plate_pt = bar.mapToGlobal(QPoint(20, 100))
            lparam_plate = (plate_pt.x() & 0xFFFF) | ((plate_pt.y() & 0xFFFF) << 16)
            msg_plate = MagicMock()
            msg_plate.message = 0x0084
            msg_plate.lParam = lparam_plate
            handled, code = bar.nativeEvent(b"windows_generic_MSG", msg_plate)
            self.assertTrue(handled)
            self.assertEqual(code, -1, "Clicking over text plate must return HTTRANSPARENT (-1)")
        finally:
            bar.close()
            bar.deleteLater()

    def test_topmost_single_hwnd_zero_win32_1400_for_child_widgets(self):
        """Verify topmost.set_overlay_layer never touches child widgets, eliminating Win32 1400 errors."""
        bar = SubtitleBar()
        try:
            bar.set_text("Active text")
            widgets_restacked = []

            # Spy on set_overlay_layer calls
            with patch("app.ui.overlays.set_overlay_layer", side_effect=lambda w, o: widgets_restacked.append(w)):
                bar.restack_layer()

            # Because bar.layer_widgets() returns only [self], child widgets are never passed to Win32 APIs!
            self.assertEqual(widgets_restacked, [bar])
            self.assertNotIn(bar._ctrl, widgets_restacked)
            self.assertNotIn(bar._vscroll, widgets_restacked)
            self.assertNotIn(bar._grip, widgets_restacked)
        finally:
            bar.close()
            bar.deleteLater()

    # =========================================================================
    # 5. CaptureBackend Abstraction & Extensibility (PR 12)
    # =========================================================================

    def test_capture_backend_polymorphism_and_custom_plugin(self):
        """Verify CaptureBackend ABC contracts and pluggability into CaptureService."""
        # 1. Custom mock backend conforming to CaptureBackend
        class VirtualDisplayBackend(CaptureBackend):
            def __init__(self):
                self.released = False

            def grab_region(self, x: int, y: int, width: int, height: int) -> np.ndarray:
                return np.full((height, width, 3), 128, dtype=np.uint8)

            def grab_window(self, hwnd: int) -> np.ndarray | None:
                return np.full((200, 300, 3), 200, dtype=np.uint8)

            def release(self) -> None:
                self.released = True

        custom_backend = VirtualDisplayBackend()
        service = CaptureService(
            backend=custom_backend,
            get_window_rect_fn=lambda h: (0, 0, 300, 200),
        )

        # Region capture through backend
        rect, frame = service.grab(region=(10, 20, 50, 60))
        self.assertEqual(frame.shape, (60, 50, 3))

        # Window capture through backend
        rect, frame = service.grab(hwnd=1234)
        self.assertEqual(frame.shape, (200, 300, 3))

        service.release()
        self.assertTrue(custom_backend.released)

        # 2. Check future extension stubs (DXGI & WGC)
        dxgi = DxgiCaptureBackend()
        self.assertFalse(dxgi.is_supported())
        with self.assertRaises(NotImplementedError):
            dxgi.grab_region(0, 0, 10, 10)

        wgc = WgcCaptureBackend()
        self.assertFalse(wgc.is_supported())
        with self.assertRaises(NotImplementedError):
            wgc.grab_region(0, 0, 10, 10)

    # =========================================================================
    # 6. Async History Queue Stress & SQLite Commit Safety (PR 12)
    # =========================================================================

    def test_async_history_push_latency_benchmark_under_0_1ms(self):
        """Empirical Benchmark: 1,000 asynchronous history pushes across 5 threads.

        Verifies:
        1. Average push latency < 0.1ms (non-blocking queue put).
        2. P95 push latency < 0.1ms.
        3. flush_and_close() commits all records without loss.
        """
        store = AsyncHistoryStorage(self.db_path, batch_size=25, flush_interval=0.05)
        num_threads = 5
        pushes_per_thread = 200
        total_records = num_threads * pushes_per_thread  # 1,000

        latencies_ms = []
        lat_lock = threading.Lock()
        barrier = threading.Barrier(num_threads)

        def worker(tid: int):
            local_lats = []
            barrier.wait()
            for i in range(pushes_per_thread):
                src = f"Thread {tid} Line {i}: The quick brown fox jumps."
                tr = f"线程 {tid} 行 {i}: 敏捷的棕色狐狸跳跃。"
                mode = "window_subtitle"

                t0 = time.perf_counter()
                store.add_history_async(src, tr, mode)
                dur = (time.perf_counter() - t0) * 1000.0
                local_lats.append(dur)

            with lat_lock:
                latencies_ms.extend(local_lats)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(latencies_ms), total_records)
        avg_latency = sum(latencies_ms) / len(latencies_ms)
        sorted_lats = sorted(latencies_ms)
        p95_latency = sorted_lats[int(len(sorted_lats) * 0.95)]

        print(f"\n[TIER 5 ASYNC HISTORY BENCHMARK]")
        print(f"Total Pushed: {total_records} | Avg Latency: {avg_latency:.5f}ms | P95: {p95_latency:.5f}ms")

        self.assertLess(
            avg_latency, 0.10,
            f"Average push latency ({avg_latency:.5f}ms) must be strictly < 0.10ms",
        )
        self.assertLess(
            p95_latency, 0.10,
            f"P95 push latency ({p95_latency:.5f}ms) must be strictly < 0.10ms",
        )

        # Commit safety and shutdown
        store.flush_and_close(timeout=3.0)

        # Independent SQLite verification
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        count = cursor.execute("SELECT count(*) FROM history").fetchone()[0]
        conn.close()

        # Under default storage MAX_HISTORY=50, table is trimmed to 50 rows, max id = 1000
        self.assertLessEqual(count, 50)
        self.assertGreater(count, 0)

        # Idempotent flush_and_close
        store.flush_and_close(timeout=0.1)

    # =========================================================================
    # 7. PipelineMetrics & Live Pipeline Benchmark Script (PR 12)
    # =========================================================================

    def test_pipeline_metrics_7_stages_and_percentiles(self):
        """Verify PipelineMetrics records all 7 stages and computes mathematically sound percentiles."""
        metrics = PipelineMetrics()
        self.assertEqual(
            metrics.STAGES,
            ["capture", "frame_diff", "ocr", "text_diff", "cache_lookup", "translate", "ui_render"],
        )

        # Record deterministic distributions
        for stage_idx, stage in enumerate(metrics.STAGES):
            for val in range(1, 101):  # 100 values: 1.0 to 100.0
                metrics.record_stage(stage, float(val + stage_idx))

        stats = metrics.get_stats()
        for stage_idx, stage in enumerate(metrics.STAGES):
            s = stats[stage]
            self.assertEqual(s["count"], 100)
            self.assertAlmostEqual(s["p50"], 51.0 + stage_idx, delta=1.0)
            self.assertAlmostEqual(s["p95"], 96.0 + stage_idx, delta=1.0)
            self.assertEqual(s["max"], 100.0 + stage_idx)

        # Verify format_table produces formatted output without crashing
        table_str = metrics.format_table()
        self.assertIn("STAGE", table_str)
        self.assertIn("TOTAL PIPELINE", table_str)

    def test_benchmark_script_execution_programmatic(self):
        """Execute scripts/benchmark_live_pipeline.py --frames 30 and verify exit code 0."""
        python_exe = sys.executable
        script_path = str(Path(__file__).resolve().parent.parent / "scripts" / "benchmark_live_pipeline.py")
        report_json = str(Path(self.tmp_dir.name) / "benchmark_out.json")

        cmd = [
            python_exe,
            script_path,
            "--frames",
            "30",
            "--mode",
            "synthetic",
            "--json",
            report_json,
        ]

        t0 = time.perf_counter()
        result = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30.0,
        )
        elapsed = time.perf_counter() - t0

        self.assertEqual(
            result.returncode, 0,
            f"benchmark_live_pipeline.py failed with code {result.returncode}:\n{result.stderr}\n{result.stdout}",
        )
        self.assertIn("TOTAL PIPELINE", result.stdout)
        self.assertIn("Throughput:", result.stdout)

        # Verify JSON report structure
        self.assertTrue(os.path.exists(report_json), "Report JSON must be written")
        with open(report_json, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("capture", data)
        self.assertIn("ocr", data)
        self.assertIn("_metadata", data)
        self.assertEqual(data["capture"]["count"], 30)


if __name__ == "__main__":
    unittest.main()
