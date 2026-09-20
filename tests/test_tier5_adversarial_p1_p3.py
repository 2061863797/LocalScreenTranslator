# -*- coding: utf-8 -*-
"""Tier 5 Adversarial Coverage Hardening & Verification Suite (Phases 1-3).

Rigorous white-box stress testing, latency benchmarking, and invariant verification:
- Phase 1 (PR 1 Zero-Flicker Subtitle Overlay, PR 2 Generation Tracking):
  * SubtitleBar hidden at startup; empty/whitespace never causes show.
  * Pre-layout calculation settled before DWM show; single HWND validation.
  * GenerationTracker monotonic IDs under concurrency; out-of-order response dropping in ResultManager.
- Phase 2 (PR 3 FrameChangeDetector, PR 4 Adaptive Polling, PR 5 ROI OCR & Fallback, PR 6 OCR Stabilizer):
  * FrameChangeDetector latency benchmark on 1080p (OpenCV & NumPy fallback < 3ms).
  * Noise filter, cursor filter, static frame skip >= 95% during idle testing.
  * AdaptivePolling state machine transitions (ACTIVE 120ms, WARM 250ms, IDLE 500ms, DEEP_IDLE 800ms).
  * ROI OCR fallback on area ratio > 0.6 and health check interval.
  * OcrStabilizer character flip jitter suppression, debounce window, and fast promotion.
- Phase 3 (PR 7 Incremental Translation, PR 8 Persistent 2-Tier Caching):
  * TranslationCache L1 hit (< 1ms), L2 SQLite hit (< 10ms) with zero LLM calls.
  * Composite SHA256 cache key invalidation (source, target, model_id, prompt_version) and NFC normalization.
  * 50k LRU eviction, access count update, and last_accessed_at touch survival.
  * SubtitleIncrementalTranslator line diffs, partial cache reuse, and strict line ordering.
"""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import os
import sqlite3
import tempfile
import threading
import time
import unicodedata
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication

from app.adaptive_polling import AdaptivePollingController, PollingState
import app.frame_detector as frame_detector_module
from app.frame_detector import FrameChangeDetector, FrameDiffResult
from app.ocr_engine import OcrLine
from app.ocr_service import OcrService
from app.ocr_stabilizer import OcrStabilizer, levenshtein_distance
from app.pipelines import GenerationTracker
from app.result_manager import ResultManager
from app.storage import Storage, MAX_CACHE_ENTRIES
from app.translation_cache import TranslationCache
from app.translation_manager import SubtitleIncrementalTranslator
from app.ui.overlays import SubtitleBar


# =========================================================================
# PHASE 1 TESTS: Zero-Flicker Subtitle Overlay & Generation Tracking
# =========================================================================

