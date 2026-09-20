# -*- coding: utf-8 -*-
"""End-to-end & component test suite for FrameChangeDetector, Adaptive Polling, ROI OCR, and OCR Stabilizer.

Covers:
- Feature 3: FrameChangeDetector (PR 3)
- Feature 4: Adaptive Polling State Machine (PR 4)
- Feature 5: ROI OCR & Full OCR Fallback (PR 5)
- Feature 6: OCR Stabilizer (PR 6)
Tiers 1-4: Static frame skipping, cursor & video noise filtering, <3ms performance benchmark,
state progression & immediate wake, ROI padding & fallback, Levenshtein jitter suppression,
and real-world document/video playback workloads.
"""

import time
import unittest
from dataclasses import dataclass
from enum import Enum
from typing import Any
import numpy as np

# Try importing production modules if available
try:
    from app.frame_detector import FrameChangeDetector as _ProdFrameChangeDetector
    from app.frame_detector import FrameDiffResult as _ProdFrameDiffResult
except ImportError:
    _ProdFrameChangeDetector = None
    _ProdFrameDiffResult = None

try:
    from app.adaptive_polling import AdaptivePollingController as _ProdAdaptivePollingController
    from app.adaptive_polling import PollingState as _ProdPollingState
except ImportError:
    _ProdAdaptivePollingController = None
    _ProdPollingState = None

try:
    from app.ocr_stabilizer import OcrStabilizer as _ProdOcrStabilizer
except ImportError:
    _ProdOcrStabilizer = None

try:
    from app.ocr_service import OcrService as _ProdOcrService
except ImportError:
    _ProdOcrService = None


# ==========================================
# Contract Definitions (from PROJECT.md / spec_report.md)
# ==========================================

@dataclass(frozen=True)
class ContractFrameDiffResult:
    has_changed: bool
    changed_ratio: float
    changed_pixels: int
    roi_box: tuple[int, int, int, int] | None  # (x1, y1, x2, y2)


class ContractFrameChangeDetector:
    """PR 3: High-performance two-stage frame change detector (<3ms on 1080p)."""

    def __init__(
        self,
        step: int = 4,
        pixel_threshold: int = 16,
        min_changed_pixels: int = 40,
        min_changed_ratio: float = 0.0002,
    ):
        self.step = step
        self.pixel_threshold = pixel_threshold
        self.min_changed_pixels = min_changed_pixels
        self.min_changed_ratio = min_changed_ratio

    def detect(self, previous: np.ndarray, current: np.ndarray) -> ContractFrameDiffResult:
        if previous.shape != current.shape:
            h, w = current.shape[:2]
            return ContractFrameDiffResult(
                has_changed=True,
                changed_ratio=1.0,
                changed_pixels=current.size,
                roi_box=(0, 0, w, h),
            )

        # Stage 1: Downsampling
        s = self.step
        prev_sub = previous[::s, ::s]
        curr_sub = current[::s, ::s]

        # Convert to grayscale via integer coefficients: (R*77 + G*150 + B*29) >> 8
        if len(curr_sub.shape) == 3:
            prev_gray = (
                prev_sub[:, :, 0].astype(np.int32) * 29
                + prev_sub[:, :, 1].astype(np.int32) * 150
                + prev_sub[:, :, 2].astype(np.int32) * 77
            ) >> 8
            curr_gray = (
                curr_sub[:, :, 0].astype(np.int32) * 29
                + curr_sub[:, :, 1].astype(np.int32) * 150
                + curr_sub[:, :, 2].astype(np.int32) * 77
            ) >> 8
        else:
            prev_gray = prev_sub.astype(np.int32)
            curr_gray = curr_sub.astype(np.int32)

        # Stage 2: Diff and Thresholding
        diff = np.abs(curr_gray - prev_gray)
        mask = diff > self.pixel_threshold
        changed_count = int(np.count_nonzero(mask))
        total_sub_pixels = mask.size
        ratio = changed_count / total_sub_pixels

        # Noise & Cursor Filtering (< 40 downsampled pixels)
        if changed_count < self.min_changed_pixels:
            return ContractFrameDiffResult(
                has_changed=False,
                changed_ratio=ratio,
                changed_pixels=changed_count,
                roi_box=None,
            )

        # ROI Bounding Box Computation
        y_indices, x_indices = np.nonzero(mask)
        x1 = int(x_indices.min() * s)
        y1 = int(y_indices.min() * s)
        x2 = int((x_indices.max() + 1) * s)
        y2 = int((y_indices.max() + 1) * s)
        h, w = current.shape[:2]
        roi_box = (max(0, x1), max(0, y1), min(w, x2), min(h, y2))

        return ContractFrameDiffResult(
            has_changed=True,
            changed_ratio=ratio,
            changed_pixels=changed_count,
            roi_box=roi_box,
        )


class ContractPollingState(str, Enum):
    ACTIVE = "ACTIVE"
    WARM = "WARM"
    IDLE = "IDLE"
    DEEP_IDLE = "DEEP_IDLE"


