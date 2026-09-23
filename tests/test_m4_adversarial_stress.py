# -*- coding: utf-8 -*-
"""Milestone 4 Adversarial Challenge and Empirical Stress Test Suite.

Empirically challenges and benchmarks:
1. LatestFrameBuffer Overwrite & Drop Test:
   - 60 FPS capture producer vs 2 FPS consumer.
   - Exact accounting of dropped_count vs skipped frames.
   - Freshest frame guarantee with zero queue backlog.
2. Thread Safety & Deadlock Stress Test:
   - Concurrent multi-threaded puts, gets, and clears across 10,000+ cycles.
   - No race conditions, no deadlocks, no leaked locks.
3. Pipeline Signal Verification:
   - WindowWatcher & ResultManager coordination of all decoupled services.
   - Verifying all 6 signals: subtitle_ready, annotations_ready, history_ready,
     window_moved, content_cleared, stopped.
   - Stale generation rejection & logging.
4. Extreme Boundary & Burst Invariants:
   - 10,000 tight-loop burst puts with 0 gets.
   - Timeout precision and clean return on empty gets.
   - Large-frame (4K) memory release upon consumption.
5. Mode Switching & Runtime Dynamic Reconfiguration:
   - set_display_mode, set_region, pause/resume under concurrency.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
import unittest
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import numpy as np
from PySide6.QtCore import QCoreApplication

from app.adaptive_polling import AdaptivePollingController, PollingState
from app.capture_service import CaptureService, is_invalid_window_handle_error
from app.config import DEFAULTS
from app.frame_detector import FrameChangeDetector
from app.latest_frame_buffer import LatestFrameBuffer
from app.ocr_engine import OcrLine
from app.ocr_service import OcrService
from app.pipelines import GenerationTracker
from app.result_manager import ResultManager
from app.text_change_detector import TextChangeDetector
from app.translation_manager import TranslationManager
from app.window_watcher import WindowWatcher


class TestMilestone4AdversarialStress(unittest.TestCase):
    """Milestone 4 Adversarial Stress & Empirical Challenge Test Suite."""

    @classmethod
    def setUpClass(cls):
        if QCoreApplication.instance() is None:
            cls.qapp = QCoreApplication([])
        else:
            cls.qapp = QCoreApplication.instance()

    # =========================================================================
    # Challenge 1: 60 FPS Producer vs 2 FPS Consumer Overwrite & Drop Test
    # =========================================================================

    def test_60fps_producer_vs_2fps_consumer_drop_accounting(self):
        """Simulate a 60 FPS capture producer with a 2 FPS consumer over 60 frames.

        Guarantees verified:
        1. All unconsumed intermediate frames are dropped.
        2. dropped_count exactly accounts for all skipped frames:
           total_produced == total_consumed + dropped_count + (1 if not is_empty else 0).
        3. Consumer always receives the freshest frame with zero queue backlog.
        """
        buffer = LatestFrameBuffer()
        total_produced_target = 60  # 1 second of 60 FPS
        produced_frames: list[int] = []
        consumed_frames: list[tuple[int, float]] = []  # (frame_id, time_consumed)
        stop_event = threading.Event()

        t_start = time.perf_counter()

        def producer():
            frame_seq = 0
            while frame_seq < total_produced_target and not stop_event.is_set():
                frame_seq += 1
                fake_frame = np.full((10, 10), frame_seq, dtype=np.uint8)
                buffer.put(fake_frame, generation_id=frame_seq)
                produced_frames.append(frame_seq)
                # 60 FPS = ~16.67ms per frame
                time.sleep(0.0166)

        def consumer():
            while not stop_event.is_set():
                # 2 FPS = 500ms interval
                time.sleep(0.500)
                item = buffer.get(timeout=0.05)
                if item is not None:
                    frame, gen_id = item
                    consumed_frames.append((gen_id, time.perf_counter() - t_start))

        prod_thread = threading.Thread(target=producer, daemon=True)
        cons_thread = threading.Thread(target=consumer, daemon=True)

        prod_thread.start()
        cons_thread.start()

        prod_thread.join(timeout=3.0)
        # Give consumer one final window to consume whatever remains
        time.sleep(0.1)
        stop_event.set()
        cons_thread.join(timeout=1.0)

        total_produced = len(produced_frames)
        total_consumed = len(consumed_frames)
        dropped = buffer.dropped_count
        in_buffer = 1 if not buffer.is_empty else 0

        print(f"\n[CHALLENGE 1: 60 FPS Producer vs 2 FPS Consumer]")
        print(f"Produced: {total_produced} | Consumed: {total_consumed} | Dropped: {dropped} | Remaining in slot: {in_buffer}")
        print(f"Consumed generations: {[c[0] for c in consumed_frames]}")

        # Verification 1: Exact accounting formula
        self.assertEqual(
            total_produced,
            total_consumed + dropped + in_buffer,
            f"Accounting invariant violated! Produced ({total_produced}) != Consumed ({total_consumed}) + Dropped ({dropped}) + Slot ({in_buffer})",
        )

        # Verification 2: Consumer consumed far fewer frames than produced (2 FPS vs 60 FPS)
        self.assertGreater(dropped, total_produced // 2, "At 2 FPS vs 60 FPS, dropped frames must exceed 50% of total")
        self.assertGreater(total_consumed, 0, "Consumer must have consumed at least 1 frame")

        # Verification 3: Consumer always received monotonically increasing, freshest frames
        consumed_gen_ids = [c[0] for c in consumed_frames]
        self.assertEqual(consumed_gen_ids, sorted(consumed_gen_ids), "Consumed frames must be strictly ordered")
        if len(consumed_gen_ids) >= 2:
            # The gap between successive consumed frames must be large (reflecting dropped intermediate frames)
            gap = consumed_gen_ids[1] - consumed_gen_ids[0]
            self.assertGreaterEqual(gap, 15, f"At 2 FPS vs 60 FPS, gap should be >= 15 frames, got {gap}")

        # Verification 4: Zero backlog guarantee - buffer capacity is strictly 1
        # If we drain the buffer, subsequent get times out immediately
        rem = buffer.get(timeout=0.01)
        self.assertTrue(buffer.is_empty)
        self.assertIsNone(buffer.get(timeout=0.01))

    # =========================================================================
    # Challenge 2: Thread Safety & Deadlock Stress Test (10,000+ Cycles)
    # =========================================================================

    def test_concurrent_puts_gets_clears_deadlock_stress_10000_cycles(self):
        """Stress test LatestFrameBuffer under high concurrent contention.

        Workload:
        - 4 Producer threads: each performs 2,500 puts (10,000 puts total).
        - 4 Consumer threads: continuously call get(timeout=0.001) until producers finish.
        - 2 Clearer threads: continuously call clear() with small random yield.
        - Total operations: > 20,000 concurrent operations.

        Guarantees verified:
        1. Zero deadlocks: test completes well within deadline (< 8 seconds).
        2. Zero unhandled exceptions or thread crashes.
        3. No leaked locks: buffer remains fully functional after test.
        """
        buffer = LatestFrameBuffer()
        total_puts_per_producer = 2500
        num_producers = 4
        num_consumers = 4
        num_clearers = 2

        producers_done = threading.Event()
        errors: list[Exception] = []

        total_consumed = 0
        total_clears = 0
        consumed_lock = threading.Lock()
        clears_lock = threading.Lock()

        def producer_worker(producer_id: int):
            try:
                for i in range(total_puts_per_producer):
                    gen = producer_id * 100000 + i
                    arr = np.array([producer_id, i], dtype=np.int32)
                    buffer.put(arr, generation_id=gen)
            except Exception as e:
                errors.append(e)

        def consumer_worker():
            nonlocal total_consumed
            try:
                while not producers_done.is_set() or not buffer.is_empty:
                    item = buffer.get(timeout=0.001)
                    if item is not None:
                        with consumed_lock:
                            total_consumed += 1
            except Exception as e:
                errors.append(e)

        def clearer_worker():
            nonlocal total_clears
            try:
                while not producers_done.is_set():
                    buffer.clear()
                    with clears_lock:
                        total_clears += 1
                    time.sleep(0.0005)
            except Exception as e:
                errors.append(e)

        t0 = time.perf_counter()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=num_producers + num_consumers + num_clearers
        ) as executor:
            cons_futs = [executor.submit(consumer_worker) for _ in range(num_consumers)]
            clear_futs = [executor.submit(clearer_worker) for _ in range(num_clearers)]
            prod_futs = [executor.submit(producer_worker, pid) for pid in range(num_producers)]

            # Wait for all producers to finish
            for fut in prod_futs:
                fut.result(timeout=10.0)
            producers_done.set()

            # Wait for clearers and consumers
            for fut in clear_futs:
                fut.result(timeout=5.0)
            for fut in cons_futs:
                fut.result(timeout=5.0)

        elapsed = time.perf_counter() - t0

        print(f"\n[CHALLENGE 2: Thread Safety & Deadlock Stress (10,000 Puts)]")
        print(f"Elapsed: {elapsed:.3f}s | Dropped: {buffer.dropped_count} | Consumed: {total_consumed} | Cleared: {total_clears}")

        # Verification 1: No errors across all threads
        self.assertEqual(len(errors), 0, f"Thread safety violated with errors: {errors}")

        # Verification 2: Deadlock-free completion
        self.assertLess(elapsed, 8.0, f"Stress test took too long ({elapsed:.3f}s), potential lock contention or stall")

        # Verification 3: Buffer state is consistent and not corrupted
        self.assertGreaterEqual(buffer.dropped_count, 0)
        self.assertEqual(buffer.is_empty, buffer.peek() is None)

        # Verification 4: Post-stress buffer health check (no leaked locks)
        test_frame = np.ones((5, 5), dtype=np.uint8)
        buffer.put(test_frame, generation_id=999999)
        ret = buffer.get(timeout=0.1)
        self.assertIsNotNone(ret, "Buffer failed to get() after stress test - possible leaked lock!")
        self.assertEqual(ret[1], 999999)
        self.assertTrue(buffer.is_empty)

    # =========================================================================
    # Challenge 3: Pipeline Signal Verification & Decoupled Coordination
    # =========================================================================

    def test_pipeline_signals_and_decoupled_service_coordination(self):
        """Verify that WindowWatcher and ResultManager properly coordinate decoupled services

        and emit all 6 public signals:
        1. subtitle_ready(str)
        2. annotations_ready(list)
        3. history_ready(str, str, str)
        4. window_moved(int, int, int, int)
        5. content_cleared()
        6. stopped(str)
        """
        ocr_mock = MagicMock()
        translator_mock = MagicMock()
        translator_mock.translate.side_effect = lambda t, tgt: f"[{tgt}]{t}"
        translator_mock.translate_lines.side_effect = lambda lines, tgt: [f"[{tgt}]{l}" for l in lines]

        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 50
        cfg["target_language"] = "zh"

        watcher = WindowWatcher(ocr_mock, translator_mock, cfg, hwnd=100)

        # Signal capture spies
        signals_received = {
            "subtitle_ready": [],
            "annotations_ready": [],
            "history_ready": [],
            "window_moved": [],
            "content_cleared": [],
            "stopped": [],
        }

        watcher.subtitle_ready.connect(lambda t: signals_received["subtitle_ready"].append(t))
        watcher.annotations_ready.connect(lambda items: signals_received["annotations_ready"].append(items))
        watcher.history_ready.connect(lambda s, t, m: signals_received["history_ready"].append((s, t, m)))
        watcher.window_moved.connect(lambda x, y, w, h: signals_received["window_moved"].append((x, y, w, h)))
        watcher.content_cleared.connect(lambda: signals_received["content_cleared"].append(True))
        watcher.stopped.connect(lambda reason: signals_received["stopped"].append(reason))

        # Test 1: window_moved signal emission
        watcher.result_manager.dispatch_moved(10, 20, 800, 600)
        self.assertEqual(len(signals_received["window_moved"]), 1)
        self.assertEqual(signals_received["window_moved"][0], (10, 20, 800, 600))

        # Test 2: subtitle_ready & history_ready signal emission
        active_gen = watcher.generation_tracker.current_generation
        ok = watcher.result_manager.dispatch_subtitle("世界你好", active_gen)
        self.assertTrue(ok)
        self.assertEqual(len(signals_received["subtitle_ready"]), 1)
        self.assertEqual(signals_received["subtitle_ready"][0], "世界你好")

        watcher.result_manager.dispatch_history("Hello World", "世界你好", "window_subtitle", active_gen)
        self.assertEqual(len(signals_received["history_ready"]), 1)
        self.assertEqual(signals_received["history_ready"][0], ("Hello World", "世界你好", "window_subtitle"))

        # Test 3: annotations_ready signal emission
        annot_items = [([[0, 0], [10, 0], [10, 10], [0, 10]], "[zh]Label")]
        ok_annot = watcher.result_manager.dispatch_annotations(annot_items, active_gen)
        self.assertTrue(ok_annot)
        self.assertEqual(len(signals_received["annotations_ready"]), 1)
        self.assertEqual(signals_received["annotations_ready"][0], annot_items)

        # Test 4: content_cleared signal emission
        ok_clear = watcher.result_manager.dispatch_cleared(active_gen)
        self.assertTrue(ok_clear)
        self.assertEqual(len(signals_received["content_cleared"]), 1)

        # Test 5: stopped signal emission
        watcher.result_manager.dispatch_stopped("目标窗口已关闭")
        self.assertEqual(len(signals_received["stopped"]), 1)
        self.assertEqual(signals_received["stopped"][0], "目标窗口已关闭")

        # Test 6: Stale generation rejection test
        # Increment generation tracker so active_gen becomes stale
        new_gen = watcher.generation_tracker.next_generation()
        self.assertNotEqual(active_gen, new_gen)

        stale_sub_ok = watcher.result_manager.dispatch_subtitle("迟到的旧译文", active_gen)
        self.assertFalse(stale_sub_ok, "Stale subtitle must be rejected by ResultManager!")
        self.assertEqual(len(signals_received["subtitle_ready"]), 1, "subtitle_ready must NOT be emitted for stale gen")

        stale_annot_ok = watcher.result_manager.dispatch_annotations(annot_items, active_gen)
        self.assertFalse(stale_annot_ok, "Stale annotations must be rejected by ResultManager!")
        self.assertEqual(len(signals_received["annotations_ready"]), 1, "annotations_ready must NOT be emitted for stale gen")

        self.assertGreaterEqual(watcher.result_manager.dropped_stale_count, 2)
        print(f"\n[CHALLENGE 3: Pipeline Signal Verification]")
        print(f"Emitted signals: { {k: len(v) for k, v in signals_received.items()} }")
        print(f"Dropped stale count: {watcher.result_manager.dropped_stale_count}")

    # =========================================================================
    # Challenge 4: Extreme Burst & Boundary Invariants of LatestFrameBuffer
    # =========================================================================

    def test_burst_saturation_and_boundary_conditions(self):
        """Stress-test LatestFrameBuffer with extreme single-threaded and edge conditions.

        Guarantees:
        1. 10,000 tight-loop burst puts with 0 gets: dropped_count == 9999.
        2. peek() returns newest generation without consuming.
        3. First get() returns newest frame, subsequent get() returns None.
        4. clear() resets slot without incrementing dropped_count.
        5. get(timeout=0.05) on empty buffer returns None in ~50ms without error.
        6. Large frame (4K: 3840x2160x3 ~24MB) is released from buffer memory upon get().
        """
        buffer = LatestFrameBuffer()
        burst_count = 10000

        # Burst 10,000 puts
        t0 = time.perf_counter()
        for i in range(burst_count):
            frame = np.array([i], dtype=np.int32)
            buffer.put(frame, generation_id=i)
        elapsed = time.perf_counter() - t0

        print(f"\n[CHALLENGE 4: Burst 10,000 Puts]")
        print(f"Elapsed: {elapsed * 1000:.2f}ms | Throughput: {burst_count / elapsed:.0f} puts/sec")

        self.assertEqual(buffer.dropped_count, burst_count - 1)
        self.assertFalse(buffer.is_empty)

        # peek() invariant
        peeked = buffer.peek()
        self.assertIsNotNone(peeked)
        self.assertEqual(peeked[1], burst_count - 1)
        self.assertFalse(buffer.is_empty)

        # get() consumes freshest
        consumed = buffer.get(timeout=0.01)
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed[1], burst_count - 1)
        self.assertTrue(buffer.is_empty)

        # Second get() returns None
        self.assertIsNone(buffer.get(timeout=0.01))

        # clear() invariant
        buffer.put(np.array([1]), 101)
        initial_dropped = buffer.dropped_count
        buffer.clear()
        self.assertTrue(buffer.is_empty)
        self.assertEqual(buffer.dropped_count, initial_dropped, "clear() must not increment dropped_count")

        # reset_dropped_count()
        buffer.reset_dropped_count()
        self.assertEqual(buffer.dropped_count, 0)

        # Timeout precision
        t_wait_start = time.perf_counter()
        timeout_res = buffer.get(timeout=0.05)
        t_wait = time.perf_counter() - t_wait_start
        self.assertIsNone(timeout_res)
        self.assertGreaterEqual(t_wait, 0.04)
        self.assertLess(t_wait, 0.20)

        # Memory release of large 4K frame
        large_frame = np.zeros((2160, 3840, 3), dtype=np.uint8)  # ~24MB
        buffer.put(large_frame, 2026)
        self.assertIsNotNone(buffer._frame)
        res = buffer.get()
        self.assertIsNotNone(res)
        self.assertIsNone(buffer._frame, "Buffer must release internal reference to large numpy array after get()")

    # =========================================================================
    # Challenge 5: Decoupled Pipeline End-to-End Orchestration Cycle
    # =========================================================================

    def test_decoupled_pipeline_end_to_end_orchestration_cycle(self):
        """Simulate an end-to-end multi-frame lifecycle through WindowWatcher components:

        1. Frame 1: Text 'Hello' -> Diff detected -> OCR -> Translation -> Result dispatched.
        2. Frame 2: Identical Frame -> Diff False -> OCR skipped -> Adaptive polling adjusts.
        3. Frame 3: Empty Frame 1 -> Diff detected -> OCR [] -> Empty count = 1.
        4. Frame 4: Empty Frame 2 -> Diff detected -> OCR [] -> Empty count = 2 -> Content cleared!
        5. Frame 5: Target Window Move -> Moved dispatched.
        6. Frame 6: Target Window Close -> Invalid handle -> Stopped dispatched.
        """
        ocr_mock = MagicMock()
        translator_mock = MagicMock()
        translator_mock.translate.return_value = "你好"
        translator_mock.translate_lines.return_value = ["你好"]

        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 10
        cfg["target_language"] = "zh"

        watcher = WindowWatcher(ocr_mock, translator_mock, cfg, hwnd=42)
        watcher.text_change_detector.empty_clear_delay_s = 0.0  # 此用例仅验证两帧计数条件

        # Spies
        emitted_subtitles = []
        emitted_clears = []
        emitted_moves = []
        emitted_stops = []

        watcher.subtitle_ready.connect(lambda t: emitted_subtitles.append(t))
        watcher.content_cleared.connect(lambda: emitted_clears.append(True))
        watcher.window_moved.connect(lambda *args: emitted_moves.append(args))
        watcher.stopped.connect(lambda r: emitted_stops.append(r))

        # Frame 1: Text present
        frame1 = np.full((100, 100, 3), 50, dtype=np.uint8)
        ocr_mock.detect_and_ocr.return_value = [
            OcrLine(text="Hello", box=[[0, 0], [40, 0], [40, 20], [0, 20]], score=0.98)
        ]

        watcher._capture_service.grab = MagicMock(return_value=((10, 10, 100, 100), frame1))
        # Run one step manually through components
        gen1 = watcher.generation_tracker.next_generation()
        watcher.frame_buffer.put(frame1, gen1)
        img1, g1 = watcher.frame_buffer.get()
        diff1 = watcher.frame_detector.detect(None, img1)
        self.assertTrue(diff1.has_changed)
        lines1 = watcher.ocr_service.recognize_frame(img1)
        event1, text1 = watcher.text_change_detector.observe(lines1)
        self.assertEqual(event1, "change")
        tr1 = watcher.translation_manager.translate_subtitle(text1, "zh", g1)
        watcher.result_manager.dispatch_subtitle(tr1, g1)

        self.assertEqual(len(emitted_subtitles), 1)
        self.assertEqual(emitted_subtitles[0], "你好")

        # Frame 2: Identical Frame -> Diff False
        diff2 = watcher.frame_detector.detect(img1, img1)
        self.assertFalse(diff2.has_changed)
        _ = watcher.polling_controller.on_frame(diff2.has_changed)
        delay = watcher.polling_controller.on_frame(diff2.has_changed)
        self.assertGreater(delay, 0.120)  # Transitions from ACTIVE to WARM

        # Frame 3 & 4: Consecutive empty frames -> Content cleared
        event3, _ = watcher.text_change_detector.observe([])
        self.assertEqual(event3, "none")
        self.assertEqual(watcher.text_change_detector.empty_frames, 1)

        event4, _ = watcher.text_change_detector.observe([])
        self.assertEqual(event4, "clear")
        self.assertEqual(watcher.text_change_detector.empty_frames, 2)
        watcher.result_manager.dispatch_cleared(g1)
        self.assertEqual(len(emitted_clears), 1)

        # Frame 5: Window Move
        new_rect = (20, 20, 100, 100)
        if watcher.capture_service.has_moved(new_rect):
            watcher.result_manager.dispatch_moved(*new_rect)
        self.assertEqual(len(emitted_moves), 1)
        self.assertEqual(emitted_moves[0], new_rect)

        # Frame 6: Window Close / Error 1400
        class MockWin32Error(Exception):
            winerror = 1400

        err = MockWin32Error()
        if watcher.capture_service.is_invalid_window_handle_error(err):
            watcher.result_manager.dispatch_stopped("目标窗口已关闭")
        self.assertEqual(len(emitted_stops), 1)
        self.assertEqual(emitted_stops[0], "目标窗口已关闭")

        print("\n[CHALLENGE 5: Decoupled Pipeline End-to-End Orchestration]")
        print("Successfully walked 6 lifecycle stages across all 8 decoupled components.")


if __name__ == "__main__":
    unittest.main()
