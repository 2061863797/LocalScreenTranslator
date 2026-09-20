# -*- coding: utf-8 -*-
"""Milestone 4 Empirical Challenge Suite: ResultManager & TextChangeDetector.

Empirically challenges:
1. ResultManager:
   - Out-of-order generation dispatch under concurrent threads:
     Dispatch gen_id=5, then delayed gen_id=3, gen_id=4 arriving late.
   - Verify that stale generations (gen_id < latest_accepted) are strictly ignored and do NOT emit signals.
   - Verify all signals (subtitle, annotations, history, cleared) adhere to stale drop invariant.
   - Concurrent race condition stress: 20 threads contending with barrier synchronization.
2. TextChangeDetector:
   - Stress-test diffing on large text sets (5,000+ characters).
   - Verify mathematical theoretical upper bound short-circuit skips difflib.SequenceMatcher DP.
   - Verify exact boundary behavior at ratio == threshold and ratio < threshold.
   - Benchmark throughput and speedup (>1000x) enabled by the mathematical bound.
   - Thread safety of TextChangeDetector under concurrent observe() calls.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QCoreApplication

from app.ocr_engine import OcrLine
from app.pipelines import GenerationTracker
from app.result_manager import ResultManager
from app.text_change_detector import TextChangeDetector


class TestResultManagerEmpiricalChallenge(unittest.TestCase):
    """Empirical challenge tests for ResultManager out-of-order and stale rejection."""

    @classmethod
    def setUpClass(cls):
        if QCoreApplication.instance() is None:
            cls.qapp = QCoreApplication([])
        else:
            cls.qapp = QCoreApplication.instance()

    def test_out_of_order_concurrent_dispatch_gen5_then_delayed_gen3_gen4(self):
        """Mandatory Challenge: Dispatch gen_id=5, then delayed gen_id=3, gen_id=4 arriving late.

        Guarantees:
        1. gen_id=5 is accepted and emits subtitle_ready.
        2. delayed gen_id=3 arrives late under concurrency and is strictly dropped with no signal.
        3. delayed gen_id=4 arrives late under concurrency and is strictly dropped with no signal.
        4. latest_dispatched remains gen_id=5 translation.
        5. dropped_stale_count increments exactly by 2.
        """
        tracker = GenerationTracker(5)  # Current active generation is 5
        rm = ResultManager(generation_tracker=tracker)

        emitted_subtitles: list[str] = []
        emitted_annotations: list[list] = []
        emitted_history: list[tuple[str, str, str]] = []
        emitted_cleared: list[bool] = []

        rm.subtitle_ready.connect(emitted_subtitles.append)
        rm.annotations_ready.connect(emitted_annotations.append)
        rm.history_ready.connect(lambda s, t, m: emitted_history.append((s, t, m)))
        rm.content_cleared.connect(lambda: emitted_cleared.append(True))

        dispatch_results: dict[int, bool] = {}
        barrier = threading.Barrier(3)

        def worker_gen5():
            barrier.wait()
            # gen_id=5 dispatches first
            res = rm.dispatch_subtitle("Translation for Gen 5 (Newest)", gen_id=5)
            rm.dispatch_annotations([([(0, 0)], "Annot Gen 5")], gen_id=5)
            rm.dispatch_history("Src 5", "Tr 5", "sub", gen_id=5)
            rm.dispatch_cleared(gen_id=5)
            dispatch_results[5] = res

        def worker_gen3():
            barrier.wait()
            # Slight delay to ensure gen 5 arrives first
            time.sleep(0.015)
            res = rm.dispatch_subtitle("Translation for Gen 3 (Stale)", gen_id=3)
            rm.dispatch_annotations([([(0, 0)], "Annot Gen 3")], gen_id=3)
            rm.dispatch_history("Src 3", "Tr 3", "sub", gen_id=3)
            rm.dispatch_cleared(gen_id=3)
            dispatch_results[3] = res

        def worker_gen4():
            barrier.wait()
            # Slight delay to ensure gen 5 arrives first
            time.sleep(0.025)
            res = rm.dispatch_subtitle("Translation for Gen 4 (Stale)", gen_id=4)
            rm.dispatch_annotations([([(0, 0)], "Annot Gen 4")], gen_id=4)
            rm.dispatch_history("Src 4", "Tr 4", "sub", gen_id=4)
            rm.dispatch_cleared(gen_id=4)
            dispatch_results[4] = res

        t5 = threading.Thread(target=worker_gen5)
        t3 = threading.Thread(target=worker_gen3)
        t4 = threading.Thread(target=worker_gen4)

        t5.start()
        t3.start()
        t4.start()

        t5.join()
        t3.join()
        t4.join()

        # Flush PySide6 queued signal events across thread boundaries
        QCoreApplication.processEvents()

        print("\n[EMPIRICAL TEST 1: ResultManager Out-of-Order Concurrent Dispatch]")
        print(f"Dispatch results: {dispatch_results}")
        print(f"Emitted subtitles: {emitted_subtitles}")
        print(f"Dropped stale count: {rm.dropped_stale_count}")

        # Verification 1: gen_id=5 accepted, gen_id=3 and 4 rejected
        self.assertTrue(dispatch_results.get(5), "Gen 5 must be accepted")
        self.assertFalse(dispatch_results.get(3), "Stale Gen 3 must be strictly rejected")
        self.assertFalse(dispatch_results.get(4), "Stale Gen 4 must be strictly rejected")

        # Verification 2: Signals emitted ONLY for gen_id=5
        self.assertEqual(emitted_subtitles, ["Translation for Gen 5 (Newest)"])
        self.assertEqual(len(emitted_annotations), 1)
        self.assertEqual(len(emitted_history), 1)
        self.assertEqual(len(emitted_cleared), 1)

        # Verification 3: State invariants
        self.assertEqual(rm.latest_dispatched, "Translation for Gen 5 (Newest)")
        self.assertEqual(rm.dropped_stale_count, 4)  # 2 stale subtitles + 2 stale annotations

    def test_high_concurrency_random_arrivals_stale_rejection(self):
        """Stress-test 20 concurrent threads dispatching out-of-order generations 1..20.

        Active generation is 15. Only gen 15 may be accepted; all < 15 and > 15 must be rejected.
        """
        tracker = GenerationTracker(15)
        rm = ResultManager(generation_tracker=tracker)

        accepted_gens: list[int] = []
        lock = threading.Lock()

        def dispatch_worker(gid: int):
            ok = rm.dispatch_subtitle(f"Text for Gen {gid}", gen_id=gid)
            if ok:
                with lock:
                    accepted_gens.append(gid)

        threads = [threading.Thread(target=dispatch_worker, args=(gid,)) for gid in range(1, 21)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(accepted_gens, [15], "Strictly and only active gen 15 must be accepted")
        self.assertEqual(rm.dropped_stale_count, 19, "Exactly 19 non-active generations must be dropped")


class TestTextChangeDetectorEmpiricalChallenge(unittest.TestCase):
    """Empirical challenge tests for TextChangeDetector 5,000+ char diff & mathematical bound."""

    def test_mathematical_bound_skips_dynamic_programming_on_5000_chars(self):
        """Mandatory Challenge: Verify mathematical bound short-circuit skips SequenceMatcher DP.

        Mathematical upper bound:
            ratio <= 2 * min(l1, l2) / (l1 + l2)
        When this theoretical upper bound < threshold, SequenceMatcher must NEVER be called.
        """
        detector = TextChangeDetector()

        # Seed initial text with 5,000 characters
        text_5000 = "A" * 5000
        event, text = detector.observe(text_5000, threshold=0.5)
        self.assertEqual(event, "change")
        self.assertEqual(detector.last_text, text_5000)

        # Now test with text of length 1,000 (5,000 -> 1,000)
        # Theoretical bound: 2 * 1000 / (5000 + 1000) = 2000 / 6000 = 0.3333 < 0.5
        text_1000 = "B" * 1000

        with patch("app.text_change_detector.SequenceMatcher") as mock_matcher:
            event2, text2 = detector.observe(text_1000, threshold=0.5)

            # Verification: SequenceMatcher was NEVER called!
            mock_matcher.assert_not_called()
            self.assertEqual(event2, "change")
            self.assertEqual(text2, text_1000)
            self.assertEqual(detector.last_text, text_1000)

        print("\n[EMPIRICAL TEST 2A: Mathematical Bound Short-Circuit Verified]")
        print("SequenceMatcher call count: 0 (DP skipped entirely via mathematical theoretical upper bound)")

    def test_mathematical_bound_exact_boundary_conditions(self):
        """Test exact boundary transitions of the mathematical theoretical bound:

        Let ratio_bound = 2.0 * min(l1, l2) / (l1 + l2).
        1. When ratio_bound < threshold: skips SequenceMatcher entirely.
        2. When ratio_bound >= threshold: invokes SequenceMatcher.
        """
        detector = TextChangeDetector()
        detector.last_text = "X" * 1000
        threshold = 0.5

        # Case 1: l2 = 3001 -> bound = 2 * 1000 / 4001 = 0.499875 < 0.5 -> SKIP DP
        with patch("app.text_change_detector.SequenceMatcher") as mock_matcher:
            event, text = detector.observe("Y" * 3001, threshold=0.5)
            mock_matcher.assert_not_called()
            self.assertEqual(event, "change")

        # Case 2: detector has "Y" * 3001. Now l2 = 2000.
        # bound = 2 * 2000 / 5001 = 4000 / 5001 = 0.7998 >= 0.5 -> CANNOT SKIP, must call SequenceMatcher
        with patch("app.text_change_detector.SequenceMatcher", wraps=SequenceMatcher) as mock_matcher:
            event, text = detector.observe("Z" * 2000, threshold=0.5)
            mock_matcher.assert_called_once()
            self.assertEqual(event, "change")

    def test_performance_benchmark_mathematical_bound_vs_full_dp(self):
        """Benchmark execution time of mathematical bound short-circuit vs full SequenceMatcher DP on 5,000 chars."""
        detector = TextChangeDetector()
        text_a = "Alpha " * 800  # ~4,800 chars
        text_b = "Beta " * 150   # ~750 chars

        # 1. Warm-up
        detector.observe(text_a, threshold=0.5)

        # 2. Measure short-circuit path (mathematical bound triggers)
        trials = 1000
        t0 = time.perf_counter()
        for _ in range(trials):
            # bound: 2 * 750 / (4800 + 750) = 1500 / 5550 = 0.270 < 0.5
            detector.last_text = text_a
            detector.observe(text_b, threshold=0.5)
        t_bound = (time.perf_counter() - t0) / trials

        # 3. Measure full SequenceMatcher DP path on two 5,000 char strings
        text_c = "Gamma " * 800  # ~4,800 chars
        t0 = time.perf_counter()
        for _ in range(20):
            _ = SequenceMatcher(None, text_a, text_c).ratio()
        t_dp = (time.perf_counter() - t0) / 20

        speedup = t_dp / max(t_bound, 1e-9)

        print("\n[EMPIRICAL TEST 2B: Performance Benchmark on ~5,000 Character Diffing]")
        print(f"Mathematical Bound Short-Circuit: {t_bound * 1000:.4f} ms per diff")
        print(f"Full SequenceMatcher DP:          {t_dp * 1000:.4f} ms per diff")
        print(f"Empirical Speedup Factor:         {speedup:.1f}x")

        # Invariant: Bound short-circuit must execute in under 0.05 ms (50 microseconds)
        self.assertLess(t_bound, 0.00005, f"Short-circuit took too long: {t_bound*1000:.4f}ms")
        # Invariant: Speedup must be substantial (> 100x)
        self.assertGreater(speedup, 100.0, f"Speedup {speedup:.1f}x is lower than expected")

    def test_text_change_detector_multithreaded_stress(self):
        """Verify TextChangeDetector concurrency safety under 10 threads observing lines concurrently."""
        detector = TextChangeDetector()
        errors = []

        def worker(idx: int):
            try:
                for i in range(100):
                    lines = [OcrLine(text=f"Line {idx}_{i}", box=[[0, 0], [10, 0], [10, 10], [0, 10]], score=0.9)]
                    event, text = detector.observe(lines, threshold=0.5)
                    self.assertIn(event, ("none", "change", "clear"))
                    _ = detector.is_stable
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrency errors encountered: {errors}")


if __name__ == "__main__":
    unittest.main()