class ContractAdaptivePollingController:
    """PR 4: Dynamic polling state machine."""

    def __init__(
        self,
        active_interval: float = 0.12,
        warm_interval: float = 0.25,
        idle_interval: float = 0.50,
        deep_idle_interval: float = 0.80,
        warm_threshold: int = 2,
        idle_threshold: int = 5,
        deep_idle_threshold: int = 20,
    ):
        self.active_interval = active_interval
        self.warm_interval = warm_interval
        self.idle_interval = idle_interval
        self.deep_idle_interval = deep_idle_interval
        self.warm_threshold = warm_threshold
        self.idle_threshold = idle_threshold
        self.deep_idle_threshold = deep_idle_threshold

        self._state = ContractPollingState.ACTIVE
        self._static_count = 0

    def on_frame(self, has_changed: bool) -> float:
        if has_changed:
            self._static_count = 0
            self._state = ContractPollingState.ACTIVE
            return self.active_interval

        self._static_count += 1
        if self._static_count >= self.deep_idle_threshold:
            self._state = ContractPollingState.DEEP_IDLE
            return self.deep_idle_interval
        elif self._static_count >= self.idle_threshold:
            self._state = ContractPollingState.IDLE
            return self.idle_interval
        elif self._static_count >= self.warm_threshold:
            self._state = ContractPollingState.WARM
            return self.warm_interval
        else:
            self._state = ContractPollingState.ACTIVE
            return self.active_interval

    @property
    def state(self) -> ContractPollingState:
        return self._state


@dataclass
class OcrLineStub:
    text: str
    box: tuple[int, int, int, int]  # (x1, y1, x2, y2)
    confidence: float = 0.99


class ContractOcrService:
    """PR 5: Localized ROI OCR with 24px padding and full OCR fallback."""

    def __init__(
        self,
        mock_ocr_fn=None,
        padding: int = 24,
        max_roi_ratio: float = 0.6,
        health_check_interval: int = 25,
    ):
        self.mock_ocr_fn = mock_ocr_fn or (lambda crop: [OcrLineStub("sample text", (0, 0, 100, 20))])
        self.padding = padding
        self.max_roi_ratio = max_roi_ratio
        self.health_check_interval = health_check_interval
        self.frames_since_full_ocr = 0
        self.last_was_fallback = False
        self.last_padded_roi: tuple[int, int, int, int] | None = None

    def compute_padded_roi(
        self, roi_box: tuple[int, int, int, int], width: int, height: int
    ) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = roi_box
        p = self.padding
        return (
            max(0, x1 - p),
            max(0, y1 - p),
            min(width, x2 + p),
            min(height, y2 + p),
        )

    def recognize_frame(
        self,
        frame: np.ndarray,
        roi_box: tuple[int, int, int, int] | None,
        force_full: bool = False,
    ) -> list[OcrLineStub]:
        h, w = frame.shape[:2]
        self.frames_since_full_ocr += 1

        # Fallback check 1: periodic health check
        if force_full or self.frames_since_full_ocr >= self.health_check_interval or roi_box is None:
            self.frames_since_full_ocr = 0
            self.last_was_fallback = True
            return self.mock_ocr_fn(frame)

        # Apply 24px padding
        padded_roi = self.compute_padded_roi(roi_box, w, h)
        self.last_padded_roi = padded_roi
        px1, py1, px2, py2 = padded_roi
        roi_area = (px2 - px1) * (py2 - py1)
        full_area = w * h
        area_ratio = roi_area / max(1, full_area)

        # Fallback check 2: area ratio > 0.6
        if area_ratio > self.max_roi_ratio:
            self.frames_since_full_ocr = 0
            self.last_was_fallback = True
            return self.mock_ocr_fn(frame)

        # Localized ROI recognition with coordinate remapping
        self.last_was_fallback = False
        crop = frame[py1:py2, px1:px2]
        crop_lines = self.mock_ocr_fn(crop)

        # Coordinate remapping: add px1, py1 to each line's bounding box
        remapped_lines = []
        for line in crop_lines:
            bx1, by1, bx2, by2 = line.box
            remapped_box = (bx1 + px1, by1 + py1, bx2 + px1, by2 + py1)
            remapped_lines.append(OcrLineStub(line.text, remapped_box, line.confidence))
        return remapped_lines


def _levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if not s2:
        return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            ins = prev[j + 1] + 1
            delete = curr[j] + 1
            sub = prev[j] + (c1 != c2)
            curr.append(min(ins, delete, sub))
        prev = curr
    return prev[-1]


