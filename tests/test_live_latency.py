"""Regression tests for localized OCR and real-time text confirmation."""

import unittest
from unittest.mock import Mock

import numpy as np

from app.ocr_engine import OcrLine
from app.text_change_detector import TextChangeDetector
from app.window_watcher import WindowWatcher


class FakeOcr:
    def __init__(self):
        self.shapes = []

    def recognize(self, image):
        height, width = image.shape[:2]
        self.shapes.append((height, width))
        if width >= 700:
            return [
                OcrLine("Status 128", 0.99, (100, 100, 500, 130)),
                OcrLine("Other text", 0.99, (700, 300, 900, 330)),
            ]
        if width >= 200:
            return [OcrLine("Status 128", 0.99, (24, 24, 424, 54))]
        return []


class TestLiveLatency(unittest.TestCase):
    def setUp(self):
        self.ocr = FakeOcr()
        self.watcher = WindowWatcher(
            self.ocr,
            Mock(),
            {"region_watch_diff_threshold": 0.9},
            region=(0, 0, 1000, 600),
            profile="region",
            display_mode="annotate",
        )
        self.frame = np.zeros((600, 1000, 3), dtype=np.uint8)
        self.old_lines = [
            OcrLine("Status 125", 0.99, (100, 100, 500, 130)),
            OcrLine("Other text", 0.99, (700, 300, 900, 330)),
        ]
        self.watcher.scene_text_state.update_full(self.old_lines)
        self.watcher.ocr_stabilizer.process(self.old_lines)

    def test_partial_change_recognizes_complete_line_and_keeps_other_text(self):
        event, _ = self.watcher.text_change_detector.observe(self.old_lines, 0.9)
        self.assertEqual(event, "change")

        first = self.watcher._recognize_live_lines(
            self.frame, (300, 108, 310, 120), 0.01
        )
        self.assertEqual([line.text for line in first], ["Status 125", "Other text"])
        self.assertFalse(self.watcher.ocr_stabilizer.last_change_was_debounced)

        second = self.watcher._recognize_live_lines(
            self.frame, (300, 108, 310, 120), 0.01
        )
        self.assertEqual([line.text for line in second], ["Status 128", "Other text"])
        self.assertTrue(self.watcher.ocr_stabilizer.last_change_was_debounced)
        self.assertTrue(all(width < 1000 for _, width in self.ocr.shapes))
        self.assertTrue(all(width >= 448 for _, width in self.ocr.shapes))
        self.assertEqual(second[0].box, (100, 100, 500, 130))
        self.assertEqual(self.watcher.scene_text_state.roi_update_count, 2)

        event, _ = self.watcher.text_change_detector.observe(
            second, 0.9, exact_change=self.watcher.ocr_stabilizer.last_change_was_debounced
        )
        self.assertEqual(event, "change")
        self.assertFalse(self.watcher.text_change_detector.has_pending_candidate)

    def test_background_only_change_does_not_remove_existing_text(self):
        lines = self.watcher._recognize_live_lines(
            self.frame, (10, 500, 20, 510), 0.01
        )
        self.assertEqual([line.text for line in lines], ["Status 125", "Other text"])
        self.assertTrue(all(width < 1000 for _, width in self.ocr.shapes))

    def test_periodic_fallback_uses_full_frame_and_resets_roi_counter(self):
        self.watcher.ocr_service.health_check_interval = 1
        self.watcher._recognize_live_lines(
            self.frame, (300, 108, 310, 120), 0.01
        )
        self.assertEqual(self.ocr.shapes, [(600, 1000)])
        self.assertTrue(self.watcher.ocr_service.last_was_fallback)
        self.assertEqual(self.watcher.scene_text_state.roi_update_count, 0)

    def test_empty_roi_is_checked_with_full_frame_before_clearing_text(self):
        class EmptyCropOcr(FakeOcr):
            def recognize(self, image):
                height, width = image.shape[:2]
                self.shapes.append((height, width))
                if width < 1000:
                    return []
                return [
                    OcrLine("Status 128", 0.99, (100, 100, 500, 130)),
                    OcrLine("Other text", 0.99, (700, 300, 900, 330)),
                ]

        ocr = EmptyCropOcr()
        self.watcher.ocr_service.ocr_engine = ocr
        lines = self.watcher._recognize_live_lines(
            self.frame, (300, 108, 310, 120), 0.01
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(ocr.shapes[-1], (600, 1000))
        self.assertEqual(len(ocr.shapes), 2)
        self.assertEqual(self.watcher.scene_text_state.roi_update_count, 0)

    def test_short_empty_gap_keeps_old_translation_until_new_text(self):
        now = [100.0]
        detector = TextChangeDetector(empty_clear_delay_s=1.2, clock=lambda: now[0])
        self.assertEqual(detector.observe("Old text")[0], "change")
        self.assertEqual(detector.observe("")[0], "none")
        now[0] = 100.5
        self.assertEqual(detector.observe("")[0], "none")
        self.assertTrue(detector.has_pending_candidate)
        now[0] = 100.8
        self.assertEqual(detector.observe("New text", exact_change=True)[0], "change")
        self.assertEqual(detector.last_text, "New text")

        now[0] = 101.0
        self.assertEqual(detector.observe("")[0], "none")
        now[0] = 102.0
        self.assertEqual(detector.observe("")[0], "none")
        now[0] = 102.3
        self.assertEqual(detector.observe("")[0], "clear")
        self.assertFalse(detector.has_pending_candidate)

    def test_unknown_text_box_falls_back_to_full_frame(self):
        self.watcher.scene_text_state.update_full(["unlocated text"])
        self.watcher._recognize_live_lines(
            self.frame, (300, 108, 310, 120), 0.01
        )
        self.assertEqual(self.ocr.shapes, [(600, 1000)])


if __name__ == "__main__":
    unittest.main()
