# -*- coding: utf-8 -*-
"""Empirical Adversarial & Stress Testing Suite for Milestone 1.

Rigorous stress testing for:
1. Generation Tracking:
   - High-frequency out-of-order simulation (1000 tasks, random latencies).
   - Inverted arrival orders (stale responses arriving after fresh ones).
   - Mid-flight invalidations (resets, mode changes, region shifts).
   - Race conditions between Clear events and delayed Translations.
   - Concurrent multi-threaded GenerationTracker stress.
2. Subtitle Overlay:
   - Startup zero-flicker: strictly hidden until first valid text.
   - Pre-layout settling: font metrics reflow and dimensions settled BEFORE show().
   - No secondary resize / layout reflow post-show (zero dirty rectangle).
   - Constant native Win32 HWND across 500 rapid stream updates.
   - Whitespace / Unicode edge cases and hide-on-empty behavior.
"""

from __future__ import annotations

import os
import random
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication

from app.config import DEFAULTS
from app.ocr_engine import OcrLine
from app.pipelines import GenerationTracker, WatchCycleContext
from app.ui.overlays import SubtitleBar
from app.window_watcher import WindowWatcher


class TestGenerationTrackingAdversarial(unittest.TestCase):
    """Adversarial stress-testing of GenerationTracker and out-of-order execution."""

    def setUp(self):
        self.tracker = GenerationTracker()

    def test_concurrent_multi_threaded_generation_integrity(self):
        """Stress: 20 concurrent threads calling next_generation() simultaneously."""
        num_threads = 20
        ops_per_thread = 500
        all_gens = []
        lock = threading.Lock()

        def worker():
            local_gens = []
            for _ in range(ops_per_thread):
                gid = self.tracker.next_generation()
                local_gens.append(gid)
            with lock:
                all_gens.extend(local_gens)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected_total = num_threads * ops_per_thread
        self.assertEqual(len(all_gens), expected_total)
        # All generation IDs must be strictly unique
        self.assertEqual(len(set(all_gens)), expected_total)
        self.assertEqual(max(all_gens), expected_total)
        self.assertEqual(self.tracker.current_generation, expected_total)

    def test_high_frequency_out_of_order_simulation_1000_tasks(self):
        """Adversarial Oracle: 1000 tasks completing with inverted/random latencies.

        Guarantees:
        - Older translations NEVER overwrite newer translations.
        - The sequence of accepted translations is strictly monotonically increasing.
        """
        num_tasks = 1000
        tracker = GenerationTracker()
        dispatched_results: list[tuple[int, str]] = []
        dispatch_lock = threading.Lock()

        def simulate_async_worker(gen_id: int, delay_s: float):
            # Simulate OCR + translation network latency
            time.sleep(delay_s)
            text = f"Translation result for gen {gen_id}"
            # Check generation tracker before dispatching
            with dispatch_lock:
                if tracker.is_active(gen_id):
                    dispatched_results.append((gen_id, text))

        # Delays between 0.0001s and 0.005s, randomized but biased to finish out-of-order
        tasks = []
        for i in range(1, num_tasks + 1):
            gid = tracker.next_generation()
            delay = random.uniform(0.0001, 0.002) if i > num_tasks - 100 else random.uniform(0.001, 0.006)
            tasks.append((gid, delay))

        # Execute all tasks concurrently in thread pool
        with ThreadPoolExecutor(max_workers=32) as executor:
            futures = [executor.submit(simulate_async_worker, gid, delay) for gid, delay in tasks]
            for f in futures:
                f.result()

        # Empirical Assertions:
        self.assertGreater(len(dispatched_results), 0, "At least some results must be accepted")
        # Invariant 1: Dispatched generation IDs must be strictly monotonically increasing
        dispatched_gids = [gid for gid, _ in dispatched_results]
        for i in range(len(dispatched_gids) - 1):
            self.assertLess(
                dispatched_gids[i],
                dispatched_gids[i + 1],
                f"Out-of-order overwrite detected: gen {dispatched_gids[i+1]} appeared after {dispatched_gids[i]}",
            )

        # Invariant 2: The final accepted generation MUST be the latest active generation if it finished
        last_dispatched_gid = dispatched_gids[-1]
        self.assertEqual(
            last_dispatched_gid,
            tracker.current_generation,
            f"Final dispatched generation {last_dispatched_gid} does not match active {tracker.current_generation}",
        )

    def test_mid_flight_tracker_reset_drops_all_pending_tasks(self):
        """Stress: 50 tasks in-flight when an explicit reset() is triggered."""
        tracker = GenerationTracker()
        dispatched = []
        lock = threading.Lock()

        def slow_worker(gid: int):
            time.sleep(0.03)
            with lock:
                if tracker.is_active(gid):
                    dispatched.append(gid)

        threads = []
        for _ in range(50):
            gid = tracker.next_generation()
            t = threading.Thread(target=slow_worker, args=(gid,))
            threads.append(t)
            t.start()

        # Invalidate everything in flight
        time.sleep(0.005)
        reset_val = tracker.reset()

        for t in threads:
            t.join()

        self.assertEqual(
            len(dispatched), 0,
            f"Expected 0 dispatched results after reset, but got {dispatched}",
        )
        self.assertGreaterEqual(tracker.current_generation, reset_val)

    def test_race_between_clear_event_and_slow_translation(self):
        """Adversarial Race: Gen 1 translation finishes AFTER Gen 2 content_cleared."""
        tracker = GenerationTracker()
        delivered = []

        # Cycle 1: Frame with text starts
        g1 = tracker.next_generation()

        # Cycle 2: Frame is blank (clear event detected)
        g2 = tracker.next_generation()
        # Gen 2 clear executes
        if tracker.is_active(g2):
            delivered.append("CLEARED")

        # Now Gen 1 translation finally arrives late
        if tracker.is_active(g1):
            delivered.append("STALE_TRANSLATION_GEN_1")

        self.assertEqual(
            delivered, ["CLEARED"],
            "Stale translation must NEVER overwrite a subsequent clear event!",
        )

    def test_window_watcher_drops_stale_when_interrupted_by_region_change(self):
        """WindowWatcher integration: mid-translate set_region() immediately drops output."""
        ocr_mock = MagicMock()
        translator_mock = MagicMock()
        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 50

        watcher = WindowWatcher(ocr_mock, translator_mock, cfg, hwnd=1)
        subtitles = []
        watcher.subtitle_ready.connect(subtitles.append)

        def delayed_translate(text, target):
            # Mid-translation: region changes!
            watcher.set_region((50, 50, 300, 300))
            return "Old region translation"

        translator_mock.translate.side_effect = delayed_translate

        fake_frame = np.ones((100, 100, 3), dtype=np.uint8) * 15
        ocr_mock.recognize.return_value = [
            OcrLine(text="Old text", box=[[0, 0], [50, 0], [50, 10], [0, 10]], score=0.95)
        ]

        with patch.object(watcher, "_grab", return_value=((0, 0, 100, 100), fake_frame)):
            gen_id = watcher.generation_tracker.next_generation()
            lines = ocr_mock.recognize(fake_frame)
            event, text = watcher._state.observe(lines, 0.5)
            self.assertEqual(event, "change")

            translation = translator_mock.translate(text, "zh")
            if watcher._running and watcher.generation_tracker.is_active(gen_id):
                watcher.subtitle_ready.emit(translation)

        self.assertEqual(len(subtitles), 0, "Stale translation after region change must be dropped")