class ContractOcrStabilizer:
    """PR 6: Temporal debounce (2 frames) & Levenshtein edit-distance jitter suppression."""

    def __init__(self, debounce_frames: int = 2, max_jitter_dist: int = 2):
        self.debounce_frames = debounce_frames
        self.max_jitter_dist = max_jitter_dist
        self._stable_lines: list[OcrLineStub] = []
        self._candidate_lines: list[OcrLineStub] = []
        self._candidate_repeat_count: int = 0

    def process(self, lines: list[OcrLineStub]) -> tuple[bool, list[OcrLineStub]]:
        """Returns (is_substantive_change, stabilized_lines)."""
        if not self._stable_lines:
            # Initial state
            self._stable_lines = lines
            return True, lines

        curr_text = " ".join([l.text for l in lines])
        prev_text = " ".join([l.text for l in self._stable_lines])

        dist = _levenshtein(curr_text, prev_text)

        # If identical or minor single-frame jitter (Levenshtein distance <= max_jitter_dist)
        if dist <= self.max_jitter_dist:
            if curr_text == prev_text:
                self._candidate_repeat_count = 0
                return False, self._stable_lines

            # Jitter candidate: check if it repeats for debounce_frames
            cand_text = " ".join([l.text for l in self._candidate_lines])
            if curr_text == cand_text:
                self._candidate_repeat_count += 1
                if self._candidate_repeat_count >= self.debounce_frames:
                    self._stable_lines = lines
                    self._candidate_repeat_count = 0
                    return True, self._stable_lines
                return False, self._stable_lines
            else:
                self._candidate_lines = lines
                self._candidate_repeat_count = 1
                return False, self._stable_lines

        # Substantive text change (Levenshtein distance > threshold)
        self._stable_lines = lines
        self._candidate_lines = []
        self._candidate_repeat_count = 0
        return True, lines


# Factory helpers
def create_frame_detector():
    return _ProdFrameChangeDetector() if _ProdFrameChangeDetector else ContractFrameChangeDetector()


def create_adaptive_polling():
    return _ProdAdaptivePollingController() if _ProdAdaptivePollingController else ContractAdaptivePollingController()


def create_ocr_stabilizer():
    return _ProdOcrStabilizer() if _ProdOcrStabilizer else ContractOcrStabilizer()


def create_ocr_service():
    return _ProdOcrService() if _ProdOcrService else ContractOcrService()


