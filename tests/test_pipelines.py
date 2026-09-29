import unittest
from unittest import mock

import numpy as np

from app.ocr_engine import OcrLine
from app.pipelines import LiveTranslationState, OneShotPipeline
from app.window_watcher import WindowWatcher


class OneShotPipelineTests(unittest.TestCase):
    def test_text_path_does_not_call_ocr(self):
        ocr = mock.Mock()
        translator = mock.Mock()
        translator.translate.return_value = "译文"
        result = OneShotPipeline(ocr, translator).run(
            text=" source ", translate=True, target_language="简体中文"
        )
        self.assertEqual((result.source, result.translation), ("source", "译文"))
        ocr.recognize.assert_not_called()

    def test_cancelled_task_does_not_start_work(self):
        ocr = mock.Mock()
        result = OneShotPipeline(ocr, mock.Mock()).run(
            image=np.zeros((2, 2, 3), dtype=np.uint8),
            translate=False,
            target_language="简体中文",
            cancelled=lambda: True,
        )
        self.assertIsNone(result)
        ocr.recognize.assert_not_called()


class LiveTranslationStateTests(unittest.TestCase):
    def test_change_stability_and_two_empty_frames(self):
        state = LiveTranslationState()
        line = OcrLine("hello", 1.0, (0, 0, 10, 10))
        self.assertEqual(state.observe([line], 0.9)[0], "change")
        self.assertEqual(state.observe([line], 0.9)[0], "none")
        self.assertEqual(state.observe([], 0.9)[0], "none")
        self.assertEqual(state.observe([], 0.9)[0], "clear")

    def test_cache_prunes_only_after_limit(self):
        state = LiveTranslationState(line_cache={str(i): str(i) for i in range(5)})
        state.prune_cache(["1", "3"], limit=4)
        self.assertEqual(state.line_cache, {"1": "1", "3": "3"})

    def test_live_worker_can_start_and_stop_fifty_times(self):
        cfg = {
            "region_watch_interval_ms": 50,
            "region_watch_diff_threshold": 0.9,
            "target_language": "简体中文",
        }
        watchers = []
        for _ in range(50):
            watcher = WindowWatcher(
                mock.Mock(), mock.Mock(), cfg,
                region=(0, 0, 1, 1), profile="region",
            )
            watcher._grab = mock.Mock(return_value=((0, 0, 1, 1), np.zeros((1, 1, 3), np.uint8)))
            watcher.start()
            watcher.stop()
            self.assertTrue(watcher.wait(2000))
            self.assertFalse(watcher.isRunning())
            watchers.append(watcher)


if __name__ == "__main__":
    unittest.main()
