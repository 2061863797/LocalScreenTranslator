# -*- coding: utf-8 -*-
"""Empirical stress test suite for Milestone 2:
- PR 3: FrameChangeDetector (latency < 3ms on 100 1080p frames, noise & cursor rejection, non-contiguous / edge cases)
- PR 4: AdaptivePollingController (chaotic arrival patterns, long idles, rapid wakeups, state invariants)
- PR 5: OcrService (coordinate remapping with varied padding, multi-box regions, area ratio 0.6 fallback, health check fallback)
- PR 6: OcrStabilizer (Levenshtein jitter suppression, candidate promotion, unicode & edge cases)
"""

import time
import unittest
from dataclasses import dataclass
from typing import Any
import numpy as np

import app.frame_detector as frame_detector_module
from app.frame_detector import FrameChangeDetector, FrameDiffResult
from app.adaptive_polling import AdaptivePollingController, PollingState
from app.ocr_service import OcrService
from app.ocr_stabilizer import OcrStabilizer, levenshtein_distance
from app.ocr_engine import OcrLine


@dataclass
class CustomDuckLine:
    text: str
    box: tuple[int, int, int, int]
    score: float = 0.95


@dataclass
class CustomConfidenceLine:
    text: str
    box: tuple[int, int, int, int]
    confidence: float = 0.91