class TestFrameChangeTiers(unittest.TestCase):
    def setUp(self):
        self.detector = create_frame_detector()
        self.polling = create_adaptive_polling()
        self.stabilizer = create_ocr_stabilizer()
        self.ocr_service = create_ocr_service()

    # ==========================================
    # Tier 1: Primary Feature Behavior
    # ==========================================

    def test_frame_diff_identical_frames(self):
        """Tier 1: Identical frames return has_changed=False, skip OCR."""
        f1 = np.full((1080, 1920, 3), 128, dtype=np.uint8)
        f2 = np.full((1080, 1920, 3), 128, dtype=np.uint8)

        res = self.detector.detect(f1, f2)
        self.assertFalse(res.has_changed)
        self.assertEqual(res.changed_pixels, 0)
        self.assertIsNone(res.roi_box)

    def test_frame_diff_significant_change(self):
        """Tier 1: Significant visual difference flags has_changed=True and returns roi_box."""
        f1 = np.zeros((600, 800, 3), dtype=np.uint8)
        f2 = np.zeros((600, 800, 3), dtype=np.uint8)
        # Add a white box in center
        f2[200:300, 300:500] = 255

        res = self.detector.detect(f1, f2)
        self.assertTrue(res.has_changed)
        self.assertGreater(res.changed_pixels, 40)
        self.assertIsNotNone(res.roi_box)
        x1, y1, x2, y2 = res.roi_box
        self.assertLessEqual(x1, 300)
        self.assertLessEqual(y1, 200)
        self.assertGreaterEqual(x2, 500)
        self.assertGreaterEqual(y2, 300)

    def test_adaptive_polling_progression(self):
        """Tier 1: Continuous static frames transition ACTIVE -> WARM -> IDLE -> DEEP_IDLE."""
        self.assertEqual(self.polling.state, ContractPollingState.ACTIVE)

        # 1 static frame -> still ACTIVE
        t1 = self.polling.on_frame(has_changed=False)
        self.assertEqual(self.polling.state, ContractPollingState.ACTIVE)
        self.assertAlmostEqual(t1, 0.12)

        # 2 static frames -> WARM (>=2)
        t2 = self.polling.on_frame(has_changed=False)
        self.assertEqual(self.polling.state, ContractPollingState.WARM)
        self.assertAlmostEqual(t2, 0.25)

        # 5 static frames total -> IDLE (>=5)
        for _ in range(3):
            self.polling.on_frame(has_changed=False)
        self.assertEqual(self.polling.state, ContractPollingState.IDLE)
        self.assertAlmostEqual(self.polling.idle_interval, 0.50)

        # 20 static frames total -> DEEP_IDLE (>=20)
        for _ in range(15):
            self.polling.on_frame(has_changed=False)
        self.assertEqual(self.polling.state, ContractPollingState.DEEP_IDLE)
        self.assertAlmostEqual(self.polling.deep_idle_interval, 0.80)

    def test_adaptive_polling_immediate_wake(self):
        """Tier 1: A frame change immediately resets DEEP_IDLE back to ACTIVE."""
        # Drive into DEEP_IDLE
        for _ in range(25):
            self.polling.on_frame(has_changed=False)
        self.assertEqual(self.polling.state, ContractPollingState.DEEP_IDLE)

        # Wake up on change
        delay = self.polling.on_frame(has_changed=True)
        self.assertEqual(self.polling.state, ContractPollingState.ACTIVE)
        self.assertAlmostEqual(delay, 0.12)

    def test_ocr_stabilizer_accepts_stable_lines(self):
        """Tier 1: Repeated identical lines do not re-trigger translation."""
        lines1 = [OcrLineStub("First subtitle line", (10, 10, 100, 30))]
        substantive, res1 = self.stabilizer.process(lines1)
        self.assertTrue(substantive)

        # Second frame: exact same text
        substantive2, res2 = self.stabilizer.process(lines1)
        self.assertFalse(substantive2, "Unchanged text must not trigger translation")

    # ==========================================
    # Tier 2: Boundary & Corner Cases
    # ==========================================

    def test_frame_diff_blinking_cursor_filtered(self):
        """Tier 2: Blinking cursor (<40 changed pixels) filtered out as noise."""
        f1 = np.full((800, 1000, 3), 240, dtype=np.uint8)
        f2 = f1.copy()
        # Cursor line: 2x16 pixels in full frame -> ~2x4 downsampled pixels = 8 pixels
        f2[100:116, 200:202] = 0

        res = self.detector.detect(f1, f2)
        self.assertFalse(res.has_changed, "Cursor blink (<40 downsampled pixels) must be filtered")
        self.assertIsNone(res.roi_box)

    def test_frame_diff_subtle_video_noise_filtered(self):
        """Tier 2: Subtle video noise (delta <= 16) filtered out."""
        f1 = np.full((600, 800, 3), 100, dtype=np.uint8)
        # Add random noise of amplitude <= 10
        noise = np.random.randint(-10, 11, f1.shape, dtype=np.int16)
        f2 = np.clip(f1.astype(np.int16) + noise, 0, 255).astype(np.uint8)

        res = self.detector.detect(f1, f2)
        self.assertFalse(res.has_changed, "Noise under pixel_threshold must be filtered")

    def test_frame_diff_benchmark_strictly_under_3ms(self):
        """Tier 2: Execution benchmark strictly < 3ms on 1080p frames."""
        f1 = np.random.randint(0, 256, (1080, 1920, 3), dtype=np.uint8)
        f2 = f1.copy()
        f2[500:600, 800:1000] = 255

        # Warmup
        self.detector.detect(f1, f2)

        # Measure 20 runs
        times = []
        for _ in range(20):
            start = time.perf_counter()
            self.detector.detect(f1, f2)
            times.append(time.perf_counter() - start)

        avg_ms = (sum(times) / len(times)) * 1000
        p95_ms = sorted(times)[int(len(times) * 0.95)] * 1000

        self.assertLess(avg_ms, 3.0, f"Average execution time {avg_ms:.2f}ms exceeds 3ms limit")
        self.assertLess(p95_ms, 3.0, f"P95 execution time {p95_ms:.2f}ms exceeds 3ms limit")

    def test_frame_diff_shape_mismatch_triggers_full_change(self):
        """Tier 2: Window resize / shape mismatch immediately returns has_changed=True."""
        f1 = np.zeros((600, 800, 3), dtype=np.uint8)
        f2 = np.zeros((700, 900, 3), dtype=np.uint8)

        res = self.detector.detect(f1, f2)
        self.assertTrue(res.has_changed)
        self.assertEqual(res.changed_ratio, 1.0)
        self.assertEqual(res.roi_box, (0, 0, 900, 700))

    def test_roi_box_calculation_with_24px_padding(self):
        """Tier 2: ROI bounding box is expanded with 24px margin padding and clamped to borders."""
        raw_box = (100, 150, 300, 250)
        w, h = 1000, 800
        padded = self.ocr_service.compute_padded_roi(raw_box, w, h)
        # Expect (100-24, 150-24, 300+24, 250+24)
        self.assertEqual(padded, (76, 126, 324, 274))

        # Test border clamping
        border_box = (10, 10, 995, 795)
        clamped = self.ocr_service.compute_padded_roi(border_box, w, h)
        self.assertEqual(clamped, (0, 0, 1000, 800))

    def test_roi_fallback_on_large_area_ratio(self):
        """Tier 2: Changes covering > 0.6 area ratio automatically trigger full OCR fallback."""
        frame = np.zeros((1000, 1000, 3), dtype=np.uint8)
        # ROI covering 800x800 = 64% of area
        large_roi = (100, 100, 900, 900)

        self.ocr_service.recognize_frame(frame, large_roi)
        self.assertTrue(
            self.ocr_service.last_was_fallback,
            "Area ratio > 0.6 must trigger full OCR fallback",
        )

    def test_roi_periodic_health_check_fallback(self):
        """Tier 2: Periodic health check forces full OCR every 25 frames."""
        frame = np.zeros((500, 500, 3), dtype=np.uint8)
        small_roi = (50, 50, 100, 100)

        # Run 24 localized frames
        for _ in range(24):
            self.ocr_service.recognize_frame(frame, small_roi)
            self.assertFalse(self.ocr_service.last_was_fallback)

        # 25th frame: triggers health check fallback
        self.ocr_service.recognize_frame(frame, small_roi)
        self.assertTrue(self.ocr_service.last_was_fallback)

    def test_ocr_stabilizer_filters_single_frame_jitter(self):
        """Tier 2: OCR character flip (e.g. '1' to 'l', edit distance <= 1) is suppressed on single frame."""
        l1 = [OcrLineStub("Text Line 1", (0, 0, 100, 20))]
        l2_jitter = [OcrLineStub("Text Line l", (0, 0, 100, 20))]

        # Initial frame
        self.stabilizer.process(l1)

        # Jitter frame
        substantive, stabilized = self.stabilizer.process(l2_jitter)
        self.assertFalse(substantive, "Single frame character flip jitter must be suppressed")
        self.assertEqual(stabilized[0].text, "Text Line 1")

    def test_ocr_stabilizer_confirms_valid_change(self):
        """Tier 2: True text changes with edit distance > 2 trigger immediately."""
        l1 = [OcrLineStub("Old dialogue content", (0, 0, 100, 20))]
        l2_new = [OcrLineStub("Completely new dialogue", (0, 0, 100, 20))]

        self.stabilizer.process(l1)
        substantive, stabilized = self.stabilizer.process(l2_new)
        self.assertTrue(substantive)
        self.assertEqual(stabilized[0].text, "Completely new dialogue")

    # ==========================================
    # Tier 3: Pairwise & Component Interactions
    # ==========================================

    def test_frame_diff_to_adaptive_polling_pipeline(self):
        """Tier 3: Connecting FrameChangeDetector outputs directly to AdaptivePollingController."""
        f1 = np.full((100, 100, 3), 10, dtype=np.uint8)
        f2 = f1.copy()

        # Step 1: Detect no change
        res = self.detector.detect(f1, f2)
        delay = self.polling.on_frame(res.has_changed)
        self.assertEqual(self.polling.state, ContractPollingState.ACTIVE)

        # 5 identical frames
        for _ in range(5):
            res = self.detector.detect(f1, f2)
            delay = self.polling.on_frame(res.has_changed)
        self.assertEqual(self.polling.state, ContractPollingState.IDLE)

        # Frame changes
        f3 = f1.copy()
        f3[20:60, 20:60] = 200
        res_change = self.detector.detect(f1, f3)
        delay_wake = self.polling.on_frame(res_change.has_changed)
        self.assertEqual(self.polling.state, ContractPollingState.ACTIVE)
        self.assertAlmostEqual(delay_wake, 0.12)

    def test_roi_ocr_remapping_coordinates(self):
        """Tier 3: Coordinates recognized inside ROI are properly remapped to global coordinates."""
        frame = np.zeros((800, 1000, 3), dtype=np.uint8)
        roi_box = (200, 300, 400, 500)

        # Mock OCR returns a line at (10, 10, 80, 30) relative to crop
        def mock_ocr(crop):
            return [OcrLineStub("localized word", (10, 10, 80, 30))]

        self.ocr_service.mock_ocr_fn = mock_ocr
        lines = self.ocr_service.recognize_frame(frame, roi_box)

        # Padded roi: 200-24 = 176, 300-24 = 276
        # Remapped box: (10+176, 10+276, 80+176, 30+276) = (186, 286, 256, 306)
        self.assertEqual(lines[0].box, (186, 286, 256, 306))

    # ==========================================
    # Tier 4: Real-World Scenarios
    # ==========================================

    def test_scenario_static_document_with_mouse_cursor(self):
        """Tier 4 Scenario 2: Static document reading with occasional mouse cursor blinking.

        Expectation: >= 95% of checks return has_changed=False, skipping OCR entirely.
        """
        doc_frame = np.full((1080, 1920, 3), 250, dtype=np.uint8)
        # Add mock text blocks
        doc_frame[100:150, 200:800] = 30
        doc_frame[200:250, 200:800] = 30

        checks = 100
        skipped_count = 0
        prev = doc_frame.copy()

        for i in range(checks):
            curr = doc_frame.copy()
            # In 10% of frames, add a cursor blink
            if i % 10 == 0:
                curr[100:116, 400:402] = 0

            res = self.detector.detect(prev, curr)
            if not res.has_changed:
                skipped_count += 1
            prev = curr

        skip_ratio = skipped_count / checks
        self.assertGreaterEqual(
            skip_ratio,
            0.95,
            f"Expected >= 95% OCR skip on static reading, got {skip_ratio*100:.1f}%",
        )

    def test_scenario_video_subtitles_continuous_playback(self):
        """Tier 4 Scenario 1: Video playback with subtitles localized at bottom.

        Expectation: ROI confines changes to bottom subtitle region; area ratio < 0.3; no fallback.
        """
        video_bg = np.full((1080, 1920, 3), 40, dtype=np.uint8)
        prev = video_bg.copy()

        curr = video_bg.copy()
        # Add subtitle at bottom: y from 950 to 1020, x from 500 to 1420
        curr[950:1020, 500:1420] = 240

        diff_res = self.detector.detect(prev, curr)
        self.assertTrue(diff_res.has_changed)
        self.assertIsNotNone(diff_res.roi_box)

        lines = self.ocr_service.recognize_frame(curr, diff_res.roi_box)
        # Localized OCR must have succeeded without fallback
        self.assertFalse(self.ocr_service.last_was_fallback)


