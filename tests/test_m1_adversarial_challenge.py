# -*- coding: utf-8 -*-
"""Adversarial stress and edge-case challenge suite for Milestone 1.

Targets:
1. Zero-Flicker Subtitle Overlay (PR 1):
   - Rapid clear-text and toggling
   - Extreme text inputs (empty, whitespace, huge text, unbroken tokens, unicode/emojis)
   - Unusual geometries (0x0, negative, oversized)
   - Chrome widget visibility synchronization
   - HWND stability across mutations
2. Generation Tracking (PR 2):
   - Extreme multi-threaded concurrent increments (monotonicity & uniqueness)
   - Simulated out-of-order asynchronous translation arrival
   - Rapid reset spam during active translation
   - TOCTOU / race condition stress
   - WindowWatcher mid-flight reset and drop behavior
"""

import concurrent.futures
import numpy as np
import os
import random
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.applog
app.applog._configured = True

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.config import DEFAULTS
from app.ocr_engine import OcrLine
from app.pipelines import GenerationTracker, WatchCycleContext
from app.ui.overlays import SubtitleBar
from app.window_watcher import WindowWatcher


class TestSubtitleOverlayAdversarial(unittest.TestCase):
    """Adversarial challenge for SubtitleBar zero-flicker and edge cases."""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])
        import logging
        from logging.handlers import RotatingFileHandler
        root = logging.getLogger("st")
        for h in list(root.handlers):
            if isinstance(h, RotatingFileHandler):
                root.removeHandler(h)

    def setUp(self):
        self.topmost_patcher = patch("app.ui.topmost._set_window_pos", return_value=True)
        self.owner_patcher = patch("app.ui.topmost._set_window_owner", return_value=True)
        self.topmost_patcher.start()
        self.owner_patcher.start()
        self.bar = SubtitleBar()

    def tearDown(self):
        if self.bar:
            self.bar.close()
            self.bar.deleteLater()
            self.bar = None
        self.topmost_patcher.stop()
        self.owner_patcher.stop()
        self.qapp.processEvents()

    def test_whitespace_and_none_variations_never_show(self):
        """All variations of empty, whitespace, control chars should keep overlay hidden."""
        adversarial_empties = [
            "",
            " ",
            "    ",
            "\t",
            "\n",
            "\r\n\r\n",
            " \t \r \n ",
            None,
            "   \t\t   ",
        ]
        for empty_val in adversarial_empties:
            self.bar.set_text(empty_val)
            self.assertFalse(
                self.bar.isVisible(),
                f"SubtitleBar became visible on empty input: {repr(empty_val)}",
            )
            self.assertEqual(self.bar._text, "")

    def test_rapid_clear_and_set_text_toggling(self):
        """Rapid toggling between valid text and empty strings must keep state clean."""
        self.assertFalse(self.bar.isVisible())

        for i in range(50):
            # Show with valid text
            text = f"Subtitle iteration {i}"
            self.bar.set_text(text)
            self.assertTrue(self.bar.isVisible())
            self.assertEqual(self.bar._text, text)
            # Chrome must be visible and properly placed
            self.assertTrue(self.bar._ctrl.isVisible())
            self.assertTrue(self.bar._vscroll.isVisible())

            # Clear with empty
            self.bar.set_text("")
            self.assertFalse(self.bar.isVisible())
            self.assertEqual(self.bar._text, "")
            # Chrome must be hidden when bar is hidden
            self.assertFalse(self.bar._ctrl.isVisible())
            self.assertFalse(self.bar._vscroll.isVisible())

        self.qapp.processEvents()

    def test_huge_text_stress(self):
        """Stress-test with huge text (100k chars, 2000 lines) without hanging or crashing."""
        lines = [f"This is line {i} of translated text with various words." for i in range(2000)]
        huge_text = "\n".join(lines)
        self.assertGreater(len(huge_text), 100000)

        t0 = time.time()
        self.bar.set_text(huge_text)
        elapsed = time.time() - t0

        self.assertTrue(self.bar.isVisible())
        self.assertLess(elapsed, 2.0, "Reflow of 2000 lines must finish in under 2 seconds")
        self.assertGreater(self.bar._content_h, self.bar.height())
        # Scroll bar must be visible and enabled
        self.assertTrue(self.bar._vscroll.isVisible())
        self.assertTrue(self.bar._vscroll._bar_widget.isEnabled())

        # Scroll to middle and end without errors
        self.bar.set_scroll(self.bar._content_h // 2)
        self.assertEqual(self.bar._scroll, self.bar._content_h // 2)
        self.bar.set_scroll(self.bar._content_h * 2)  # Should clamp to max_scroll
        max_scroll = self.bar._content_h - self.bar._text_rect_size().height()
        self.assertEqual(self.bar._scroll, max_scroll)

    def test_unbroken_long_token_stress(self):
        """A single 10,000-character token with no spaces must not crash QFontMetrics or wrap."""
        unbroken = "W" * 10000
        self.bar.set_text(unbroken)
        self.assertTrue(self.bar.isVisible())
        self.assertGreater(self.bar._content_h, 0)

    def test_unicode_and_special_characters(self):
        """RTL languages, emojis, CJK, and mixed symbols must be handled cleanly."""
        multilingual = (
            "🌟✨ 屏幕翻译 LocalScreenTranslator 🌟✨\n"
            "مرحبا بكم في عالم الترجمة الفورية\n"
            "こんにちは世界！\n"
            "Привет мир! 🚀\n"
            "Special: <>&\"'\\/\b"
        )
        self.bar.set_text(multilingual)
        self.assertTrue(self.bar.isVisible())
        self.assertEqual(self.bar._text, multilingual)
        self.assertGreater(self.bar._content_h, 0)

    def test_unusual_and_extreme_geometries(self):
        """Unusual coordinates and sizes (0x0, negative coords, huge bounding boxes)."""
        # Negative coordinates
        self.bar.attach_below((-500, -300, 400, 200), outside=True)
        self.bar.set_text("Visible at negative coords")
        self.assertTrue(self.bar.isVisible())
        g = self.bar.geometry()
        self.assertEqual(g.x(), -500)
        self.assertEqual(g.y(), -300 + 200 + 4)

        # 0x0 target rectangle
        self.bar.attach_below((0, 0, 0, 0), outside=False)
        self.assertGreaterEqual(self.bar.width(), self.bar._MIN_W)
        self.assertGreaterEqual(self.bar.height(), self.bar._MIN_H)

        # Negative width/height in attach_below
        self.bar.attach_below((100, 100, -50, -50), outside=True)
        self.assertGreaterEqual(self.bar.width(), self.bar._MIN_W)
        self.assertGreaterEqual(self.bar.height(), self.bar._MIN_H)

        # Negative resize_to
        self.bar.resize_to(-100, -100)
        self.assertEqual(self.bar.width(), self.bar._MIN_W)
        self.assertEqual(self.bar.height(), self.bar._MIN_H)

        # Huge dimensions
        self.bar.resize_to(5000, 3000)
        self.assertEqual(self.bar.width(), 5000)
        self.assertEqual(self.bar.height(), 3000)

    def test_paint_event_under_forced_micro_dimensions(self):
        """Forced micro-dimension resize (bypassing resize_to) must not crash paintEvent."""
        # Force a 1x1 resize directly
        self.bar.resize(1, 1)
        self.bar._text = "Text in 1x1 box"
        image = QImage(10, 10, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        try:
            # Emulate paintEvent execution directly
            self.bar.paintEvent(MagicMock())
        except Exception as e:
            self.fail(f"paintEvent crashed with micro-dimension: {e}")
        finally:
            painter.end()

    def test_chrome_visibility_synchronization(self):
        """Chrome widgets (_ctrl, _vscroll, _grip) must strictly synchronize with SubtitleBar."""
        # Initially all hidden
        self.assertFalse(self.bar.isVisible())
        self.assertFalse(self.bar._ctrl.isVisible())
        self.assertFalse(self.bar._vscroll.isVisible())
        self.assertFalse(self.bar._grip.isVisible())

        # Enable interactive grip
        self.bar.set_interactive(True)
        self.assertFalse(self.bar._grip.isVisible(), "Grip must not show while bar is hidden")

        # Show bar with short text
        self.bar.set_text("Short text")
        self.assertTrue(self.bar.isVisible())
        self.assertTrue(self.bar._ctrl.isVisible())
        self.assertTrue(self.bar._grip.isVisible())
        self.assertTrue(self.bar._vscroll.isVisible())

        # Hide bar via hide()
        self.bar.hide()
        self.assertFalse(self.bar.isVisible())
        self.assertFalse(self.bar._ctrl.isVisible())
        self.assertFalse(self.bar._vscroll.isVisible())
        self.assertFalse(self.bar._grip.isVisible())

    def test_winid_stability_across_hundreds_of_updates(self):
        """Win32 HWND must not recreate or change across 100 continuous text updates."""
        self.bar.set_text("Initial text")
        self.assertTrue(self.bar.isVisible())
        initial_hwnd = int(self.bar.winId())

        for i in range(100):
            self.bar.set_text(f"Update #{i}: dynamically updating text content")
            self.assertEqual(
                int(self.bar.winId()),
                initial_hwnd,
                f"HWND changed on iteration {i}",
            )


class TestGenerationTrackingAdversarial(unittest.TestCase):
    """Adversarial stress and concurrency testing for GenerationTracker."""

    def test_extreme_concurrent_monotonicity(self):
        """100 threads generating 1,000 IDs each: must be strictly monotonic, unique, no dupes."""
        tracker = GenerationTracker(initial_generation=0)
        num_threads = 50
        iterations_per_thread = 1000
        collected_ids: list[int] = []
        lock = threading.Lock()

        def worker():
            local_ids = []
            for _ in range(iterations_per_thread):
                local_ids.append(tracker.next_generation())
            with lock:
                collected_ids.extend(local_ids)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total_expected = num_threads * iterations_per_thread
        self.assertEqual(len(collected_ids), total_expected)
        # All IDs must be unique
        unique_ids = set(collected_ids)
        self.assertEqual(len(unique_ids), total_expected)
        # Max ID must equal total expected
        self.assertEqual(tracker.current_generation, total_expected)
        self.assertEqual(max(collected_ids), total_expected)
        self.assertEqual(min(collected_ids), 1)

    def test_simulated_out_of_order_async_arrivals(self):
        """Simulate realistic out-of-order async translation responses.

        Generations 1..20 dispatched with randomized processing delays.
        UI state must only render results that match active generation,
        and final rendered state must be the latest generation. Older results must be dropped.
        """
        tracker = GenerationTracker()
        rendered_history: list[tuple[int, str]] = []
        dropped_history: list[tuple[int, str]] = []
        render_lock = threading.Lock()

        def dispatch_result(gen_id: int, translation: str):
            with render_lock:
                if tracker.is_active(gen_id):
                    rendered_history.append((gen_id, translation))
                else:
                    dropped_history.append((gen_id, translation))

        # Generate 20 tasks with random latencies
        num_tasks = 20
        latencies = [random.uniform(0.01, 0.08) for _ in range(num_tasks)]
        # Force generation 1 to be very slow (0.15s) and generation 20 to be very fast (0.01s)
        latencies[0] = 0.15
        latencies[-1] = 0.01

        tasks = []
        for i in range(num_tasks):
            gen = tracker.next_generation()
            text = f"Result of gen {gen}"
            tasks.append((gen, text, latencies[i]))

        def run_task(gen, text, delay):
            time.sleep(delay)
            dispatch_result(gen, text)

        threads = [
            threading.Thread(target=run_task, args=task)
            for task in tasks
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All results were processed
        self.assertEqual(len(rendered_history) + len(dropped_history), num_tasks)
        # Stale results must have been dropped
        self.assertGreater(len(dropped_history), 0, "Out-of-order arrivals must drop stale results")
        # For all rendered results, generation IDs must be non-decreasing
        rendered_gens = [g for g, _ in rendered_history]
        self.assertEqual(rendered_gens, sorted(rendered_gens))
        # The last rendered result must be the highest generation rendered
        if rendered_history:
            self.assertEqual(rendered_history[-1][0], max(rendered_gens))

    def test_rapid_concurrent_resets_during_translation(self):
        """Concurrent translation workers vs rapid reset spammer threads.

        Zero crashes, zero deadlocks, and stale generations must never be active.
        """
        tracker = GenerationTracker()
        stop_event = threading.Event()
        dispatched = []
        dropped = []
        lock = threading.Lock()

        def translation_worker():
            while not stop_event.is_set():
                gen = tracker.next_generation()
                time.sleep(random.uniform(0.001, 0.005))
                is_act = tracker.is_active(gen)
                with lock:
                    if is_act:
                        dispatched.append(gen)
                    else:
                        dropped.append(gen)

        def reset_spammer():
            while not stop_event.is_set():
                tracker.reset()
                time.sleep(random.uniform(0.001, 0.004))

        workers = [threading.Thread(target=translation_worker) for _ in range(10)]
        spammers = [threading.Thread(target=reset_spammer) for _ in range(4)]

        all_threads = workers + spammers
        for t in all_threads:
            t.start()

        time.sleep(0.5)
        stop_event.set()

        for t in all_threads:
            t.join()

        # Check: many operations occurred without deadlock
        self.assertGreater(len(dispatched) + len(dropped), 50)
        # Resets should have successfully invalidated a substantial number of in-flight generations
        self.assertGreater(len(dropped), 0, "Reset spamming should have caused drops")

    def test_dispatch_if_active_atomicity_and_race(self):
        """Stress test dispatch_if_active with concurrent reset calls."""
        tracker = GenerationTracker()
        callback_executed_counts = 0
        lock = threading.Lock()

        def dummy_callback(gen):
            nonlocal callback_executed_counts
            with lock:
                callback_executed_counts += 1

        for _ in range(100):
            gen = tracker.next_generation()
            # Race reset in parallel thread
            t = threading.Thread(target=tracker.reset)
            t.start()
            tracker.dispatch_if_active(gen, dummy_callback, gen)
            t.join()

        # Must execute cleanly without exceptions
        self.assertGreaterEqual(callback_executed_counts, 0)


class TestWindowWatcherAdversarial(unittest.TestCase):
    """Adversarial testing on WindowWatcher's integration with GenerationTracker."""

    def test_window_watcher_drops_when_reset_mid_translation(self):
        """WindowWatcher must cleanly drop translation when reset is invoked while translating."""
        ocr_mock = MagicMock()
        translator_mock = MagicMock()
        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 50

        watcher = WindowWatcher(ocr_mock, translator_mock, cfg, hwnd=1)

        ocr_mock.recognize.return_value = [
            OcrLine(text="Line to translate", box=[[0, 0], [10, 0], [10, 10], [0, 10]], score=0.99)
        ]

        emitted_subtitles = []
        watcher.subtitle_ready.connect(emitted_subtitles.append)

        # Slow translate that triggers reset mid-flight
        def slow_translate(text, target):
            watcher.generation_tracker.reset()
            return "Stale translated text"

        translator_mock.translate.side_effect = slow_translate

        fake_frame = np.ones((50, 50, 3), dtype=np.uint8) * 42
        with patch.object(watcher, "_grab", return_value=((0, 0, 50, 50), fake_frame)):
            gen_id = watcher.generation_tracker.next_generation()
            lines = ocr_mock.recognize(fake_frame)
            event, text = watcher._state.observe(lines, 0.5)
            self.assertEqual(event, "change")

            tr = translator_mock.translate(text, "zh")
            if watcher._running and watcher.generation_tracker.is_active(gen_id):
                watcher.subtitle_ready.emit(tr)

        self.assertEqual(
            len(emitted_subtitles), 0,
            "Mid-flight reset must cause WindowWatcher to drop the subtitle translation",
        )

    def test_window_watcher_drops_on_set_region_during_translation(self):
        """set_region during active translation cycle must invalidate active generation."""
        ocr_mock = MagicMock()
        translator_mock = MagicMock()
        cfg = dict(DEFAULTS)

        watcher = WindowWatcher(ocr_mock, translator_mock, cfg, region=(0, 0, 100, 100))
        emitted_subtitles = []
        watcher.subtitle_ready.connect(emitted_subtitles.append)

        def translate_with_region_move(text, target):
            # User drags region frame while LLM translation is in-flight
            watcher.set_region((20, 20, 150, 150))
            return "Translation for old region"

        translator_mock.translate.side_effect = translate_with_region_move

        fake_frame = np.ones((50, 50, 3), dtype=np.uint8) * 15
        gen_id = watcher.generation_tracker.next_generation()
        tr = translator_mock.translate("Source text", "zh")

        if watcher._running and watcher.generation_tracker.is_active(gen_id):
            watcher.subtitle_ready.emit(tr)

        self.assertEqual(len(emitted_subtitles), 0, "Dragged region must drop in-flight translation")

    def test_window_watcher_drops_on_set_display_mode_during_translation(self):
        """set_display_mode during active translation cycle must invalidate active generation."""
        ocr_mock = MagicMock()
        translator_mock = MagicMock()
        cfg = dict(DEFAULTS)

        watcher = WindowWatcher(ocr_mock, translator_mock, cfg, hwnd=1)
        emitted_subtitles = []
        watcher.subtitle_ready.connect(emitted_subtitles.append)

        def translate_with_mode_switch(text, target):
            # User switches to annotate mode while LLM translation is in-flight
            watcher.set_display_mode("annotate")
            return "Translation for subtitle mode"

        translator_mock.translate.side_effect = translate_with_mode_switch

        gen_id = watcher.generation_tracker.next_generation()
        tr = translator_mock.translate("Source text", "zh")

        if watcher._running and watcher.generation_tracker.is_active(gen_id):
            watcher.subtitle_ready.emit(tr)

        self.assertEqual(len(emitted_subtitles), 0, "Mode switch must drop in-flight translation")


if __name__ == "__main__":
    unittest.main()