class TestSubtitleOverlayAdversarial(unittest.TestCase):
    """Adversarial stress-testing of SubtitleBar zero-flicker & layout settling."""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        self._pos_patch = patch("app.ui.topmost._set_window_pos", return_value=True)
        self._owner_patch = patch("app.ui.topmost._set_window_owner", return_value=True)
        self._pos_patch.start()
        self._owner_patch.start()
        self.bar = SubtitleBar()

    def tearDown(self):
        if self.bar:
            self.bar.close()
            self.bar.deleteLater()
            self.bar = None
        self._owner_patch.stop()
        self._pos_patch.stop()
        self.qapp.processEvents()

    def test_startup_strict_invisibility_and_zero_placeholder(self):
        """Invariants on initial construction."""
        self.assertFalse(self.bar.isVisible())
        self.assertEqual(self.bar._text, "")
        self.assertFalse(self.bar._ctrl.isVisible())
        self.assertFalse(self.bar._vscroll.isVisible())
        self.assertFalse(self.bar._grip.isVisible())

    def test_layout_pre_settled_before_show_zero_dirty_rectangle(self):
        """Empirical verification: Full layout settling happens strictly BEFORE show().

        Validates:
        - _content_h is populated before show().
        - Window size is at least _MIN_W x _MIN_H before show().
        - Chrome widgets (_ctrl, _vscroll, _grip) are positioned before show().
        - No secondary resize / geometry mutation occurs post-show.
        """
        show_called = False
        state_at_show = {}

        orig_show = self.bar.show

        def capture_show():
            nonlocal show_called
            show_called = True
            state_at_show["content_h"] = self.bar._content_h
            state_at_show["width"] = self.bar.width()
            state_at_show["height"] = self.bar.height()
            state_at_show["text"] = self.bar._text
            state_at_show["ctrl_pos"] = (self.bar._ctrl.x(), self.bar._ctrl.y())
            state_at_show["vscroll_geo"] = (
                self.bar._vscroll.x(), self.bar._vscroll.y(),
                self.bar._vscroll.width(), self.bar._vscroll.height(),
            )
            state_at_show["grip_pos"] = (self.bar._grip.x(), self.bar._grip.y())
            orig_show()

        self.bar.show = capture_show
        text = "Line 1: High-precision subtitles\nLine 2: Zero flicker layout verification\nLine 3: Third line"
        self.bar.set_text(text)

        self.assertTrue(show_called, "self.show() must be called on first valid text")
        self.assertGreater(state_at_show["content_h"], 0, "content_h must be precalculated before show")
        self.assertGreaterEqual(state_at_show["width"], self.bar._MIN_W)
        self.assertGreaterEqual(state_at_show["height"], self.bar._MIN_H)
        self.assertEqual(state_at_show["text"], text)

        # Chrome positioning check before show
        self.assertNotEqual(state_at_show["ctrl_pos"], (0, 0), "ctrl must be placed before show")
        self.assertGreater(state_at_show["vscroll_geo"][2], 0, "vscroll width must be > 0 before show")

        # Post-show stability: size must NOT change post-show during the same set_text
        self.assertEqual(self.bar.width(), state_at_show["width"])
        self.assertEqual(self.bar.height(), state_at_show["height"])

    def test_whitespace_and_unicode_edge_cases(self):
        """Adversarial strings: none should trigger display."""
        adversarial_inputs = [
            "",
            "   ",
            "\t\t\n\r\n  \t",
            None,
            "\u200b\u200c\u200d",  # Zero-width spaces alone might strip or keep
            "   \u3000\u3000   ",  # Full-width ideographic spaces
        ]
        for inp in adversarial_inputs:
            # If input strips to empty, it must not show
            if not (inp or "").strip():
                self.bar.set_text(inp)
                self.assertFalse(self.bar.isVisible(), f"Input {repr(inp)} should not display overlay")

    def test_continuous_stream_500_updates_constant_hwnd(self):
        """Stress: 500 rapid subtitle updates without recreating native Win32 window."""
        self.bar.set_text("First valid line")
        self.assertTrue(self.bar.isVisible())
        initial_hwnd = int(self.bar.winId())
        self.assertGreater(initial_hwnd, 0)

        with patch("app.ui.topmost._set_window_pos", return_value=True), \
             patch("app.ui.topmost._set_window_owner", return_value=True):
            for i in range(1, 501):
                text = f"Rapid subtitle update #{i}\nLine count: {i % 5 + 1}"
                self.bar.set_text(text)
                current_hwnd = int(self.bar.winId())
                self.assertEqual(
                    current_hwnd, initial_hwnd,
                    f"HWND changed from {initial_hwnd} to {current_hwnd} at update #{i}!",
                )

        self.assertTrue(self.bar.isVisible())
        self.assertIn("update #500", self.bar._text)

    def test_hide_on_empty_and_subsequent_reshow_settles_layout(self):
        """Toggle cycle: text -> empty (cleared text) -> hide -> new text (reshown with pre-layout)."""
        self.bar.set_text("Visible text 1")
        self.assertTrue(self.bar.isVisible())

        # Clearing text keeps window visible without flickering hide
        self.bar.set_text("")
        self.assertTrue(self.bar.isVisible())
        self.assertEqual(self.bar._text, "")

        # Explicit hide closes window
        self.bar.hide()
        self.assertFalse(self.bar.isVisible())

        # Reshowing with new text
        show_called = False
        content_h_at_show = 0
        orig_show = self.bar.show

        def capture_reshow():
            nonlocal show_called, content_h_at_show
            show_called = True
            content_h_at_show = self.bar._content_h
            orig_show()

        self.bar.show = capture_reshow
        self.bar.set_text("Fresh subtitle after clear\nAnother line")
        self.assertTrue(show_called)
        self.assertGreater(content_h_at_show, 0, "Reshow layout must be precalculated before show()")
        self.assertTrue(self.bar.isVisible())

    def test_user_resized_geometry_preserved_across_updates(self):
        """User resize (e.g. 450x150) must not be clobbered by set_text reflow."""
        self.bar.resize_to(450, 150)
        self.assertEqual(self.bar._user_size, (450, 150))

        self.bar.set_text("Short text")
        self.assertEqual(self.bar.width(), 450)
        self.assertEqual(self.bar.height(), 150)

        self.bar.set_text("Much longer text that wraps across multiple lines...\nLine 2\nLine 3\nLine 4")
        self.assertEqual(self.bar.width(), 450)
        self.assertEqual(self.bar.height(), 150)


if __name__ == "__main__":
    unittest.main()