class TestPhase1Adversarial(unittest.TestCase):
    """Adversarial stress and verification for Phase 1 (PR 1 & PR 2)."""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bar = SubtitleBar()
        self.tracker = GenerationTracker()

    def tearDown(self):
        if self.bar:
            self.bar.close()
            self.bar.deleteLater()
            self.bar = None
        self.qapp.processEvents()

    def test_subtitle_bar_hidden_at_startup_and_whitespace_rejection(self):
        """Verify SubtitleBar is strictly hidden at startup and empty/whitespace never reveals it."""
        self.assertFalse(self.bar.isVisible(), "SubtitleBar must start invisible")
        self.assertFalse(self.bar._layout_calculated_before_show)

        # Feed empty, whitespace, and None
        for empty_val in ["", "   ", "\n\t  \r", None]:
            self.bar.set_text(empty_val)
            self.assertFalse(self.bar.isVisible(), f"SubtitleBar showed on empty input: {empty_val!r}")

        # Manipulations while hidden must not accidentally show the bar
        self.bar.attach_below((100, 100, 800, 600))
        self.assertFalse(self.bar.isVisible(), "attach_below should not show window before text")
        self.bar.move_to(200, 300)
        self.assertFalse(self.bar.isVisible(), "move_to should not show window before text")
        self.bar.resize_to(500, 200)
        self.assertFalse(self.bar.isVisible(), "resize_to should not show window before text")

    def test_subtitle_bar_pre_layout_and_first_valid_translation(self):
        """Verify pre-layout calculation completes BEFORE show() on first valid translation."""
        layout_happened_before_show = False

        original_show = self.bar.show

        def hooked_show():
            nonlocal layout_happened_before_show
            # At the moment show() is called, prepare_layout must have already run
            if self.bar._layout_calculated_before_show and self.bar._content_h > 0:
                layout_happened_before_show = True
            original_show()

        self.bar.show = hooked_show

        # Send first valid translation
        self.bar.set_text("First valid translation text appearing on screen")
        self.assertTrue(layout_happened_before_show, "Layout calculation must be settled before show()")
        self.assertTrue(self.bar.isVisible(), "SubtitleBar must be visible after valid text")

    def test_subtitle_bar_single_hwnd_consolidation(self):
        """Verify SubtitleBar, controls, scrollbar, and grip share exactly one native top-level HWND."""
        self.assertTrue(self.bar.is_single_hwnd(), "SubtitleBar must maintain a single top-level HWND")
        self.assertTrue(self.bar.isWindow(), "SubtitleBar itself must be a top-level window")
        self.assertFalse(self.bar._ctrl.isWindow(), "_ctrl must not be a top-level window")
        self.assertFalse(self.bar._vscroll.isWindow(), "_vscroll must not be a top-level window")
        self.assertFalse(self.bar._grip.isWindow(), "_grip must not be a top-level window")
        self.assertIs(self.bar._ctrl.parent(), self.bar)
        self.assertIs(self.bar._vscroll.parent(), self.bar)
        self.assertIs(self.bar._grip.parent(), self.bar)

        # Layer widgets list must contain only self (single HWND)
        self.assertEqual(self.bar.layer_widgets(), [self.bar])

    def test_generation_tracker_monotonicity_under_concurrency(self):
        """Verify GenerationTracker generates strictly monotonic, collision-free IDs across threads."""
        num_threads = 16
        ops_per_thread = 500
        all_ids: list[int] = []
        lock = threading.Lock()

        def worker():
            local_ids = []
            for _ in range(ops_per_thread):
                local_ids.append(self.tracker.next_generation())
            with lock:
                all_ids.extend(local_ids)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total_expected = num_threads * ops_per_thread
        self.assertEqual(len(all_ids), total_expected)
        self.assertEqual(len(set(all_ids)), total_expected, "Collision detected in generation IDs")
        self.assertEqual(min(all_ids), 1)
        self.assertEqual(max(all_ids), total_expected)
        self.assertEqual(self.tracker.current_generation, total_expected)

    def test_result_manager_drops_out_of_order_stale_responses(self):
        """Verify ResultManager drops older responses (Result 1 arriving after Result 2)."""
        rm = ResultManager(generation_tracker=self.tracker)
        emitted_subtitles: list[str] = []
        rm.subtitle_ready.connect(lambda s: emitted_subtitles.append(s))

        # Start Gen 1
        gen1 = self.tracker.next_generation()
        self.assertEqual(gen1, 1)

        # Start Gen 2
        gen2 = self.tracker.next_generation()
        self.assertEqual(gen2, 2)

        # Result 2 arrives FIRST
        res2_ok = rm.dispatch_subtitle("Translation Gen 2 (Fresh)", gen_id=gen2)
        self.assertTrue(res2_ok, "Fresh Gen 2 should be dispatched")
        self.assertEqual(emitted_subtitles, ["Translation Gen 2 (Fresh)"])

        # Result 1 arrives LATER (Out of order)
        res1_ok = rm.dispatch_subtitle("Translation Gen 1 (Stale)", gen_id=gen1)
        self.assertFalse(res1_ok, "Stale Gen 1 must be dropped")
        self.assertEqual(rm.dropped_stale_count, 1)
        # UI must still only have Result 2!
        self.assertEqual(emitted_subtitles, ["Translation Gen 2 (Fresh)"])
        self.assertEqual(rm.latest_dispatched, "Translation Gen 2 (Fresh)")


# =========================================================================
# PHASE 2 TESTS: Visual Change, Adaptive Polling, ROI OCR & Stabilizer
# =========================================================================

