# -*- coding: utf-8 -*-
"""Milestone 4 Standalone Empirical Stress & Adversarial Test Suite for LatestFrameBuffer & Pipeline Concurrency.

Objectives:
1. Exact Empirical Stress Specification:
   - Producer thread pushing 1,000 frames rapidly (1000 FPS simulate high-frequency capture).
   - Consumer thread sleeping 5ms per frame (200 FPS simulate heavy OCR inference).
   - Verify exactly: consumer receives monotonically increasing gen_ids;
     dropped_count + consumer_received_count == total_pushed (or total_pushed - remaining);
     no deadlock, race condition, or exception.
2. Concurrency & Contention Stress:
   - 10 concurrent producers pushing 100 frames each (1,000 frames total).
   - Concurrent consumer draining frames.
   - Exact dropped_count + total_consumed == 1,000.
3. Race Safety with Interleaved clear():
   - Producer, consumer, and clearer operating concurrently on shared buffer.
   - Buffer remains fully operational afterwards with no leaked locks or deadlocks.
4. Synchronous 1:1 Invariant:
   - Consumer matching producer rate: dropped_count == 0.
5. Boundary Invariants:
   - Non-blocking get(timeout=0) and get(timeout=0.0) return immediately.
   - peek() inspects slot without consuming.
6. Generation Tracking & Out-of-Order Resolution:
   - Concurrent asynchronous translations arriving out-of-order.
   - ResultManager strictly drops stale generations and accepts only active generation.
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

try:
    from PySide6.QtCore import Qt
    _HAS_QT = True
except ImportError:
    _HAS_QT = False

from app.latest_frame_buffer import LatestFrameBuffer
from app.pipelines import GenerationTracker
from app.result_manager import ResultManager


class TestLatestFrameBufferStress(unittest.TestCase):
    """Empirical stress and concurrency validation for LatestFrameBuffer and Pipeline."""

    def test_1000_fps_producer_vs_200_fps_consumer(self):
        """Stress-test 1,000 frames at ~1000 FPS against a 200 FPS (5ms sleep) consumer.

        Specification Requirements:
        - Producer thread pushing 1,000 frames rapidly (1000 FPS simulate high-frequency capture).
        - Consumer thread sleeping 5ms per frame (200 FPS simulate heavy OCR inference).
        - Verify exactly: consumer receives monotonically increasing gen_ids;
          dropped_count + consumer_received_count == total_pushed (or total_pushed - remaining);
          no deadlock, race condition, or exception.
        """
        buffer = LatestFrameBuffer()
        total_pushed_target = 1000
        consumed_gen_ids: list[int] = []
        producer_done = threading.Event()
        errors: list[Exception] = []

        t_start = time.perf_counter()

        def producer():
            try:
                for seq in range(1, total_pushed_target + 1):
                    frame = np.full((16, 16), seq % 256, dtype=np.uint8)
                    buffer.put(frame, generation_id=seq)
                    # Yield / sleep ~1ms (1000 FPS simulation)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)
            finally:
                producer_done.set()

        def consumer():
            try:
                while not producer_done.is_set() or not buffer.is_empty:
                    item = buffer.get(timeout=0.02)
                    if item is not None:
                        _, gen_id = item
                        consumed_gen_ids.append(gen_id)
                        # Simulate 200 FPS heavy OCR inference (~5ms sleep)
                        time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        prod_thread = threading.Thread(target=producer, name="StressProducer")
        cons_thread = threading.Thread(target=consumer, name="StressConsumer")

        prod_thread.start()
        cons_thread.start()

        prod_thread.join(timeout=10.0)
        self.assertFalse(prod_thread.is_alive(), "Producer thread timed out or deadlocked!")

        cons_thread.join(timeout=10.0)
        self.assertFalse(cons_thread.is_alive(), "Consumer thread timed out or deadlocked!")

        # Check remaining frame in slot
        remaining = 0
        rem_item = buffer.get(timeout=0.01)
        if rem_item is not None:
            remaining = 1
            consumed_gen_ids.append(rem_item[1])

        total_consumed = len(consumed_gen_ids)
        dropped = buffer.dropped_count
        elapsed = time.perf_counter() - t_start

        print(f"\n[EMPIRICAL STRESS RESULT]")
        print(f"Total Pushed Target: {total_pushed_target}")
        print(f"Total Consumed:      {total_consumed}")
        print(f"Total Dropped:       {dropped}")
        print(f"Remaining in Slot:   {remaining}")
        print(f"Sum (Consumed+Drop): {total_consumed + dropped}")
        print(f"Elapsed Time:        {elapsed:.3f}s")
        print(f"Consumer Samples:    First 5: {consumed_gen_ids[:5]} ... Last 5: {consumed_gen_ids[-5:]}")

        # Invariant 1: Zero exceptions or errors
        self.assertEqual(len(errors), 0, f"Exceptions occurred during stress run: {errors}")

        # Invariant 2: Exact frame accounting: dropped_count + consumer_received_count == total_pushed
        self.assertEqual(
            total_consumed + dropped,
            total_pushed_target,
            f"Accounting mismatch! consumed ({total_consumed}) + dropped ({dropped}) != total ({total_pushed_target})"
        )

        # Invariant 3: Strictly monotonically increasing generation IDs
        for i in range(len(consumed_gen_ids) - 1):
            self.assertLess(
                consumed_gen_ids[i],
                consumed_gen_ids[i + 1],
                f"Non-monotonic gen_id at index {i}: {consumed_gen_ids[i]} >= {consumed_gen_ids[i+1]}"
            )

        # Invariant 4: Frame drop ratio: 5ms inference is ~5x slower than 1ms capture
        self.assertGreater(dropped, 500, f"Expected dropped frames > 500 at 5x speed disparity, got {dropped}")
        self.assertGreater(total_consumed, 100, f"Expected consumed frames > 100, got {total_consumed}")

    def test_concurrent_producers_burst_contention(self):
        """10 parallel producers pushing 100 frames each concurrently (1,000 total) into buffer."""
        buffer = LatestFrameBuffer()
        total_per_producer = 100
        num_producers = 10
        total_pushed = total_per_producer * num_producers
        consumed_items: list[tuple[int, int]] = []
        errors: list[Exception] = []

        all_produced = threading.Event()

        def producer_task(pid: int):
            try:
                for seq in range(1, total_per_producer + 1):
                    gen = pid * 10000 + seq
                    frame = np.array([pid, seq], dtype=np.int32)
                    buffer.put(frame, generation_id=gen)
                    time.sleep(0.0005)
            except Exception as e:
                errors.append(e)

        def consumer_task():
            try:
                while not all_produced.is_set() or not buffer.is_empty:
                    item = buffer.get(timeout=0.01)
                    if item is not None:
                        arr, gen = item
                        consumed_items.append((int(arr[0]), int(arr[1])))
                        time.sleep(0.002)
            except Exception as e:
                errors.append(e)

        cons_thread = threading.Thread(target=consumer_task)
        cons_thread.start()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_producers) as pool:
            futs = [pool.submit(producer_task, pid) for pid in range(num_producers)]
            for f in futs:
                f.result(timeout=10.0)

        all_produced.set()
        cons_thread.join(timeout=5.0)

        rem = buffer.get(timeout=0.01)
        if rem is not None:
            arr, _ = rem
            consumed_items.append((int(arr[0]), int(arr[1])))

        total_consumed = len(consumed_items)
        dropped = buffer.dropped_count

        self.assertEqual(len(errors), 0, f"Concurrent contention produced errors: {errors}")
        self.assertEqual(
            total_consumed + dropped,
            total_pushed,
            f"Multi-producer accounting failed: consumed {total_consumed} + dropped {dropped} != {total_pushed}"
        )

    def test_interleaved_clear_race_safety(self):
        """Validate clear() called while producer puts and consumer gets concurrently."""
        buffer = LatestFrameBuffer()
        stop_event = threading.Event()
        errors: list[Exception] = []

        def producer():
            try:
                seq = 0
                while not stop_event.is_set() and seq < 500:
                    seq += 1
                    buffer.put(np.array([seq]), generation_id=seq)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def consumer():
            try:
                while not stop_event.is_set():
                    buffer.get(timeout=0.005)
            except Exception as e:
                errors.append(e)

        def clearer():
            try:
                while not stop_event.is_set():
                    buffer.clear()
                    time.sleep(0.002)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=producer),
            threading.Thread(target=consumer),
            threading.Thread(target=clearer),
        ]
        for t in threads:
            t.start()

        time.sleep(0.5)
        stop_event.set()

        for t in threads:
            t.join(timeout=3.0)

        self.assertEqual(len(errors), 0, f"Race condition during clear(): {errors}")
        # Buffer should remain completely functional
        buffer.put(np.array([999]), generation_id=999)
        res = buffer.get(timeout=0.1)
        self.assertIsNotNone(res)
        self.assertEqual(res[1], 999)

    def test_synchronous_put_get_zero_drop(self):
        """When consumer matches producer speed (1:1), dropped_count must be strictly 0."""
        buffer = LatestFrameBuffer()
        count = 1000
        for i in range(1, count + 1):
            buffer.put(np.array([i]), generation_id=i)
            item = buffer.get(timeout=0.01)
            self.assertIsNotNone(item)
            self.assertEqual(item[1], i)
        self.assertEqual(buffer.dropped_count, 0)
        self.assertTrue(buffer.is_empty)

    def test_timeout_zero_and_peek_invariants(self):
        """Validate get(timeout=0), get(timeout=0.0), and peek() invariants."""
        buffer = LatestFrameBuffer()
        t0 = time.perf_counter()
        res0 = buffer.get(timeout=0)
        elapsed0 = time.perf_counter() - t0
        self.assertIsNone(res0)
        self.assertLess(elapsed0, 0.05, "timeout=0 must return immediately")

        t1 = time.perf_counter()
        res1 = buffer.get(timeout=0.0)
        elapsed1 = time.perf_counter() - t1
        self.assertIsNone(res1)
        self.assertLess(elapsed1, 0.05, "timeout=0.0 must return immediately")

        # peek() invariant
        self.assertIsNone(buffer.peek())
        test_frame = np.array([42])
        buffer.put(test_frame, generation_id=101)
        self.assertFalse(buffer.is_empty)
        peeked = buffer.peek()
        self.assertIsNotNone(peeked)
        self.assertEqual(peeked[1], 101)
        self.assertFalse(buffer.is_empty, "peek() must not consume frame")

        consumed = buffer.get()
        self.assertEqual(consumed[1], 101)
        self.assertTrue(buffer.is_empty)

    def test_generation_tracking_out_of_order_resolution(self):
        """Verify ResultManager and GenerationTracker drop stale asynchronous results.

        Simulates:
        - 5 asynchronous translation tasks launched for generations 1, 2, 3, 4, 5.
        - Results complete and arrive in reverse/out-of-order order: [3, 1, 5, 2, 4].
        - ResultManager must drop all results whose generation is < active generation.
        """
        tracker = GenerationTracker()
        received_subtitles: list[tuple[int, str]] = []

        result_mgr = ResultManager(
            tracker,
            on_subtitle=lambda text: received_subtitles.append((tracker.current_generation, text)),
        )

        # Advance tracker through generations 1, 2, 3, 4, 5
        for expected_gen in range(1, 6):
            gen = tracker.next_generation()
            self.assertEqual(gen, expected_gen)

        # Active generation is now 5
        self.assertEqual(tracker.current_generation, 5)

        # Simulated arrivals in arbitrary order: 3, 1, 5, 2, 4
        arrival_order = [3, 1, 5, 2, 4]
        dispatch_results = {}
        for arriving_gen in arrival_order:
            ok = result_mgr.dispatch_subtitle(f"Result for {arriving_gen}", arriving_gen)
            dispatch_results[arriving_gen] = ok

        # Generations 1, 2, 3, 4 must be dropped as stale!
        self.assertFalse(dispatch_results[1], "Stale gen 1 must be dropped")
        self.assertFalse(dispatch_results[2], "Stale gen 2 must be dropped")
        self.assertFalse(dispatch_results[3], "Stale gen 3 must be dropped")
        self.assertFalse(dispatch_results[4], "Stale gen 4 must be dropped")

        # Generation 5 must be accepted
        self.assertTrue(dispatch_results[5], "Active gen 5 must be accepted")
        self.assertEqual(result_mgr.dropped_stale_count, 4)
        self.assertEqual(len(received_subtitles), 1)
        self.assertEqual(received_subtitles[0][1], "Result for 5")


def run_standalone_stress():
    print("=" * 70)
    print("RUNNING STANDALONE EMPIRICAL STRESS HARNESS FOR LatestFrameBuffer")
    print("=" * 70)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestLatestFrameBufferStress)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("\nALL STRESS TESTS COMPLETED WITH EXIT CODE 0.")


if __name__ == "__main__":
    run_standalone_stress()
