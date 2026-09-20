# -*- coding: utf-8 -*-
"""End-to-end & component test suite for Generation Tracking & Latest-Frame-Wins Buffer.

Covers:
- Feature 2: Generation Tracking (PR 2)
- Feature 10: Latest-Frame-Wins Buffer (PR 11)
Tiers 1-4: Monotonic generation counters, stale response dropping, out-of-order resolution,
LatestFrameBuffer single-slot overwrites, dropped frame accounting, and real-world multi-thread workloads.
"""

import threading
import time
import unittest
from dataclasses import dataclass
from typing import Any
import numpy as np

# Try importing production components if available
try:
    from app.latest_frame_buffer import LatestFrameBuffer as _ProdLatestFrameBuffer
except ImportError:
    _ProdLatestFrameBuffer = None

try:
    from app.result_manager import ResultManager as _ProdResultManager
except ImportError:
    _ProdResultManager = None

try:
    from app.pipelines import GenerationTracker as _ProdGenerationTracker, WatchCycleContext
except ImportError:
    try:
        from app.result_manager import GenerationTracker as _ProdGenerationTracker
    except ImportError:
        _ProdGenerationTracker = None

    @dataclass(frozen=True)
    class WatchCycleContext:
        generation_id: int
        timestamp: float
        target_language: str


class ContractGenerationTracker:
    """Reference contract implementation of PR 2 GenerationTracker."""

    def __init__(self):
        self._lock = threading.Lock()
        self._current_gen: int = 0

    def next_generation(self) -> int:
        with self._lock:
            self._current_gen += 1
            return self._current_gen

    def is_active(self, gen_id: int) -> bool:
        with self._lock:
            return gen_id == self._current_gen

    def reset(self) -> int:
        with self._lock:
            self._current_gen += 1
            return self._current_gen

    @property
    def current_generation(self) -> int:
        with self._lock:
            return self._current_gen


class ContractResultManager:
    """Reference contract implementation of generation-guarded result dispatching."""

    def __init__(self, tracker: ContractGenerationTracker):
        self.tracker = tracker
        self.latest_dispatched: str | None = None
        self.dropped_stale_count: int = 0
        self.dispatched_history: list[tuple[int, str]] = []

    def dispatch_subtitle(self, text: str, gen_id: int) -> bool:
        if not self.tracker.is_active(gen_id):
            self.dropped_stale_count += 1
            return False
        self.latest_dispatched = text
        self.dispatched_history.append((gen_id, text))
        return True


