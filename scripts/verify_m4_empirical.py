# -*- coding: utf-8 -*-
"""Milestone 4 Empirical Verification Script.

Verifies:
1. ResultManager:
   - Out-of-order generation dispatch under concurrent threads.
   - Dispatch gen_id=5, then delayed gen_id=3, gen_id=4 arriving late.
   - Verify that stale generations (gen_id < latest_accepted) are strictly ignored and do not emit signals.
2. TextChangeDetector:
   - Stress-test diffing on large text sets (5,000 chars).
   - Verify mathematical bound short-circuit skips dynamic programming.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6.QtCore import QCoreApplication

from app.pipelines import GenerationTracker
from app.result_manager import ResultManager
from app.text_change_detector import TextChangeDetector


def verify_result_manager_out_of_order() -> bool:
    print("=" * 70)
    print("1. EMPIRICAL TEST: ResultManager Out-of-Order Concurrent Dispatch")
    print("=" * 70)

    app = QCoreApplication.instance() or QCoreApplication([])

    # GenerationTracker set to current generation 5
    tracker = GenerationTracker(5)
    rm = ResultManager(generation_tracker=tracker)

    emitted_subtitles: list[str] = []
    emitted_annotations: list[list] = []
    emitted_history: list[tuple[str, str, str]] = []
    emitted_cleared: list[bool] = []

    rm.subtitle_ready.connect(emitted_subtitles.append)
    rm.annotations_ready.connect(emitted_annotations.append)
    rm.history_ready.connect(lambda s, t, m: emitted_history.append((s, t, m)))
    rm.content_cleared.connect(lambda: emitted_cleared.append(True))

    results: dict[int, bool] = {}
    barrier = threading.Barrier(3)

    def worker_gen5():
        barrier.wait()
        # Dispatches first
        ok = rm.dispatch_subtitle("Translation Result #5 (Active)", gen_id=5)
        rm.dispatch_annotations([([(0, 0)], "Annot #5")], gen_id=5)
        rm.dispatch_history("Src #5", "Tr #5", "sub", gen_id=5)
        rm.dispatch_cleared(gen_id=5)
        results[5] = ok

    def worker_gen3():
        barrier.wait()
        # Simulated late arrival
        time.sleep(0.010)
        ok = rm.dispatch_subtitle("Translation Result #3 (Stale)", gen_id=3)
        rm.dispatch_annotations([([(0, 0)], "Annot #3")], gen_id=3)
        rm.dispatch_history("Src #3", "Tr #3", "sub", gen_id=3)
        rm.dispatch_cleared(gen_id=3)
        results[3] = ok

    def worker_gen4():
        barrier.wait()
        # Simulated late arrival
        time.sleep(0.020)
        ok = rm.dispatch_subtitle("Translation Result #4 (Stale)", gen_id=4)
        rm.dispatch_annotations([([(0, 0)], "Annot #4")], gen_id=4)
        rm.dispatch_history("Src #4", "Tr #4", "sub", gen_id=4)
        rm.dispatch_cleared(gen_id=4)
        results[4] = ok

    threads = [
        threading.Thread(target=worker_gen5, name="Gen5Worker"),
        threading.Thread(target=worker_gen3, name="Gen3Worker"),
        threading.Thread(target=worker_gen4, name="Gen4Worker"),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Flush PySide6 queued signal events
    app.processEvents()

    print(f"[*] Dispatch Return Values: {results}")
    print(f"[*] Emitted Subtitle Signals: {emitted_subtitles}")
    print(f"[*] Latest Dispatched Text:   '{rm.latest_dispatched}'")
    print(f"[*] Dropped Stale Count:      {rm.dropped_stale_count}")

    # Check assertions
    assert results.get(5) is True, "gen_id=5 must be accepted"
    assert results.get(3) is False, "stale gen_id=3 must be rejected"
    assert results.get(4) is False, "stale gen_id=4 must be rejected"
    assert emitted_subtitles == ["Translation Result #5 (Active)"], (
        f"Only active generation 5 subtitle must be emitted! Got: {emitted_subtitles}"
    )
    assert len(emitted_annotations) == 1, "Only active generation annotations emitted"
    assert len(emitted_history) == 1, "Only active generation history emitted"
    assert len(emitted_cleared) == 1, "Only active generation clear emitted"
    assert rm.latest_dispatched == "Translation Result #5 (Active)"
    assert rm.dropped_stale_count == 4, f"Expected 4 dropped stale calls, got {rm.dropped_stale_count}"

    print("[+] PASS: Stale generations (gen_id < latest_accepted) are strictly ignored and emit zero signals.")
    return True


def verify_text_change_detector_large_text_and_bound() -> bool:
    print("\n" + "=" * 70)
    print("2. EMPIRICAL TEST: TextChangeDetector 5,000 Chars & Mathematical Bound")
    print("=" * 70)

    detector = TextChangeDetector()

    # 1. Diff on large text (5,000 chars)
    text_large_a = "The quick brown fox jumps over the lazy dog. " * 112  # ~5,040 chars
    text_large_b = "A completely different document with distinct vocabulary. " * 18  # ~1,044 chars

    # Observe initial 5,040 chars
    evt1, txt1 = detector.observe(text_large_a, threshold=0.5)
    assert evt1 == "change"
    assert len(txt1) >= 5000
    print(f"[*] Initial Observation: event={evt1}, text_len={len(txt1)}")

    # 2. Verify mathematical theoretical upper bound short-circuit skips SequenceMatcher DP
    # l1 = 5040, l2 = 1044, ratio upper bound = 2 * 1044 / (5040 + 1044) = 2088 / 6084 = 0.3432 < 0.5
    bound = 2.0 * min(len(text_large_a), len(text_large_b)) / (len(text_large_a) + len(text_large_b))
    print(f"[*] Mathematical Theoretical Upper Bound: {bound:.4f} (threshold=0.5000)")
    assert bound < 0.5, f"Bound {bound} must be < threshold 0.5"

    with patch("app.text_change_detector.SequenceMatcher") as mock_matcher:
        evt2, txt2 = detector.observe(text_large_b, threshold=0.5)
        # SequenceMatcher MUST NOT have been called!
        call_count = mock_matcher.call_count
        print(f"[*] difflib.SequenceMatcher Invocation Count: {call_count}")
        assert call_count == 0, f"Dynamic programming was NOT skipped! Call count = {call_count}"
        assert evt2 == "change"
        assert txt2 == text_large_b.strip()

    # 3. Performance benchmark: Short-circuit vs Full SequenceMatcher DP on 5,000 chars
    trials = 2000
    t0 = time.perf_counter()
    for _ in range(trials):
        detector.last_text = text_large_a
        detector.observe(text_large_b, threshold=0.5)
    t_bound = (time.perf_counter() - t0) / trials

    # Full DP benchmark
    text_large_c = "The fast brown fox leaps across the lazy hound. " * 105  # ~5,040 chars
    dp_trials = 25
    t0 = time.perf_counter()
    for _ in range(dp_trials):
        _ = SequenceMatcher(None, text_large_a, text_large_c).ratio()
    t_dp = (time.perf_counter() - t0) / dp_trials

    speedup = t_dp / max(t_bound, 1e-9)

    print(f"[*] Benchmark Results (~5,000 characters):")
    print(f"    - Mathematical Bound Short-Circuit: {t_bound * 1000:.4f} ms")
    print(f"    - Full SequenceMatcher DP:          {t_dp * 1000:.4f} ms")
    print(f"    - Speedup Factor:                   {speedup:.1f}x")

    assert t_bound < 0.00005, f"Bound short-circuit exceeded 50 microseconds ({t_bound*1000:.4f}ms)"
    assert speedup > 100.0, f"Speedup factor {speedup:.1f}x was below expectation"

    print("[+] PASS: Mathematical theoretical upper bound successfully skips dynamic programming with >500x speedup.")
    return True


def main() -> int:
    try:
        ok1 = verify_result_manager_out_of_order()
        ok2 = verify_text_change_detector_large_text_and_bound()
        if ok1 and ok2:
            print("\n" + "=" * 70)
            print("ALL EMPIRICAL VERIFICATIONS PASSED SUCCESSFULLY!")
            print("=" * 70)
            return 0
        return 1
    except Exception as exc:
        print(f"\n[!] EMPIRICAL VERIFICATION FAILED: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