class TestPhase2Adversarial(unittest.TestCase):
    """Adversarial stress and verification for Phase 2 (PR 3, 4, 5, 6)."""

    def setUp(self):
        self.detector = FrameChangeDetector()
        self.polling = AdaptivePollingController()
        self.stabilizer = OcrStabilizer(debounce_frames=2, max_jitter_dist=2)

    def test_frame_change_detector_1080p_latency_benchmark(self):
        """Benchmark FrameChangeDetector latency strictly < 3ms on 1080p frames."""
        h, w = 1080, 1920
        np.random.seed(123)
        frame1 = np.random.randint(0, 255, (h, w, 3), dtype=np.uint8)
        frame2 = frame1.copy()
        # Create a substantive change in a region (e.g. 200x200 patch)
        frame2[300:500, 400:600] = (255 - frame2[300:500, 400:600])

        # Warmup
        for _ in range(5):
            self.detector.detect(frame1, frame2)

        # Test both OpenCV path (if available) and NumPy fallback
        paths_to_test = []
        if frame_detector_module.cv2 is not None:
            paths_to_test.append(("OpenCV", frame_detector_module.cv2))
        paths_to_test.append(("Pure NumPy", None))

        for name, cv_lib in paths_to_test:
            with patch.object(frame_detector_module, "cv2", cv_lib):
                latencies = []
                for _ in range(50):
                    t0 = time.perf_counter()
                    res = self.detector.detect(frame1, frame2)
                    t1 = time.perf_counter()
                    latencies.append((t1 - t0) * 1000.0)

                avg_ms = sum(latencies) / len(latencies)
                p95_ms = float(np.percentile(latencies, 95))
                max_ms = max(latencies)

                print(f"\n[BENCHMARK FrameChangeDetector 1080p ({name})] Avg: {avg_ms:.3f}ms, P95: {p95_ms:.3f}ms, Max: {max_ms:.3f}ms")
                self.assertLess(avg_ms, 3.0, f"{name} average latency exceeded 3ms target: {avg_ms:.3f}ms")
                self.assertLess(p95_ms, 3.0, f"{name} P95 latency exceeded 3ms target: {p95_ms:.3f}ms")
                self.assertTrue(res.has_changed)
                self.assertIsNotNone(res.roi_box)

    def test_noise_filter_and_cursor_filter_rejection(self):
        """Verify noise filter and cursor blinking are rejected without triggering OCR."""
        h, w = 1080, 1920
        base = np.zeros((h, w, 3), dtype=np.uint8)
        base.fill(128)

        # 1. Micro noise test (uniform noise with diff <= 16, or small pixel count)
        noisy = base.copy()
        # Add random noise of magnitude +-10 (threshold is 16)
        noise = np.random.randint(-10, 10, (h, w, 3), dtype=np.int16)
        noisy = np.clip(noisy.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        diff_noise = self.detector.detect(base, noisy)
        self.assertFalse(diff_noise.has_changed, "Micro noise should be filtered out by pixel_threshold")
        self.assertIsNone(diff_noise.roi_box)

        # 2. Cursor filter test: 2x16 cursor line inverted (32 pixels, less than min_changed_pixels 40)
        cursor_frame = base.copy()
        cursor_frame[100:116, 200:202] = 255
        diff_cursor = self.detector.detect(base, cursor_frame)
        self.assertFalse(diff_cursor.has_changed, "Cursor blink (<40 px) must be filtered out")
        self.assertIsNone(diff_cursor.roi_box)

    def test_static_frame_skip_rate_ge_95_percent(self):
        """Verify static frame skip >= 95% during idle testing."""
        h, w = 720, 1280
        base = np.ones((h, w, 3), dtype=np.uint8) * 100
        frames_tested = 200
        skipped = 0

        prev = base
        for i in range(frames_tested):
            # 98% completely identical frames, 2% small cursor blinks
            if i % 50 == 0:
                curr = base.copy()
                curr[50:60, 50:52] = 255  # tiny cursor
            else:
                curr = base.copy()

            res = self.detector.detect(prev, curr)
            if not res.has_changed:
                skipped += 1
            prev = curr

        skip_rate = skipped / frames_tested
        print(f"\n[IDLE TEST] Frames: {frames_tested}, Skipped: {skipped}, Skip Rate: {skip_rate * 100:.1f}%")
        self.assertGreaterEqual(skip_rate, 0.95, f"Static frame skip rate {skip_rate} fell below 95%")

    def test_adaptive_polling_state_machine_transitions(self):
        """Verify AdaptivePolling transitions: ACTIVE(120ms)->WARM(250ms)->IDLE(500ms)->DEEP_IDLE(800ms)."""
        ctrl = self.polling
        self.assertEqual(ctrl.state, PollingState.ACTIVE)
        self.assertAlmostEqual(ctrl.current_interval, 0.12)

        # Frame 1: static (count=1, below warm_threshold 2) -> ACTIVE 120ms
        int1 = ctrl.on_frame(has_changed=False)
        self.assertEqual(ctrl.state, PollingState.ACTIVE)
        self.assertAlmostEqual(int1, 0.12)

        # Frame 2: static (count=2, reaches warm_threshold 2) -> WARM 250ms
        int2 = ctrl.on_frame(has_changed=False)
        self.assertEqual(ctrl.state, PollingState.WARM)
        self.assertAlmostEqual(int2, 0.25)

        # Frames 3, 4: static -> WARM 250ms
        ctrl.on_frame(has_changed=False)
        int4 = ctrl.on_frame(has_changed=False)
        self.assertEqual(ctrl.state, PollingState.WARM)
        self.assertAlmostEqual(int4, 0.25)

        # Frame 5: static (count=5, reaches idle_threshold 5) -> IDLE 500ms
        int5 = ctrl.on_frame(has_changed=False)
        self.assertEqual(ctrl.state, PollingState.IDLE)
        self.assertAlmostEqual(int5, 0.50)

        # Progress to 19: still IDLE
        for _ in range(14):
            ctrl.on_frame(has_changed=False)
        self.assertEqual(ctrl.state, PollingState.IDLE)

        # Frame 20: static (count=20, reaches deep_idle_threshold 20) -> DEEP_IDLE 800ms
        int20 = ctrl.on_frame(has_changed=False)
        self.assertEqual(ctrl.state, PollingState.DEEP_IDLE)
        self.assertAlmostEqual(int20, 0.80)

        # Immediate wakeup upon frame change -> ACTIVE 120ms
        int_wakeup = ctrl.on_frame(has_changed=True)
        self.assertEqual(ctrl.state, PollingState.ACTIVE)
        self.assertAlmostEqual(int_wakeup, 0.12)
        self.assertEqual(ctrl.static_count, 0)

    def test_roi_ocr_fallback_ratio_and_health_check(self):
        """Verify ROI OCR fallback on area ratio > 0.6 and health check interval."""
        mock_calls: list[tuple[int, int]] = []  # records (crop_w, crop_h)

        def mock_ocr(img: np.ndarray):
            h, w = img.shape[:2]
            mock_calls.append((w, h))
            return [OcrLine(text="Hello", score=0.95, box=(10, 10, 50, 30))]

        service = OcrService(
            mock_ocr_fn=mock_ocr,
            padding=10,
            max_roi_ratio=0.6,
            health_check_interval=10,
        )

        frame = np.zeros((1000, 1000, 3), dtype=np.uint8)

        # 1. Localized small ROI (100x100 box, padded to 120x120 -> area 14,400 / 1,000,000 = 0.0144 < 0.6)
        mock_calls.clear()
        lines = service.recognize_frame(frame, roi_box=(100, 100, 200, 200))
        self.assertFalse(service.last_was_fallback, "Small ROI should NOT trigger fallback")
        self.assertEqual(mock_calls[-1], (120, 120))  # Cropped image sent
        # Coordinate remapping: box was (10, 10, 50, 30) relative to crop (90, 90) -> (100, 100, 140, 120)
        self.assertEqual(lines[0].box, (100, 100, 140, 120))

        # 2. Large ROI (800x800 box in 1000x1000 frame -> area ratio ~ 0.64 > 0.6)
        mock_calls.clear()
        _ = service.recognize_frame(frame, roi_box=(100, 100, 900, 900))
        self.assertTrue(service.last_was_fallback, "Large ROI ratio > 0.6 MUST trigger fallback to full frame")
        self.assertEqual(mock_calls[-1], (1000, 1000))  # Full image sent

        # 3. Periodic health check fallback (interval = 10)
        service.frames_since_full_ocr = 9
        mock_calls.clear()
        _ = service.recognize_frame(frame, roi_box=(100, 100, 200, 200))
        self.assertTrue(service.last_was_fallback, "Health check frame MUST trigger fallback to full frame")
        self.assertEqual(mock_calls[-1], (1000, 1000))
        self.assertEqual(service.frames_since_full_ocr, 0)

    def test_ocr_stabilizer_character_flip_jitter_suppression(self):
        """Verify OcrStabilizer suppresses 1-2 char optical jitter and promotes stable candidate."""
        # Initial frame establishes baseline
        changed, lines = self.stabilizer.process(["Hello World"])
        self.assertTrue(changed)
        self.assertEqual([line if isinstance(line, str) else line.text for line in lines], ["Hello World"])

        # Frame 2: single char flip "Hello Wor1d" (Levenshtein dist = 1 <= 2)
        changed2, lines2 = self.stabilizer.process(["Hello Wor1d"])
        self.assertFalse(changed2, "Single character flip jitter should be suppressed")
        self.assertEqual([line if isinstance(line, str) else line.text for line in lines2], ["Hello World"])

        # Frame 3: same candidate repeats (debounce_frames = 2 reached)
        changed3, lines3 = self.stabilizer.process(["Hello Wor1d"])
        self.assertTrue(changed3, "Candidate repeating across debounce frames should be promoted")
        self.assertEqual([line if isinstance(line, str) else line.text for line in lines3], ["Hello Wor1d"])

        # Frame 4: completely different text (dist >> 2) -> immediate substantive change
        changed4, lines4 = self.stabilizer.process(["Completely different text"])
        self.assertTrue(changed4, "Substantive change should be immediately accepted")
        self.assertEqual([line if isinstance(line, str) else line.text for line in lines4], ["Completely different text"])


# =========================================================================
# PHASE 3 TESTS: Incremental Translation & Persistent 2-Tier Caching
# =========================================================================

class TestPhase3Adversarial(unittest.TestCase):
    """Adversarial stress and verification for Phase 3 (PR 7 & PR 8)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "tier5_cache.db"
        self.cache = TranslationCache(db_path=self.db_path, l1_capacity=1000, l2_max_entries=50000)

    def tearDown(self):
        if self.cache:
            self.cache.close()
        self.tmp_dir.cleanup()

    def test_translation_cache_l1_and_l2_latency_benchmark(self):
        """Benchmark L1 hit (< 1ms) and L2 SQLite hit (< 10ms) without calling llama-server."""
        source = "Fast Benchmark Test String"
        target = "zh"
        model_id = "test_model"
        pver = "v1"
        expected_trans = "快速基准测试字符串"

        self.cache.put(source, target, model_id, pver, expected_trans)

        # 1. Benchmark L1 hit latency over 5,000 queries
        l1_times = []
        for _ in range(5000):
            t0 = time.perf_counter()
            res = self.cache.get(source, target, model_id, pver)
            t1 = time.perf_counter()
            l1_times.append((t1 - t0) * 1000.0)

        l1_avg_ms = sum(l1_times) / len(l1_times)
        l1_p95_ms = float(np.percentile(l1_times, 95))
        print(f"\n[BENCHMARK L1 Cache Hit] Avg: {l1_avg_ms:.5f}ms, P95: {l1_p95_ms:.5f}ms")
        self.assertLess(l1_avg_ms, 1.0, f"L1 hit avg latency exceeded 1ms: {l1_avg_ms:.5f}ms")
        self.assertLess(l1_p95_ms, 1.0, f"L1 hit P95 latency exceeded 1ms: {l1_p95_ms:.5f}ms")
        self.assertEqual(res, expected_trans)

        # 2. Benchmark L2 SQLite hit latency: clear L1 to force disk read
        l2_times = []
        for _ in range(500):
            self.cache._l1.clear()  # Evict from L1
            t0 = time.perf_counter()
            res = self.cache.get(source, target, model_id, pver)
            t1 = time.perf_counter()
            l2_times.append((t1 - t0) * 1000.0)

        l2_avg_ms = sum(l2_times) / len(l2_times)
        l2_p95_ms = float(np.percentile(l2_times, 95))
        print(f"\n[BENCHMARK L2 SQLite Hit] Avg: {l2_avg_ms:.5f}ms, P95: {l2_p95_ms:.5f}ms")
        self.assertLess(l2_avg_ms, 10.0, f"L2 hit avg latency exceeded 10ms: {l2_avg_ms:.5f}ms")
        self.assertLess(l2_p95_ms, 10.0, f"L2 hit P95 latency exceeded 10ms: {l2_p95_ms:.5f}ms")
        self.assertEqual(res, expected_trans)

    def test_composite_key_sha256_invalidation_and_nfc(self):
        """Verify composite key SHA256 invalidation when any parameter changes, plus NFC normalization."""
        source = "Unique Test Source"
        target = "zh"
        model_id = "model_A"
        pver = "v1"
        trans = "翻译A"

        self.cache.put(source, target, model_id, pver, trans)

        # Same query hits
        self.assertEqual(self.cache.get(source, target, model_id, pver), trans)

        # Different target -> miss
        self.assertIsNone(self.cache.get(source, "ja", model_id, pver))

        # Different model_id -> miss (model switch invalidation)
        self.assertIsNone(self.cache.get(source, target, "model_B", pver))

        # Different prompt_version -> miss (prompt update invalidation)
        self.assertIsNone(self.cache.get(source, target, model_id, "v2"))

        # Unicode NFC normalization: accented characters in NFC vs NFD
        source_nfc = unicodedata.normalize("NFC", "café résumé")
        source_nfd = unicodedata.normalize("NFD", "café résumé")
        self.cache.put(source_nfc, "zh", model_id, pver, "咖啡简历")
        # Query with NFD representation must still hit!
        self.assertEqual(
            self.cache.get(source_nfd, "zh", model_id, pver),
            "咖啡简历",
            "NFC normalization failed to match NFD input",
        )

    def test_50k_lru_eviction_and_access_touch_survival(self):
        """Verify 50k LRU eviction preserves cap and touch updates last_accessed_at."""
        # Use small custom capacity for testing eviction logic
        small_cache = TranslationCache(
            db_path=Path(self.tmp_dir.name) / "small_cache.db",
            l1_capacity=50,
            l2_max_entries=100,
        )
        try:
            # Insert 100 entries
            for i in range(100):
                small_cache.put(f"src_{i}", "zh", "m1", "v1", f"trans_{i}")

            # Touch entry 0 by getting it (updates last_accessed_at and access_count)
            time.sleep(0.01)
            t0_val = small_cache.get("src_0", "zh", "m1", "v1")
            self.assertEqual(t0_val, "trans_0")

            # Insert 101st entry, triggering LRU eviction
            small_cache.put("src_101", "zh", "m1", "v1", "trans_101")

            cur = small_cache._conn.execute("SELECT count(*) FROM translation_cache")
            total = cur.fetchone()[0]
            self.assertLessEqual(total, 100, f"Total entries {total} exceeded cap 100")

            # Verify entry 0 survived eviction because it was touched recently!
            self.assertEqual(small_cache.get("src_0", "zh", "m1", "v1"), "trans_0")
            # Verify oldest untouched entry (e.g. src_1) was evicted
            self.assertIsNone(small_cache.get("src_1", "zh", "m1", "v1"))
        finally:
            small_cache.close()

    def test_subtitle_incremental_translator_line_diffs_and_ordering(self):
        """Verify SubtitleIncrementalTranslator translates only modified lines and preserves line order."""
        translated_lines_log: list[list[str]] = []

        def mock_translate_lines(lines: list[str], target: str) -> list[str]:
            translated_lines_log.append(list(lines))
            return [f"[{target}]{line}" for line in lines]

        inc_translator = SubtitleIncrementalTranslator(
            cache=self.cache,
            mock_translate_fn=mock_translate_lines,
            model_id="test_model",
            prompt_version="v1",
        )

        # Cycle 1: 3 lines
        lines_c1 = ["First line of dialogue", "Second line of dialogue", "Third line of dialogue"]
        sub1, used_cache1 = inc_translator.translate_subtitle_lines(lines_c1, "zh")

        self.assertFalse(used_cache1, "First cycle has no cache")
        self.assertEqual(len(translated_lines_log), 1)
        self.assertEqual(translated_lines_log[0], lines_c1)
        expected_sub1 = "[zh]First line of dialogue\n[zh]Second line of dialogue\n[zh]Third line of dialogue"
        self.assertEqual(sub1, expected_sub1)

        # Cycle 2: line 2 changes, lines 1 & 3 stay unchanged!
        lines_c2 = ["First line of dialogue", "MODIFIED second line", "Third line of dialogue"]
        translated_lines_log.clear()
        sub2, used_cache2 = inc_translator.translate_subtitle_lines(lines_c2, "zh")

        self.assertTrue(used_cache2, "Second cycle must reuse cached lines 1 and 3")
        self.assertEqual(len(translated_lines_log), 1)
        # ONLY the modified line was sent to the translator!
        self.assertEqual(translated_lines_log[0], ["MODIFIED second line"])

        # Strict ordering check: line 1 (cached), line 2 (new), line 3 (cached)
        expected_sub2 = "[zh]First line of dialogue\n[zh]MODIFIED second line\n[zh]Third line of dialogue"
        self.assertEqual(sub2, expected_sub2)


if __name__ == "__main__":
    unittest.main()