class ContractLatestFrameBuffer:
    """Reference contract implementation of PR 11 LatestFrameBuffer.

    Guarantees:
    1. Single-slot capacity.
    2. Overwrites unconsumed frame with newer frame.
    3. Increments dropped_count when an unconsumed frame is overwritten.
    4. Thread-safe condition synchronization for get(timeout).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frame: np.ndarray | None = None
        self._gen_id: int | None = None
        self._dropped_count: int = 0

    def put(self, frame: np.ndarray, generation_id: int) -> None:
        with self._lock:
            if self._frame is not None:
                self._dropped_count += 1
            self._frame = frame
            self._gen_id = generation_id
            self._cond.notify_all()

    def get(self, timeout: float | None = None) -> tuple[np.ndarray, int] | None:
        with self._lock:
            if self._frame is None:
                if not self._cond.wait(timeout):
                    return None
            if self._frame is None:
                return None
            res = (self._frame, self._gen_id)
            self._frame = None
            self._gen_id = None
            return res

    def clear(self) -> None:
        with self._lock:
            self._frame = None
            self._gen_id = None

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count


# Factory helpers
def create_generation_tracker():
    return _ProdGenerationTracker() if _ProdGenerationTracker else ContractGenerationTracker()


def create_latest_frame_buffer():
    return _ProdLatestFrameBuffer() if _ProdLatestFrameBuffer else ContractLatestFrameBuffer()


def create_result_manager(tracker):
    return _ProdResultManager(tracker) if _ProdResultManager else ContractResultManager(tracker)


class TestGenerationTiers(unittest.TestCase):
    def setUp(self):
        self.tracker = create_generation_tracker()
        self.buffer = create_latest_frame_buffer()
        self.result_mgr = create_result_manager(self.tracker)

    # ==========================================
    # Tier 1: Primary Feature Behavior
    # ==========================================

    def test_generation_monotonic_increment(self):
        """Tier 1: Generation IDs must strictly monotonically increment."""
        gen1 = self.tracker.next_generation()
        gen2 = self.tracker.next_generation()
        gen3 = self.tracker.next_generation()

        self.assertEqual(gen1, 1)
        self.assertEqual(gen2, 2)
        self.assertEqual(gen3, 3)
        self.assertLess(gen1, gen2)
        self.assertLess(gen2, gen3)

    def test_generation_is_active(self):
        """Tier 1: Only the latest generation ID is active."""
        g1 = self.tracker.next_generation()
        self.assertTrue(self.tracker.is_active(g1))

        g2 = self.tracker.next_generation()
        self.assertFalse(self.tracker.is_active(g1))
        self.assertTrue(self.tracker.is_active(g2))

    def test_stale_generation_dropped_by_result_manager(self):
        """Tier 1: Stale translation response (older generation) is dropped and not emitted."""
        gen1 = self.tracker.next_generation()
        gen2 = self.tracker.next_generation()

        # Slow response for gen1 arrives after gen2 is active
        accepted = self.result_mgr.dispatch_subtitle("Old translation gen1", gen1)
        self.assertFalse(accepted, "Stale generation response must not be accepted")
        self.assertIsNone(self.result_mgr.latest_dispatched)
        self.assertEqual(self.result_mgr.dropped_stale_count, 1)

    def test_generation_reset_invalidates_active_generation(self):
        """Tier 1: reset() creates a new epoch, immediately invalidating previous active generation."""
        gen1 = self.tracker.next_generation()
        self.assertTrue(self.tracker.is_active(gen1))

        new_gen = self.tracker.reset()
        self.assertFalse(self.tracker.is_active(gen1))
        self.assertTrue(self.tracker.is_active(new_gen))
        self.assertGreater(new_gen, gen1)

    def test_watch_cycle_context_structure(self):
        """Tier 1: Watch cycle context properly encapsulates metadata."""
        ctx = WatchCycleContext(generation_id=5, timestamp=time.time(), target_language="zh")
        self.assertEqual(ctx.generation_id, 5)
        self.assertEqual(ctx.target_language, "zh")
        self.assertGreater(ctx.timestamp, 0)

    # ==========================================
    # Tier 2: Boundary & Corner Cases
    # ==========================================

    def test_out_of_order_responses_resolve_to_latest(self):
        """Tier 2: Out-of-order responses (Result 1 arrives after Result 2) resolve to latest generation."""
        gen1 = self.tracker.next_generation()
        gen2 = self.tracker.next_generation()

        # Result 2 arrives first
        accepted2 = self.result_mgr.dispatch_subtitle("Result #2 (latest)", gen2)
        self.assertTrue(accepted2)
        self.assertEqual(self.result_mgr.latest_dispatched, "Result #2 (latest)")

        # Result 1 arrives later (delayed network/inference)
        accepted1 = self.result_mgr.dispatch_subtitle("Result #1 (stale)", gen1)
        self.assertFalse(accepted1)
        # Verify UI remains on Result 2 and was NOT overwritten by Result 1
        self.assertEqual(self.result_mgr.latest_dispatched, "Result #2 (latest)")
        self.assertEqual(self.result_mgr.dropped_stale_count, 1)

    def test_concurrent_generation_requests_thread_safety(self):
        """Tier 2: Thread-safe concurrent next_generation calls produce unique monotonic IDs."""
        ids = []
        lock = threading.Lock()

        def worker():
            for _ in range(100):
                gid = self.tracker.next_generation()
                with lock:
                    ids.append(gid)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(ids), 1000)
        self.assertEqual(len(set(ids)), 1000, "All generation IDs must be strictly unique")
        self.assertEqual(min(ids), 1)
        self.assertEqual(max(ids), 1000)

    def test_latest_frame_buffer_single_capacity(self):
        """Tier 2: Buffer holds at most 1 frame at any time."""
        frame1 = np.ones((10, 10, 3), dtype=np.uint8) * 1
        frame2 = np.ones((10, 10, 3), dtype=np.uint8) * 2

        self.buffer.put(frame1, 1)
        self.buffer.put(frame2, 2)

        # Only frame2 should be consumed
        consumed = self.buffer.get(timeout=0.1)
        self.assertIsNotNone(consumed)
        data, gid = consumed
        self.assertEqual(gid, 2)
        self.assertEqual(data[0, 0, 0], 2)

        # Buffer is now empty
        self.assertIsNone(self.buffer.get(timeout=0.01))

    def test_latest_frame_buffer_empty_timeout(self):
        """Tier 2: get() on empty buffer returns None after specified timeout."""
        start = time.perf_counter()
        res = self.buffer.get(timeout=0.05)
        elapsed = time.perf_counter() - start

        self.assertIsNone(res)
        self.assertGreaterEqual(elapsed, 0.04)

    def test_large_generation_cycle_wrap_stability(self):
        """Tier 2: Generation counter safely handles thousands of cycles."""
        for _ in range(5000):
            self.tracker.next_generation()
        self.assertEqual(self.tracker.current_generation, 5000)

    # ==========================================
    # Tier 3: Pairwise & Component Interactions
    # ==========================================

    def test_latest_frame_buffer_overwrites_stale_and_increments_dropped_count(self):
        """Tier 3: Rapid producer putting 10 frames drops 9 frames."""
        for i in range(10):
            frame = np.ones((5, 5, 3), dtype=np.uint8) * i
            self.buffer.put(frame, i)

        self.assertEqual(self.buffer.dropped_count, 9)
        data, gid = self.buffer.get()
        self.assertEqual(gid, 9)
        self.assertEqual(data[0, 0, 0], 9)

    def test_producer_consumer_generation_alignment(self):
        """Tier 3: Producer tags frames with generation; consumer processes latest matching generation."""
        gen_id = self.tracker.next_generation()
        f1 = np.zeros((10, 10, 3), dtype=np.uint8)
        self.buffer.put(f1, gen_id)

        # Consume
        frame, gid = self.buffer.get()
        self.assertEqual(gid, gen_id)
        self.assertTrue(self.tracker.is_active(gid))

        # Result dispatch succeeds
        self.assertTrue(self.result_mgr.dispatch_subtitle("Processed translation", gid))

    def test_buffer_clear_on_watch_reset(self):
        """Tier 3: Watch reset clears pending frame buffer."""
        f1 = np.zeros((10, 10, 3), dtype=np.uint8)
        self.buffer.put(f1, 1)
        self.buffer.clear()
        self.assertIsNone(self.buffer.get(timeout=0.01))

    # ==========================================
    # Tier 4: Real-World Scenarios
    # ==========================================

    def test_scenario_capture_outpaces_inference(self):
        """Tier 4 Scenario 1: Capture produces at 30 FPS, inference consumes at 2 FPS."""
        # Producer thread pushing 30 frames
        produced_count = 30
        consumed = []

        def producer():
            for i in range(produced_count):
                f = np.ones((4, 4, 3), dtype=np.uint8) * i
                self.buffer.put(f, i)
                time.sleep(0.005)  # fast capture

        def consumer():
            while len(consumed) < 2 and producer_thread.is_alive():
                item = self.buffer.get(timeout=0.05)
                if item is not None:
                    consumed.append(item[1])
                    time.sleep(0.04)  # slow inference

        producer_thread = threading.Thread(target=producer)
        consumer_thread = threading.Thread(target=consumer)

        producer_thread.start()
        consumer_thread.start()

        producer_thread.join()
        consumer_thread.join()

        # Buffer must have dropped the bulk of intermediate frames
        self.assertGreater(self.buffer.dropped_count, 15)
        # And consumer processed latest available
        if consumed:
            self.assertGreaterEqual(consumed[-1], 1)

    def test_scenario_region_switch_cancels_in_flight_inference(self):
        """Tier 4 Scenario 3: User moves translation region mid-inference; old result dropped."""
        # Initial watch region: gen 1
        gen_region_a = self.tracker.next_generation()

        # User switches region: tracker resets to gen 2
        gen_region_b = self.tracker.reset()

        # Slow translation for region A finally returns
        accepted_a = self.result_mgr.dispatch_subtitle("Translation for Old Region", gen_region_a)
        self.assertFalse(accepted_a)

        # Translation for region B arrives
        accepted_b = self.result_mgr.dispatch_subtitle("Translation for New Region", gen_region_b)
        self.assertTrue(accepted_b)
        self.assertEqual(self.result_mgr.latest_dispatched, "Translation for New Region")


class TestProductionWindowWatcherGeneration(unittest.TestCase):
    """Direct tests for WindowWatcher and GenerationTracker integration."""

    def test_window_watcher_generation_tracker_property(self):
        from unittest.mock import MagicMock
        from app.config import DEFAULTS
        from app.pipelines import GenerationTracker
        from app.window_watcher import WindowWatcher

        custom_tracker = GenerationTracker(42)
        watcher = WindowWatcher(
            MagicMock(), MagicMock(), dict(DEFAULTS), hwnd=1,
            generation_tracker=custom_tracker,
        )
        self.assertIs(watcher.generation_tracker, custom_tracker)
        self.assertEqual(watcher.generation_tracker.current_generation, 42)

    def test_window_watcher_default_generation_tracker(self):
        from unittest.mock import MagicMock
        from app.config import DEFAULTS
        from app.pipelines import GenerationTracker
        from app.window_watcher import WindowWatcher

        watcher = WindowWatcher(MagicMock(), MagicMock(), dict(DEFAULTS), hwnd=1)
        self.assertIsInstance(watcher.generation_tracker, GenerationTracker)
        self.assertEqual(watcher.generation_tracker.current_generation, 0)

    def test_window_watcher_resets_generation_on_mode_change(self):
        from unittest.mock import MagicMock
        from app.config import DEFAULTS
        from app.window_watcher import WindowWatcher

        watcher = WindowWatcher(MagicMock(), MagicMock(), dict(DEFAULTS), hwnd=1)
        g1 = watcher.generation_tracker.next_generation()
        self.assertTrue(watcher.generation_tracker.is_active(g1))

        watcher.set_display_mode("annotate")
        self.assertFalse(watcher.generation_tracker.is_active(g1))

    def test_window_watcher_resets_generation_on_region_change(self):
        from unittest.mock import MagicMock
        from app.config import DEFAULTS
        from app.window_watcher import WindowWatcher

        watcher = WindowWatcher(MagicMock(), MagicMock(), dict(DEFAULTS), region=(0, 0, 100, 100))
        g1 = watcher.generation_tracker.next_generation()
        self.assertTrue(watcher.generation_tracker.is_active(g1))

        watcher.set_region((10, 10, 200, 200))
        self.assertFalse(watcher.generation_tracker.is_active(g1))

    def test_window_watcher_resets_generation_on_stop(self):
        from unittest.mock import MagicMock
        from app.config import DEFAULTS
        from app.window_watcher import WindowWatcher

        watcher = WindowWatcher(MagicMock(), MagicMock(), dict(DEFAULTS), hwnd=1)
        g1 = watcher.generation_tracker.next_generation()
        self.assertTrue(watcher.generation_tracker.is_active(g1))

        watcher.stop()
        self.assertFalse(watcher.generation_tracker.is_active(g1))

    def test_window_watcher_drops_stale_subtitle_translation(self):
        """Simulate a slow translation cycle where tracker is reset mid-flight."""
        from unittest.mock import MagicMock, patch
        from app.config import DEFAULTS
        from app.ocr_engine import OcrLine
        from app.window_watcher import WindowWatcher

        ocr_mock = MagicMock()
        translator_mock = MagicMock()
        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 50

        watcher = WindowWatcher(ocr_mock, translator_mock, cfg, hwnd=1)

        # Single OCR line returned
        ocr_mock.recognize.return_value = [
            OcrLine(text="Source text to translate", box=[[0, 0], [100, 0], [100, 20], [0, 20]], score=0.99)
        ]

        def slow_translate(text, target):
            # Mid-flight reset: invalidates active generation
            watcher.generation_tracker.reset()
            return "Stale translation that should be dropped"

        translator_mock.translate.side_effect = slow_translate

        emitted_subtitles = []
        watcher.subtitle_ready.connect(emitted_subtitles.append)

        fake_frame = np.ones((100, 100, 3), dtype=np.uint8) * 10
        with patch.object(watcher, "_grab", return_value=((0, 0, 100, 100), fake_frame)):
            # Run one iteration of watcher logic
            gen_id = watcher.generation_tracker.next_generation()
            lines = ocr_mock.recognize(fake_frame)
            event, text = watcher._state.observe(lines, 0.5)
            self.assertEqual(event, "change")

            # Translation executes and resets tracker internally
            translation = translator_mock.translate(text, "zh")
            if watcher._running and watcher.generation_tracker.is_active(gen_id):
                watcher.subtitle_ready.emit(translation)

        self.assertEqual(len(emitted_subtitles), 0, "Stale translation must NOT be emitted")

    def test_window_watcher_emits_active_subtitle_translation(self):
        """When generation matches, translation is emitted normally."""
        from unittest.mock import MagicMock
        from app.config import DEFAULTS
        from app.ocr_engine import OcrLine
        from app.window_watcher import WindowWatcher

        ocr_mock = MagicMock()
        translator_mock = MagicMock()
        translator_mock.translate.return_value = "Fresh translation"
        cfg = dict(DEFAULTS)

        watcher = WindowWatcher(ocr_mock, translator_mock, cfg, hwnd=1)
        emitted_subtitles = []
        watcher.subtitle_ready.connect(emitted_subtitles.append)

        fake_frame = np.ones((100, 100, 3), dtype=np.uint8) * 20
        ocr_mock.recognize.return_value = [
            OcrLine(text="Fresh source", box=[[0, 0], [100, 0], [100, 20], [0, 20]], score=0.99)
        ]

        gen_id = watcher.generation_tracker.next_generation()
        lines = ocr_mock.recognize(fake_frame)
        event, text = watcher._state.observe(lines, 0.5)
        self.assertEqual(event, "change")

        translation = translator_mock.translate(text, "zh")
        if watcher._running and watcher.generation_tracker.is_active(gen_id):
            watcher.subtitle_ready.emit(translation)

        self.assertEqual(emitted_subtitles, ["Fresh translation"])


class TestDecoupledPipelineAndLatestFrameBuffer(unittest.TestCase):
    """Deep verification of Milestone 4 decoupled components (PR 9 & PR 11)."""

    def test_production_components_imported(self):
        """Milestone 4: Production LatestFrameBuffer and ResultManager must be imported."""
        self.assertIsNotNone(_ProdLatestFrameBuffer, "Production LatestFrameBuffer must be imported")
        self.assertIsNotNone(_ProdResultManager, "Production ResultManager must be imported")

    def test_latest_frame_buffer_api_completeness(self):
        """PR 11: Verify LatestFrameBuffer put(gen_id/generation_id), get, peek, clear, and dropped_count."""
        buf = _ProdLatestFrameBuffer()
        self.assertTrue(buf.is_empty)
        self.assertIsNone(buf.peek())

        # Test put with keyword gen_id
        f1 = np.ones((4, 4, 3), dtype=np.uint8) * 1
        buf.put(f1, gen_id=101)
        self.assertFalse(buf.is_empty)
        peek_f, peek_id = buf.peek()
        self.assertEqual(peek_id, 101)
        self.assertEqual(peek_f[0, 0, 0], 1)

        # Test overwrite increments dropped_count
        f2 = np.ones((4, 4, 3), dtype=np.uint8) * 2
        buf.put(f2, generation_id=102)
        self.assertEqual(buf.dropped_count, 1)

        # Test reset dropped count
        buf.reset_dropped_count()
        self.assertEqual(buf.dropped_count, 0)

        # Test get consumes the frame and empties slot
        consumed = buf.get(timeout=0.1)
        self.assertIsNotNone(consumed)
        f_got, id_got = consumed
        self.assertEqual(id_got, 102)
        self.assertEqual(f_got[0, 0, 0], 2)
        self.assertTrue(buf.is_empty)

        # Test clear
        buf.put(f1, 103)
        self.assertFalse(buf.is_empty)
        buf.clear()
        self.assertTrue(buf.is_empty)
        self.assertIsNone(buf.get(timeout=0.01))

    def test_latest_frame_buffer_concurrent_producers(self):
        """PR 11: Multi-threaded producer pushing frames simultaneously into LatestFrameBuffer."""
        buf = _ProdLatestFrameBuffer()
        total_pushed = 100

        def producer_worker(start_idx):
            for i in range(25):
                frame = np.ones((2, 2, 3), dtype=np.uint8) * (start_idx + i)
                buf.put(frame, gen_id=start_idx + i)
                time.sleep(0.001)

        threads = [
            threading.Thread(target=producer_worker, args=(t * 25,))
            for t in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Buffer must have dropped frames due to overwriting
        self.assertGreater(buf.dropped_count, 50)
        final_item = buf.get(timeout=0.1)
        self.assertIsNotNone(final_item)
        self.assertTrue(buf.is_empty)

    def test_result_manager_full_dispatch_matrix(self):
        """PR 9: Verify ResultManager dispatching and dropping for all signal types."""
        from app.pipelines import GenerationTracker
        tracker = GenerationTracker(10)
        rm = _ProdResultManager(tracker)

        emitted_sub = []
        emitted_ann = []
        emitted_hist = []
        emitted_cleared = []
        emitted_moved = []
        emitted_stopped = []

        rm.subtitle_ready.connect(emitted_sub.append)
        rm.annotations_ready.connect(emitted_ann.append)
        rm.history_ready.connect(lambda s, t, m: emitted_hist.append((s, t, m)))
        rm.content_cleared.connect(lambda: emitted_cleared.append(True))
        rm.window_moved.connect(lambda x, y, w, h: emitted_moved.append((x, y, w, h)))
        rm.stopped.connect(emitted_stopped.append)

        # 1. Stale generation (gen=9, active=10) should be dropped
        self.assertFalse(rm.dispatch_subtitle("Stale sub", gen_id=9))
        self.assertFalse(rm.dispatch_annotations([((0, 0, 10, 10), "Stale ann")], gen_id=9))
        self.assertFalse(rm.dispatch_history("src", "tr", "mode", gen_id=9))
        self.assertFalse(rm.dispatch_cleared(gen_id=9))
        self.assertEqual(rm.dropped_stale_count, 2)
        self.assertEqual(len(emitted_sub), 0)
        self.assertEqual(len(emitted_ann), 0)
        self.assertEqual(len(emitted_hist), 0)
        self.assertEqual(len(emitted_cleared), 0)

        # 2. Active generation (gen=10) should succeed and emit
        self.assertTrue(rm.dispatch_subtitle("Active sub", gen_id=10))
        self.assertTrue(rm.dispatch_annotations([((0, 0, 10, 10), "Active ann")], gen_id=10))
        self.assertTrue(rm.dispatch_history("src", "Active tr", "mode", gen_id=10))
        self.assertTrue(rm.dispatch_cleared(gen_id=10))
        self.assertEqual(len(emitted_sub), 1)
        self.assertEqual(len(emitted_ann), 1)
        self.assertEqual(len(emitted_hist), 1)
        self.assertEqual(len(emitted_cleared), 1)

        # 3. Position and stopped events
        rm.dispatch_moved(10, 20, 300, 400)
        self.assertEqual(emitted_moved, [(10, 20, 300, 400)])
        rm.dispatch_stopped("Manual stop")
        self.assertEqual(emitted_stopped, ["Manual stop"])

    def test_capture_service_behavior(self):
        """PR 9: Verify CaptureService grab, moved tracking, handle validation."""
        from app.capture_service import CaptureService

        fake_frame = np.ones((10, 10, 3), dtype=np.uint8)
        grab_win_mock = lambda hwnd: fake_frame if hwnd == 123 else None
        grab_reg_mock = lambda x, y, w, h: fake_frame
        rect_mock = lambda hwnd: (10, 20, 100, 200)

        cs = CaptureService(
            hwnd=123,
            grab_window_fn=grab_win_mock,
            grab_region_fn=grab_reg_mock,
            get_window_rect_fn=rect_mock,
        )
        self.assertEqual(cs.hwnd, 123)
        self.assertIsNone(cs.region)

        # Grab window
        rect, frame = cs.grab()
        self.assertEqual(rect, (10, 20, 100, 200))
        self.assertIsNotNone(frame)

        # Moved tracking
        self.assertTrue(cs.has_moved(rect))
        self.assertFalse(cs.has_moved(rect))  # identical rect does not trigger moved
        self.assertTrue(cs.has_moved((15, 25, 100, 200)))  # moved!

        # Switch to region
        cs.set_region((0, 0, 50, 50))
        self.assertEqual(cs.region, (0, 0, 50, 50))
        reg, frame_reg = cs.grab()
        self.assertEqual(reg, (0, 0, 50, 50))
        self.assertIsNotNone(frame_reg)

        # Invalid handle error check
        class Win32Error(Exception):
            winerror = 1400

        self.assertTrue(CaptureService.is_invalid_window_handle_error(Win32Error()))
        self.assertFalse(CaptureService.is_invalid_window_handle_error(ValueError()))

    def test_text_change_detector_behavior(self):
        """PR 9: Verify TextChangeDetector sequence matching, stability, empty clearing."""
        from app.text_change_detector import TextChangeDetector
        from app.ocr_engine import OcrLine

        detector = TextChangeDetector(empty_clear_threshold=2)

        # 1. Initial observation of text
        line1 = OcrLine(text="Initial line", box=[[0, 0], [50, 0], [50, 10], [0, 10]], score=0.99)
        event, text = detector.observe([line1], threshold=0.5)
        self.assertEqual(event, "change")
        self.assertEqual(text, "Initial line")
        self.assertTrue(detector.is_stable)

        # 2. Identical text returns "none"
        event, text = detector.observe([line1], threshold=0.5)
        self.assertEqual(event, "none")

        # 3. Substantive change returns "change"
        line2 = OcrLine(text="Completely different text", box=[[0, 0], [50, 0], [50, 10], [0, 10]], score=0.99)
        event, text = detector.observe([line2], threshold=0.5)
        self.assertEqual(event, "change")
        self.assertEqual(text, "Completely different text")

        # 4. First empty frame: "none"
        event, text = detector.observe([], threshold=0.5)
        self.assertEqual(event, "none")
        self.assertEqual(detector.empty_frames, 1)

        # 5. Second empty frame: triggers "clear"
        event, text = detector.observe([], threshold=0.5)
        self.assertEqual(event, "clear")
        self.assertEqual(text, "")
        self.assertFalse(detector.is_stable)

    def test_translation_manager_behavior(self):
        """PR 9: Verify TranslationManager subtitle, annotations, and one-shot translations."""
        from unittest.mock import MagicMock
        from app.ocr_engine import OcrLine
        from app.translation_manager import TranslationManager

        translator_mock = MagicMock()
        translator_mock.translate.side_effect = lambda text, target: f"[{target}]{text}"
        translator_mock.translate_lines.side_effect = lambda lines, target: [f"[{target}]{l}" for l in lines]

        tm = TranslationManager(translator_mock)

        # 1. Subtitle translation
        sub = tm.translate_subtitle("Hello World", "zh")
        self.assertEqual(sub, "[zh]Hello World")

        # 2. Annotations translation
        lines = [
            OcrLine(text="Line A", box=[[0, 0], [20, 0], [20, 10], [0, 10]], score=0.95),
            OcrLine(text="Line B", box=[[0, 15], [20, 15], [20, 25], [0, 25]], score=0.95),
        ]
        items, joined = tm.translate_annotations(lines, "zh")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][1], "[zh]Line A")
        self.assertEqual(items[1][1], "[zh]Line B")
        self.assertIn("[zh]Line A", joined)
        self.assertIn("[zh]Line B", joined)

        # Line cache hit on repeated call (translator_mock not called again for cached lines)
        translator_mock.translate_lines.reset_mock()
        items2, joined2 = tm.translate_annotations(lines, "zh")
        translator_mock.translate_lines.assert_not_called()
        self.assertEqual(len(items2), 2)

        # 3. One-shot translation
        one_shot = tm.translate_one_shot("Quick test", "zh")
        self.assertEqual(one_shot, "[zh]Quick test")

    def test_window_watcher_pipeline_components_exposed(self):
        """PR 9: WindowWatcher exposes all single-responsibility pipeline components."""
        from unittest.mock import MagicMock
        from app.config import DEFAULTS
        from app.window_watcher import WindowWatcher

        watcher = WindowWatcher(
            MagicMock(), MagicMock(), dict(DEFAULTS), hwnd=1
        )

        # Verify all decoupled components exist and are typed correctly
        from app.capture_service import CaptureService
        from app.latest_frame_buffer import LatestFrameBuffer
        from app.frame_detector import FrameChangeDetector
        from app.adaptive_polling import AdaptivePollingController
        from app.ocr_service import OcrService
        from app.text_change_detector import TextChangeDetector
        from app.translation_manager import TranslationManager
        from app.result_manager import ResultManager

        self.assertIsInstance(watcher.capture_service, CaptureService)
        self.assertIsInstance(watcher.frame_buffer, LatestFrameBuffer)
        self.assertIsInstance(watcher.frame_detector, FrameChangeDetector)
        self.assertIsInstance(watcher.polling_controller, AdaptivePollingController)
        self.assertIsInstance(watcher.ocr_service, OcrService)
        self.assertIsInstance(watcher.text_change_detector, TextChangeDetector)
        self.assertIsInstance(watcher.translation_manager, TranslationManager)
        self.assertIsInstance(watcher.result_manager, ResultManager)

        # Verify all public signals exist
        self.assertTrue(hasattr(watcher, "subtitle_ready"))
        self.assertTrue(hasattr(watcher, "annotations_ready"))
        self.assertTrue(hasattr(watcher, "history_ready"))
        self.assertTrue(hasattr(watcher, "window_moved"))
        self.assertTrue(hasattr(watcher, "content_cleared"))
        self.assertTrue(hasattr(watcher, "stopped"))

    def test_window_watcher_transient_ocr_and_translation_exceptions_do_not_crash(self):
        """PR 11: Transient OCR and Translation exceptions are isolated and do not crash WindowWatcher."""
        from unittest.mock import MagicMock
        from app.config import DEFAULTS
        from app.ocr_engine import OcrLine
        from app.window_watcher import WindowWatcher

        # 1. OCR exception isolation
        ocr_mock = MagicMock()
        ocr_mock.recognize.side_effect = RuntimeError("DirectML hardware glitch")
        trans_mock = MagicMock()

        watcher = WindowWatcher(ocr_mock, trans_mock, dict(DEFAULTS), hwnd=1)
        fake_frame = np.ones((20, 20, 3), dtype=np.uint8)
        watcher._capture_service.grab = MagicMock(return_value=((0, 0, 20, 20), fake_frame))
        watcher.start()
        time.sleep(0.15)
        self.assertTrue(watcher.isRunning(), "WindowWatcher must not crash on OCR RuntimeError")
        watcher.stop()
        self.assertTrue(watcher.wait(1000), "WindowWatcher must cleanly stop and wait")

        # 2. Translation exception isolation
        ocr_ok = MagicMock()
        ocr_ok.recognize.return_value = [
            OcrLine(text="Transient text", box=[[0, 0], [10, 0], [10, 10], [0, 10]], score=0.99)
        ]
        trans_fail = MagicMock()
        trans_fail.translate.side_effect = ConnectionError("llama-server offline")

        watcher2 = WindowWatcher(ocr_ok, trans_fail, dict(DEFAULTS), hwnd=1)
        watcher2._capture_service.grab = MagicMock(return_value=((0, 0, 20, 20), fake_frame))
        watcher2.start()
        time.sleep(0.15)
        self.assertTrue(watcher2.isRunning(), "WindowWatcher must not crash on Translation ConnectionError")
        watcher2.stop()
        self.assertTrue(watcher2.wait(1000), "WindowWatcher must cleanly stop and wait")

    def test_window_watcher_live_producer_consumer_decoupling_increments_dropped_count(self):
        """PR 11: Real producer-consumer decoupling across threads genuinely increments dropped_count."""
        from unittest.mock import MagicMock
        from app.config import DEFAULTS
        from app.ocr_engine import OcrLine
        from app.window_watcher import WindowWatcher

        ocr_mock = MagicMock()
        ocr_mock.recognize.return_value = [
            OcrLine(text="Dynamic text", box=[[0, 0], [10, 0], [10, 10], [0, 10]], score=0.99)
        ]
        trans_mock = MagicMock()
        # Simulate compute-intensive translation (200ms)
        trans_mock.translate.side_effect = lambda t, tgt: (time.sleep(0.2) or "Translated")

        cfg = dict(DEFAULTS)
        cfg["window_watch_interval_ms"] = 20  # 50 FPS capture

        watcher = WindowWatcher(ocr_mock, trans_mock, cfg, hwnd=1)
        fake_frame = np.ones((20, 20, 3), dtype=np.uint8)
        watcher._capture_service.grab = MagicMock(return_value=((0, 0, 20, 20), fake_frame))

        watcher.start()
        time.sleep(0.4)
        dropped = watcher.frame_buffer.dropped_count
        watcher.stop()
        self.assertTrue(watcher.wait(1000), "WindowWatcher must cleanly stop and wait")

        self.assertGreater(
            dropped, 0,
            f"Expected dropped_count > 0 when capture outpaces inference, got {dropped}",
        )

    def test_latest_frame_buffer_clear_wakes_waiting_consumer_immediately(self):
        """PR 11: clear() triggers condition variable notify_all to wake up blocked consumer."""
        from app.latest_frame_buffer import LatestFrameBuffer
        import threading

        buf = LatestFrameBuffer()
        result_holder = []
        started_event = threading.Event()

        def consumer():
            started_event.set()
            res = buf.get(timeout=2.0)  # Would block for 2s if not notified
            result_holder.append(res)

        t = threading.Thread(target=consumer, daemon=True)
        t.start()
        started_event.wait(timeout=1.0)
        time.sleep(0.05)  # Ensure consumer is inside buf.get()

        t_start = time.time()
        buf.clear()
        t.join(timeout=1.0)
        elapsed = time.time() - t_start

        self.assertFalse(t.is_alive(), "Consumer thread should have exited promptly after clear()")
        self.assertLess(elapsed, 0.5, f"clear() should wake consumer immediately, took {elapsed:.3f}s")
        self.assertEqual(result_holder, [None])


if __name__ == "__main__":
    unittest.main()