class TestMilestone2EmpiricalStress(unittest.TestCase):
    """Rigorous empirical challenge for Milestone 2 implementation."""

    def setUp(self):
        self.detector = FrameChangeDetector()
        self.polling = AdaptivePollingController()
        self.stabilizer = OcrStabilizer()

    # =========================================================================
    # 1. FrameChangeDetector: 100-Frame Latency Benchmark (<3ms avg & P95)
    # =========================================================================

    def test_benchmark_100_frames_opencv_path(self):
        """Benchmark 100 consecutive 1080p frames through OpenCV SIMD path."""
        np.random.seed(42)
        h, w = 1080, 1920

        # Base frame
        base_frame = np.random.randint(40, 220, (h, w, 3), dtype=np.uint8)
        prev = base_frame

        # Warmup
        for _ in range(5):
            _ = self.detector.detect(prev, prev)

        latencies = []
        for i in range(100):
            curr = prev.copy()
            # Simulate dynamic realistic screen updates:
            # 30% of frames have localized subtitle updates
            if i % 3 == 0:
                curr[950:1020, 400:1520] = np.random.randint(200, 255, (70, 1120, 3), dtype=np.uint8)
            # 5% of frames have minor cursor blink
            elif i % 20 == 0:
                curr[200:220, 500:502] = 0
            # Otherwise static

            t0 = time.perf_counter()
            res = self.detector.detect(prev, curr)
            t1 = time.perf_counter()

            latencies.append((t1 - t0) * 1000.0)  # in ms
            prev = curr

        avg_ms = float(np.mean(latencies))
        p50_ms = float(np.percentile(latencies, 50))
        p95_ms = float(np.percentile(latencies, 95))
        p99_ms = float(np.percentile(latencies, 99))
        max_ms = float(np.max(latencies))

        print(
            f"\n[BENCHMARK OpenCV 1080p (100 frames)] "
            f"Avg: {avg_ms:.3f}ms, P50: {p50_ms:.3f}ms, P95: {p95_ms:.3f}ms, P99: {p99_ms:.3f}ms, Max: {max_ms:.3f}ms"
        )

        self.assertLess(avg_ms, 3.0, f"Average latency {avg_ms:.3f}ms violates <3.0ms requirement")
        self.assertLess(p95_ms, 3.0, f"P95 latency {p95_ms:.3f}ms violates <3.0ms requirement")

    def test_benchmark_100_frames_numpy_fallback_path(self):
        """Benchmark 100 consecutive 1080p frames with cv2=None (NumPy fallback)."""
        np.random.seed(42)
        h, w = 1080, 1920

        # Temporarily mock cv2 as None in module
        orig_cv2 = frame_detector_module.cv2
        try:
            frame_detector_module.cv2 = None
            detector_fallback = FrameChangeDetector()

            base_frame = np.random.randint(40, 220, (h, w, 3), dtype=np.uint8)
            prev = base_frame

            # Warmup
            for _ in range(5):
                _ = detector_fallback.detect(prev, prev)

            latencies = []
            for i in range(100):
                curr = prev.copy()
                if i % 3 == 0:
                    curr[950:1020, 400:1520] = np.random.randint(200, 255, (70, 1120, 3), dtype=np.uint8)
                elif i % 20 == 0:
                    curr[200:220, 500:502] = 0

                t0 = time.perf_counter()
                res = detector_fallback.detect(prev, curr)
                t1 = time.perf_counter()

                latencies.append((t1 - t0) * 1000.0)
                prev = curr

            avg_ms = float(np.mean(latencies))
            p95_ms = float(np.percentile(latencies, 95))
            max_ms = float(np.max(latencies))

            print(
                f"\n[BENCHMARK NumPy Fallback 1080p (100 frames)] "
                f"Avg: {avg_ms:.3f}ms, P95: {p95_ms:.3f}ms, Max: {max_ms:.3f}ms"
            )

            self.assertLess(avg_ms, 3.0, f"NumPy fallback average {avg_ms:.3f}ms exceeds 3ms")
            self.assertLess(p95_ms, 4.0, f"NumPy fallback P95 {p95_ms:.3f}ms exceeds 4ms")
        finally:
            frame_detector_module.cv2 = orig_cv2

    def test_benchmark_100_frames_bgra_4channels(self):
        """Benchmark 100 consecutive 1080p frames with 4-channel BGRA captures."""
        np.random.seed(42)
        h, w = 1080, 1920
        base_frame = np.full((h, w, 4), 180, dtype=np.uint8)
        prev = base_frame

        latencies = []
        for i in range(100):
            curr = prev.copy()
            if i % 4 == 0:
                curr[900:1000, 300:1600, :3] = 40
            t0 = time.perf_counter()
            _ = self.detector.detect(prev, curr)
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1000.0)
            prev = curr

        avg_ms = float(np.mean(latencies))
        p95_ms = float(np.percentile(latencies, 95))
        self.assertLess(avg_ms, 3.0)
        self.assertLess(p95_ms, 3.0)

    # =========================================================================
    # 2. FrameChangeDetector: Noise Rejection & Edge Cases
    # =========================================================================

    def test_noise_rejection_cursor_blinking_varied_positions(self):
        """Stress-test cursor blinking across 50 arbitrary positions on 1080p screen."""
        h, w = 1080, 1920
        base = np.full((h, w, 3), 245, dtype=np.uint8)

        # Caret sizes: 2x16, 2x24, 3x28
        cursor_sizes = [(16, 2), (24, 2), (28, 3)]

        for ch, cw in cursor_sizes:
            for x in [0, 10, 100, 500, 960, 1500, 1910]:
                for y in [0, 10, 200, 540, 900, 1050]:
                    curr = base.copy()
                    curr[y : y + ch, x : x + cw] = 0  # black cursor
                    res = self.detector.detect(base, curr)
                    self.assertFalse(
                        res.has_changed,
                        f"Cursor ({ch}x{cw}) at ({x}, {y}) should be rejected as noise (changed_pixels={res.changed_pixels})",
                    )
                    self.assertIsNone(res.roi_box)

    def test_noise_rejection_random_pixel_jitter_under_threshold(self):
        """Uniform noise with amplitude <= 16 across entire 1080p frame must be filtered."""
        np.random.seed(123)
        f1 = np.random.randint(30, 220, (1080, 1920, 3), dtype=np.uint8)
        # Noise within [-15, 15]
        noise = np.random.randint(-15, 16, f1.shape, dtype=np.int16)
        f2 = np.clip(f1.astype(np.int16) + noise, 0, 255).astype(np.uint8)

        res = self.detector.detect(f1, f2)
        self.assertFalse(res.has_changed)
        self.assertEqual(res.changed_pixels, 0)
        self.assertIsNone(res.roi_box)

    def test_noise_rejection_isolated_salt_and_pepper(self):
        """Salt and pepper noise with count < min_changed_pixels (40) must be rejected."""
        f1 = np.full((1080, 1920, 3), 100, dtype=np.uint8)
        f2 = f1.copy()
        # Change exactly 30 downsampled pixels (using 30 isolated 4x4 blocks)
        for i in range(30):
            r = i * 30
            c = i * 50
            f2[r : r + 4, c : c + 4] = 255

        res = self.detector.detect(f1, f2)
        self.assertFalse(res.has_changed)
        self.assertLessEqual(res.changed_pixels, 30)
        self.assertIsNone(res.roi_box)

    def test_frame_change_non_contiguous_arrays(self):
        """Ensure detector handles non-contiguous memory layouts (strides, views)."""
        big = np.zeros((1080, 1920 * 2, 3), dtype=np.uint8)
        f1 = big[:, ::2, :]  # non-contiguous slice
        f2 = f1.copy()
        f2[100:200, 100:300] = 255

        self.assertFalse(f1.flags["C_CONTIGUOUS"])
        res = self.detector.detect(f1, f2)
        self.assertTrue(res.has_changed)
        self.assertIsNotNone(res.roi_box)

    def test_frame_change_extreme_resolutions(self):
        """Ensure detector handles 1x1, 2x2, odd-dimension frames without crashing."""
        for dims in [(1, 1, 3), (2, 2, 3), (3, 3, 3), (7, 13, 3), (1081, 1919, 3)]:
            f1 = np.zeros(dims, dtype=np.uint8)
            f2 = f1.copy()
            res = self.detector.detect(f1, f2)
            self.assertFalse(res.has_changed)

    # =========================================================================
    # 3. AdaptivePollingController: Chaotic Patterns & State Invariants
    # =========================================================================

    def test_adaptive_polling_state_invariants(self):
        """Verify strict mathematical invariants on AdaptivePollingController."""
        controller = AdaptivePollingController(
            active_interval=0.12,
            warm_interval=0.25,
            idle_interval=0.50,
            deep_idle_interval=0.80,
            warm_threshold=2,
            idle_threshold=5,
            deep_idle_threshold=20,
        )

        # Invariant 1: Fresh controller starts at ACTIVE with 0 static frames
        self.assertEqual(controller.state, PollingState.ACTIVE)
        self.assertEqual(controller.static_count, 0)

        # Invariant 2: Step-by-step threshold transitions
        expected_progression = [
            (False, 1, PollingState.ACTIVE, 0.12),
            (False, 2, PollingState.WARM, 0.25),
            (False, 3, PollingState.WARM, 0.25),
            (False, 4, PollingState.WARM, 0.25),
            (False, 5, PollingState.IDLE, 0.50),
            (False, 19, PollingState.IDLE, 0.50),
            (False, 20, PollingState.DEEP_IDLE, 0.80),
            (False, 100, PollingState.DEEP_IDLE, 0.80),
        ]

        # Walk through from 0 to 100 static frames
        for frame_idx in range(1, 101):
            delay = controller.on_frame(has_changed=False)
            self.assertEqual(controller.static_count, frame_idx)

            if frame_idx < 2:
                self.assertEqual(controller.state, PollingState.ACTIVE)
                self.assertAlmostEqual(delay, 0.12)
            elif frame_idx < 5:
                self.assertEqual(controller.state, PollingState.WARM)
                self.assertAlmostEqual(delay, 0.25)
            elif frame_idx < 20:
                self.assertEqual(controller.state, PollingState.IDLE)
                self.assertAlmostEqual(delay, 0.50)
            else:
                self.assertEqual(controller.state, PollingState.DEEP_IDLE)
                self.assertAlmostEqual(delay, 0.80)

    def test_adaptive_polling_rapid_wakeups(self):
        """Stress-test 100 continuous cycles of deep idle -> immediate wake."""
        controller = AdaptivePollingController()

        for cycle in range(100):
            # Drive to DEEP_IDLE
            for _ in range(25):
                controller.on_frame(has_changed=False)
            self.assertEqual(controller.state, PollingState.DEEP_IDLE)

            # Wake up on visual change
            delay = controller.on_frame(has_changed=True)
            self.assertEqual(
                controller.state,
                PollingState.ACTIVE,
                f"Cycle {cycle}: must immediately return to ACTIVE",
            )
            self.assertEqual(
                controller.static_count,
                0,
                f"Cycle {cycle}: static count must be 0",
            )
            self.assertAlmostEqual(delay, 0.12)

    def test_adaptive_polling_chaotic_arrival_stream(self):
        """Simulate 2,000 chaotic frame arrival decisions (Markov process)."""
        np.random.seed(999)
        controller = AdaptivePollingController()

        # States: 0 = static, 1 = changed
        # Markov transition probabilities
        current_event = False
        for step in range(2000):
            if current_event:
                # After change: 70% chance of another change (active burst), 30% static
                current_event = np.random.rand() < 0.70
            else:
                # After static: 10% chance of wake up, 90% chance of remaining static
                current_event = np.random.rand() < 0.10

            delay = controller.on_frame(current_event)

            # Invariants verification
            if current_event:
                self.assertEqual(controller.state, PollingState.ACTIVE)
                self.assertEqual(controller.static_count, 0)
                self.assertAlmostEqual(delay, 0.12)
            else:
                if controller.static_count >= 20:
                    self.assertEqual(controller.state, PollingState.DEEP_IDLE)
                    self.assertAlmostEqual(delay, 0.80)
                elif controller.static_count >= 5:
                    self.assertEqual(controller.state, PollingState.IDLE)
                    self.assertAlmostEqual(delay, 0.50)
                elif controller.static_count >= 2:
                    self.assertEqual(controller.state, PollingState.WARM)
                    self.assertAlmostEqual(delay, 0.25)
                else:
                    self.assertEqual(controller.state, PollingState.ACTIVE)
                    self.assertAlmostEqual(delay, 0.12)

    def test_adaptive_polling_long_idle_100k_frames(self):
        """Stress-test long idle of 100,000 frames to check memory & overflow."""
        controller = AdaptivePollingController()
        t0 = time.perf_counter()
        for _ in range(100_000):
            controller.on_frame(has_changed=False)
        t1 = time.perf_counter()

        self.assertEqual(controller.static_count, 100_000)
        self.assertEqual(controller.state, PollingState.DEEP_IDLE)
        total_time_ms = (t1 - t0) * 1000.0
        # 100k transitions should take less than 100ms
        self.assertLess(total_time_ms, 100.0, f"100k iterations took too long: {total_time_ms:.1f}ms")

    # =========================================================================
    # 4. OcrService: ROI Remapping, Varied Padding, Fallback Logic
    # =========================================================================

    def test_roi_coordinate_remapping_varied_padding_and_multibox(self):
        """Test multi-box coordinate remapping across varied paddings (0, 10, 24, 60, 200)."""
        frame_w, frame_h = 1920, 1080
        frame = np.zeros((frame_h, frame_w, 3), dtype=np.uint8)

        # Region of change: (400, 300, 900, 600)
        roi_box = (400, 300, 900, 600)

        paddings = [0, 10, 24, 50, 100]
        for pad in paddings:
            # Crop dimensions:
            # px1 = max(0, 400 - pad), py1 = max(0, 300 - pad)
            # px2 = min(1920, 900 + pad), py2 = min(1080, 600 + pad)
            px1 = max(0, 400 - pad)
            py1 = max(0, 300 - pad)

            # Mock OCR returns 3 lines within crop coordinates
            mock_lines = [
                OcrLine("Line 1", 0.99, (10, 20, 150, 50)),
                CustomDuckLine("Line 2", (200, 80, 350, 120), 0.94),
                CustomConfidenceLine("Line 3", (50, 150, 400, 190), 0.88),
            ]

            service = OcrService(mock_ocr_fn=lambda crop: list(mock_lines), padding=pad, max_roi_ratio=0.7)
            results = service.recognize_frame(frame, roi_box)

            self.assertFalse(service.last_was_fallback)
            self.assertEqual(len(results), 3)

            # Check Line 1 (OcrLine dataclass)
            expected_box_1 = (10 + px1, 20 + py1, 150 + px1, 50 + py1)
            self.assertEqual(results[0].box, expected_box_1)
            self.assertEqual(results[0].text, "Line 1")
            self.assertAlmostEqual(results[0].score, 0.99)
            self.assertIsInstance(results[0], OcrLine)

            # Check Line 2 (CustomDuckLine with score)
            expected_box_2 = (200 + px1, 80 + py1, 350 + px1, 120 + py1)
            self.assertEqual(results[1].box, expected_box_2)
            self.assertEqual(results[1].text, "Line 2")
            self.assertAlmostEqual(results[1].score, 0.94)

            # Check Line 3 (CustomConfidenceLine with confidence)
            expected_box_3 = (50 + px1, 150 + py1, 400 + px1, 190 + py1)
            self.assertEqual(results[2].box, expected_box_3)
            self.assertEqual(results[2].text, "Line 3")
            self.assertAlmostEqual(results[2].confidence, 0.88)

    def test_roi_area_ratio_boundary_0_6(self):
        """Verify strict area ratio fallback trigger around 0.6 threshold."""
        w, h = 1000, 1000  # Total area = 1,000,000
        frame = np.zeros((h, w, 3), dtype=np.uint8)

        # Service with padding=0 to control exact box areas
        service = OcrService(
            mock_ocr_fn=lambda img: [OcrLine("text", 0.9, (0, 0, 10, 10))],
            padding=0,
            max_roi_ratio=0.6,
        )

        # Test Case 1: Area = 590,000 (ratio = 0.59 < 0.6) -> Localized ROI
        box_59 = (0, 0, 1000, 590)
        res1 = service.recognize_frame(frame, box_59)
        self.assertFalse(service.last_was_fallback, "Ratio 0.59 should NOT trigger fallback")

        # Test Case 2: Area = 600,000 (ratio = 0.60 <= 0.6) -> Localized ROI (ratio <= max_roi_ratio)
        box_60 = (0, 0, 1000, 600)
        res2 = service.recognize_frame(frame, box_60)
        self.assertFalse(service.last_was_fallback, "Ratio 0.60 should NOT trigger fallback (> 0.6 triggers)")

        # Test Case 3: Area = 601,000 (ratio = 0.601 > 0.6) -> Full OCR Fallback!
        box_601 = (0, 0, 1000, 601)
        res3 = service.recognize_frame(frame, box_601)
        self.assertTrue(service.last_was_fallback, "Ratio 0.601 MUST trigger full OCR fallback")

    def test_health_check_cadence_and_reset(self):
        """Stress-test health check fallback cadence (every 25 frames) and reset semantics."""
        frame = np.zeros((1000, 1000, 3), dtype=np.uint8)
        small_box = (100, 100, 200, 200)

        received_crops = []

        def mock_ocr(crop):
            received_crops.append(crop.shape)
            return [OcrLine("ok", 1.0, (0, 0, 10, 10))]

        service = OcrService(mock_ocr_fn=mock_ocr, padding=10, health_check_interval=25)

        # 1. 24 localized calls
        for f in range(1, 25):
            service.recognize_frame(frame, small_box)
            self.assertFalse(
                service.last_was_fallback,
                f"Frame {f} should be localized, not fallback",
            )
            self.assertEqual(service.frames_since_full_ocr, f)
            # Crop should be padded box: (100-10) to (200+10) = 120x120
            self.assertEqual(received_crops[-1][:2], (120, 120))

        # 2. 25th frame: Health check triggers full OCR
        service.recognize_frame(frame, small_box)
        self.assertTrue(service.last_was_fallback, "25th frame must trigger health check fallback")
        self.assertEqual(service.frames_since_full_ocr, 0, "Counter must reset to 0 after health check")
        # Full frame received
        self.assertEqual(received_crops[-1][:2], (1000, 1000))

        # 3. 26th frame: Returns to localized OCR
        service.recognize_frame(frame, small_box)
        self.assertFalse(service.last_was_fallback)
        self.assertEqual(service.frames_since_full_ocr, 1)
        self.assertEqual(received_crops[-1][:2], (120, 120))

        # 4. Premature full OCR fallback resets health check counter
        # Run 5 localized frames
        for _ in range(5):
            service.recognize_frame(frame, small_box)
        self.assertEqual(service.frames_since_full_ocr, 6)

        # Large box triggers area ratio fallback
        large_box = (0, 0, 900, 900)
        service.recognize_frame(frame, large_box)
        self.assertTrue(service.last_was_fallback)
        self.assertEqual(
            service.frames_since_full_ocr,
            0,
            "Area ratio fallback must reset frames_since_full_ocr to 0",
        )

        # 5. force_full=True resets counter
        service.recognize_frame(frame, small_box)
        self.assertEqual(service.frames_since_full_ocr, 1)
        service.recognize_frame(frame, small_box, force_full=True)
        self.assertTrue(service.last_was_fallback)
        self.assertEqual(
            service.frames_since_full_ocr,
            0,
            "force_full=True must reset frames_since_full_ocr to 0",
        )

        # 6. roi_box=None resets counter
        service.recognize_frame(frame, small_box)
        self.assertEqual(service.frames_since_full_ocr, 1)
        service.recognize_frame(frame, None)
        self.assertTrue(service.last_was_fallback)
        self.assertEqual(
            service.frames_since_full_ocr,
            0,
            "roi_box=None must reset frames_since_full_ocr to 0",
        )

    # =========================================================================
    # 5. OcrStabilizer: Levenshtein & Debounce Edge Cases
    # =========================================================================

    def test_levenshtein_distance_stress(self):
        """Test Levenshtein distance on corner cases, unicode, and long strings."""
        self.assertEqual(levenshtein_distance("", ""), 0)
        self.assertEqual(levenshtein_distance("abc", ""), 3)
        self.assertEqual(levenshtein_distance("", "def"), 3)
        self.assertEqual(levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(levenshtein_distance("你好世界", "你好视界"), 1)
        self.assertEqual(levenshtein_distance("Hello 1", "Hello l"), 1)
        self.assertEqual(levenshtein_distance("a" * 1000, "a" * 1000), 0)
        self.assertEqual(levenshtein_distance("a" * 1000, "a" * 998 + "bb"), 2)

    def test_stabilizer_jitter_candidate_switching(self):
        """Verify that when jitter candidates shift before 2 debounces, candidate resets."""
        stabilizer = OcrStabilizer(debounce_frames=2, max_jitter_dist=2)

        # Baseline: "Target Text"
        c1, lines = stabilizer.process(["Target Text"])
        self.assertTrue(c1)

        # Frame 2: candidate A "Target Text1" (dist 1) -> suppressed
        c2, lines = stabilizer.process(["Target Text1"])
        self.assertFalse(c2)
        self.assertEqual(lines, ["Target Text"])

        # Frame 3: candidate B "Target Text2" (dist 1 from baseline, but diff from cand A)
        # Should reset candidate repeat count to 1 and NOT promote!
        c3, lines = stabilizer.process(["Target Text2"])
        self.assertFalse(c3)
        self.assertEqual(lines, ["Target Text"])

        # Frame 4: candidate B repeats 2nd time -> now promoted!
        c4, lines = stabilizer.process(["Target Text2"])
        self.assertTrue(c4)
        self.assertEqual(lines, ["Target Text2"])

    def test_ocr_stabilizer_consecutive_empty_frames_flaw(self):
        """EMPIRICAL VERIFICATION: OcrStabilizer suppresses consecutive empty frames after fix."""
        stabilizer = OcrStabilizer()
        # Frame 1: empty screen -> initial frame establishes baseline
        c1, _ = stabilizer.process([])
        self.assertTrue(c1)

        # Frame 2: screen remains empty -> correctly evaluates to False
        c2, _ = stabilizer.process([])
        self.assertFalse(
            c2,
            "OcrStabilizer must not return True on consecutive empty frames",
        )

    def test_ocr_stabilizer_cleared_then_empty_flaw(self):
        """EMPIRICAL VERIFICATION: Text clearing then remaining empty suppresses false positives after fix."""
        stabilizer = OcrStabilizer()
        # Frame 1: Text present
        c1, _ = stabilizer.process(["Subtitle line"])
        self.assertTrue(c1)

        # Frame 2: Text disappears -> substantive change to empty
        c2, _ = stabilizer.process([])
        self.assertTrue(c2, "Text cleared is expected to report substantive change")

        # Frame 3: Screen is STILL empty -> should NOT report substantive change!
        c3, _ = stabilizer.process([])
        self.assertFalse(
            c3,
            "OcrStabilizer must not return True on continued empty frames after text clear",
        )


if __name__ == "__main__":
    unittest.main()