class TestMilestone2ProductionModules(unittest.TestCase):
    """Direct verification of production modules for PR 3, PR 4, PR 5, and PR 6."""

    def test_production_imports_and_types(self):
        self.assertIsNotNone(_ProdFrameChangeDetector)
        self.assertIsNotNone(_ProdFrameDiffResult)
        self.assertIsNotNone(_ProdAdaptivePollingController)
        self.assertIsNotNone(_ProdPollingState)
        self.assertIsNotNone(_ProdOcrService)
        self.assertIsNotNone(_ProdOcrStabilizer)

    def test_frame_change_detector_edge_cases(self):
        detector = _ProdFrameChangeDetector()

        # None current frame
        res_none_curr = detector.detect(np.zeros((100, 100, 3), dtype=np.uint8), None)
        self.assertFalse(res_none_curr.has_changed)
        self.assertEqual(res_none_curr.changed_pixels, 0)
        self.assertIsNone(res_none_curr.roi_box)

        # None previous frame
        curr = np.zeros((100, 100, 3), dtype=np.uint8)
        res_none_prev = detector.detect(None, curr)
        self.assertTrue(res_none_prev.has_changed)
        self.assertEqual(res_none_prev.roi_box, (0, 0, 100, 100))

        # 2D Grayscale frames
        g1 = np.zeros((200, 200), dtype=np.uint8)
        g2 = np.zeros((200, 200), dtype=np.uint8)
        g2[50:150, 50:150] = 255
        res_gray = detector.detect(g1, g2)
        self.assertTrue(res_gray.has_changed)
        self.assertIsNotNone(res_gray.roi_box)

        # 4-Channel BGRA frames
        bgra1 = np.zeros((200, 200, 4), dtype=np.uint8)
        bgra2 = np.zeros((200, 200, 4), dtype=np.uint8)
        bgra2[50:150, 50:150, :3] = 255
        res_bgra = detector.detect(bgra1, bgra2)
        self.assertTrue(res_bgra.has_changed)
        self.assertIsNotNone(res_bgra.roi_box)

        # Custom thresholds
        custom_det = _ProdFrameChangeDetector(step=2, pixel_threshold=10, min_changed_pixels=20)
        self.assertEqual(custom_det.step, 2)
        self.assertEqual(custom_det.pixel_threshold, 10)
        self.assertEqual(custom_det.min_changed_pixels, 20)

    def test_adaptive_polling_custom_and_reset(self):
        controller = _ProdAdaptivePollingController(
            active_interval=0.10,
            warm_interval=0.20,
            idle_interval=0.40,
            deep_idle_interval=0.60,
            warm_threshold=1,
            idle_threshold=3,
            deep_idle_threshold=10,
        )
        self.assertEqual(controller.state, _ProdPollingState.ACTIVE)
        self.assertEqual(controller.static_count, 0)

        # 1 static frame moves to WARM (warm_threshold=1)
        delay = controller.on_frame(has_changed=False)
        self.assertEqual(controller.state, _ProdPollingState.WARM)
        self.assertAlmostEqual(delay, 0.20)
        self.assertEqual(controller.static_count, 1)

        # 3 static frames moves to IDLE (idle_threshold=3)
        controller.on_frame(has_changed=False)
        delay = controller.on_frame(has_changed=False)
        self.assertEqual(controller.state, _ProdPollingState.IDLE)
        self.assertAlmostEqual(delay, 0.40)

        # Reset back to ACTIVE
        controller.reset()
        self.assertEqual(controller.state, _ProdPollingState.ACTIVE)
        self.assertEqual(controller.static_count, 0)

    def test_ocr_service_edge_cases_and_remapping(self):
        try:
            from app.ocr_engine import OcrLine
        except ImportError:
            OcrLine = None

        service = _ProdOcrService(padding=20, max_roi_ratio=0.5, health_check_interval=10)

        # Clamping at boundary
        clamped = service.compute_padded_roi((5, 5, 95, 95), 100, 100)
        self.assertEqual(clamped, (0, 0, 100, 100))

        # None / empty frame
        self.assertEqual(service.recognize_frame(None, (10, 10, 50, 50)), [])
        self.assertEqual(service.recognize_frame(np.zeros((0, 0, 3), dtype=np.uint8), (10, 10, 50, 50)), [])

        # Missing roi_box forces fallback
        dummy_frame = np.zeros((400, 400, 3), dtype=np.uint8)
        service.recognize_frame(dummy_frame, None)
        self.assertTrue(service.last_was_fallback)

        # Force full forces fallback
        service.recognize_frame(dummy_frame, (10, 10, 50, 50), force_full=True)
        self.assertTrue(service.last_was_fallback)

        # Test engine coordinate remapping with real OcrLine
        if OcrLine is not None:
            class MockEngine:
                def recognize(self, crop):
                    return [OcrLine("hello", 0.98, (5, 5, 45, 25))]

            engine_service = _ProdOcrService(ocr_engine=MockEngine(), padding=10)
            res_lines = engine_service.recognize_frame(dummy_frame, (100, 100, 200, 200))
            self.assertFalse(engine_service.last_was_fallback)
            self.assertEqual(len(res_lines), 1)
            # Padded box: 100-10=90, 100-10=90.
            # Remapped box: 5+90=95, 5+90=95, 45+90=135, 25+90=115
            self.assertEqual(res_lines[0].box, (95, 95, 135, 115))
            self.assertEqual(res_lines[0].text, "hello")
            self.assertAlmostEqual(res_lines[0].score, 0.98)

    def test_ocr_stabilizer_jitter_candidate_promotion_and_reset(self):
        stabilizer = _ProdOcrStabilizer(debounce_frames=2, max_jitter_dist=2)

        # Frame 1: establishes stable baseline
        c1, lines1 = stabilizer.process(["Hello World"])
        self.assertTrue(c1)
        self.assertEqual(lines1, ["Hello World"])

        # Frame 2: minor jitter 'Hello Wor1d' (edit distance 1 <= 2) -> suppressed!
        c2, lines2 = stabilizer.process(["Hello Wor1d"])
        self.assertFalse(c2)
        self.assertEqual(lines2, ["Hello World"])

        # Frame 3: same candidate 'Hello Wor1d' repeats 2nd time -> confirmed!
        c3, lines3 = stabilizer.process(["Hello Wor1d"])
        self.assertTrue(c3)
        self.assertEqual(lines3, ["Hello Wor1d"])

        # Frame 4: completely different text (distance >> 2) -> immediately confirmed
        c4, lines4 = stabilizer.process(["Completely different line"])
        self.assertTrue(c4)
        self.assertEqual(lines4, ["Completely different line"])

        # Reset
        stabilizer.reset()
        c5, lines5 = stabilizer.process(["Fresh start"])
        self.assertTrue(c5)
        self.assertEqual(lines5, ["Fresh start"])

    def test_ocr_stabilizer_consecutive_empty_frames_stabilize(self):
        """Verify empty frame transitions and that consecutive empty frames do not re-trigger change."""
        stabilizer = _ProdOcrStabilizer(debounce_frames=2, max_jitter_dist=2)

        # 1. Initial text
        c1, lines1 = stabilizer.process(["Active subtitle"])
        self.assertTrue(c1)
        self.assertEqual(lines1, ["Active subtitle"])

        # 2. Text cleared -> reports substantive change to clear display
        c2, lines2 = stabilizer.process([])
        self.assertTrue(c2)
        self.assertEqual(lines2, [])

        # 3. Subsequent consecutive empty frames -> must report False
        for _ in range(5):
            c_empty, lines_empty = stabilizer.process([])
            self.assertFalse(c_empty, "Consecutive empty frames must not report change")
            self.assertEqual(lines_empty, [])

        # 4. Text reappears -> reports substantive change
        c3, lines3 = stabilizer.process(["Next line"])
        self.assertTrue(c3)
        self.assertEqual(lines3, ["Next line"])

        # 5. Fresh stabilizer starting on empty screen
        s_fresh = _ProdOcrStabilizer()
        c_fresh1, _ = s_fresh.process([])
        self.assertTrue(c_fresh1, "Initial frame baseline establishes change")
        c_fresh2, _ = s_fresh.process([])
        self.assertFalse(c_fresh2, "Second empty frame must stabilize to False")

    def test_frame_detector_zero_threshold_and_dimension_safety(self):
        """Verify FrameChangeDetector handles zero thresholds and atypical inputs without ValueError."""
        # Zero thresholds on identical frames
        zero_det = _ProdFrameChangeDetector(min_changed_pixels=0, min_changed_ratio=0.0)
        f = np.zeros((100, 100, 3), dtype=np.uint8)
        res = zero_det.detect(f, f)
        self.assertFalse(res.has_changed)
        self.assertEqual(res.changed_pixels, 0)
        self.assertIsNone(res.roi_box)

        # Dtype mismatch returns has_changed=True without OpenCV error
        f_u8 = np.zeros((100, 100, 3), dtype=np.uint8)
        f_f32 = np.zeros((100, 100, 3), dtype=np.float32)
        res_dtype = zero_det.detect(f_u8, f_f32)
        self.assertTrue(res_dtype.has_changed)
        self.assertEqual(res_dtype.roi_box, (0, 0, 100, 100))

        # 1D array does not raise unpack error
        f_1d = np.zeros((10,), dtype=np.uint8)
        res_1d = zero_det.detect(f_1d, f_1d)
        self.assertFalse(res_1d.has_changed)
        self.assertIsNone(res_1d.roi_box)

    def test_levenshtein_distance_fast_length_delta_exit(self):
        """Verify Levenshtein distance and OcrStabilizer bypass quadratic stalls on large text."""
        from app.ocr_stabilizer import levenshtein_distance

        # Direct function fast length-delta exit
        t0 = time.perf_counter()
        dist = levenshtein_distance("a" * 2000, "a" * 2050, max_dist=2)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertGreater(dist, 2)
        self.assertLess(elapsed_ms, 1.0, f"Levenshtein length exit too slow: {elapsed_ms:.2f}ms")

        # Fast identity on 2,000-character strings (< 1.0ms)
        t_ident = time.perf_counter()
        dist_ident = levenshtein_distance("a" * 2000, "a" * 2000, max_dist=2)
        elapsed_ms_ident = (time.perf_counter() - t_ident) * 1000
        self.assertEqual(dist_ident, 0)
        self.assertLess(elapsed_ms_ident, 1.0, f"Identical Levenshtein too slow: {elapsed_ms_ident:.2f}ms")

        # 1-character substitution at the end on 2,000-character strings (< 1.0ms)
        t_sub_end = time.perf_counter()
        dist_sub_end = levenshtein_distance("a" * 2000, "a" * 1999 + "b", max_dist=2)
        elapsed_ms_sub_end = (time.perf_counter() - t_sub_end) * 1000
        self.assertEqual(dist_sub_end, 1)
        self.assertLess(elapsed_ms_sub_end, 1.0, f"1-sub end Levenshtein too slow: {elapsed_ms_sub_end:.2f}ms")

        # 1-character substitution in the middle on 2,000-character strings (< 1.0ms)
        t_sub_mid = time.perf_counter()
        dist_sub_mid = levenshtein_distance("a" * 2000, "a" * 1000 + "b" + "a" * 999, max_dist=2)
        elapsed_ms_sub_mid = (time.perf_counter() - t_sub_mid) * 1000
        self.assertEqual(dist_sub_mid, 1)
        self.assertLess(elapsed_ms_sub_mid, 1.0, f"1-sub middle Levenshtein too slow: {elapsed_ms_sub_mid:.2f}ms")

        # Bounded distance on large equal-length text
        t1 = time.perf_counter()
        dist_equal = levenshtein_distance("a" * 2000, "b" * 2000, max_dist=2)
        elapsed_ms_equal = (time.perf_counter() - t1) * 1000
        self.assertGreater(dist_equal, 2)
        self.assertLess(elapsed_ms_equal, 10.0, f"Bounded Levenshtein too slow: {elapsed_ms_equal:.2f}ms")

        # OcrStabilizer process with dense text
        stabilizer = _ProdOcrStabilizer(debounce_frames=2, max_jitter_dist=2)
        stabilizer.process(["a" * 2000])

        # Identical text fast-path in stabilizer (< 1.0ms)
        t_proc_ident = time.perf_counter()
        sub_ident, lines_ident = stabilizer.process(["a" * 2000])
        elapsed_proc_ident = (time.perf_counter() - t_proc_ident) * 1000
        self.assertFalse(sub_ident)
        self.assertEqual(lines_ident, ["a" * 2000])
        self.assertLess(elapsed_proc_ident, 1.0, f"OcrStabilizer identical text too slow: {elapsed_proc_ident:.2f}ms")

        # 1-character jitter filtering in stabilizer (< 1.0ms)
        t_proc_jitter = time.perf_counter()
        sub_jitter, lines_jitter = stabilizer.process(["a" * 1999 + "b"])
        elapsed_proc_jitter = (time.perf_counter() - t_proc_jitter) * 1000
        self.assertFalse(sub_jitter)
        self.assertEqual(lines_jitter, ["a" * 2000])
        self.assertLess(elapsed_proc_jitter, 1.0, f"OcrStabilizer jitter text too slow: {elapsed_proc_jitter:.2f}ms")

        # Length delta substantive change in stabilizer (< 1.0ms)
        t2 = time.perf_counter()
        substantive, _ = stabilizer.process(["a" * 2050])
        elapsed_proc_ms = (time.perf_counter() - t2) * 1000
        self.assertTrue(substantive)
        self.assertLess(elapsed_proc_ms, 1.0, f"OcrStabilizer dense text processing too slow: {elapsed_proc_ms:.2f}ms")

    def test_ocr_service_coordinate_clamping_to_frame_bounds(self):
        """Verify OcrService clamps remapped boxes to frame bounds and has no dummy stubs."""
        try:
            from app.ocr_engine import OcrLine
        except ImportError:
            OcrLine = None

        if OcrLine is not None:
            # Mock OCR returns bounding box extending past crop bounds
            def mock_overflow_ocr(crop):
                return [OcrLine("overflow", 0.95, (0, 0, crop.shape[1] + 50, crop.shape[0] + 50))]

            service = _ProdOcrService(mock_ocr_fn=mock_overflow_ocr, padding=24)
            frame = np.zeros((100, 100, 3), dtype=np.uint8)
            # ROI near corner
            lines = service.recognize_frame(frame, (80, 80, 100, 100))
            self.assertEqual(len(lines), 1)
            x1, y1, x2, y2 = lines[0].box
            self.assertGreaterEqual(x1, 0)
            self.assertGreaterEqual(y1, 0)
            self.assertLessEqual(x2, 100, f"x2={x2} exceeds frame width 100")
            self.assertLessEqual(y2, 100, f"y2={y2} exceeds frame height 100")

        # Verify no hardcoded dummy sample stub
        empty_service = _ProdOcrService()
        dummy_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        self.assertEqual(empty_service._call_ocr(dummy_frame), [])


if __name__ == "__main__":
    unittest.main()
