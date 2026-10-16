import time
import unittest
from unittest import mock

import numpy as np

from app.ocr_engine import OcrLine
from app.pipelines import OneShotPipeline
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

    def _ocr(self, *texts):
        ocr = mock.Mock()
        ocr.recognize.return_value = [
            # 行间距足够大，不会被合成同一段落
            OcrLine(text, 0.99, (0, i * 30, 20, i * 30 + 10)) for i, text in enumerate(texts)
        ]
        return ocr

    def test_image_path_translates_each_paragraph_and_falls_back_to_source(self):
        translator = mock.Mock()
        translator.translate.side_effect = lambda text, *a, **k: "你好" if text == "Hello" else ""
        result = OneShotPipeline(self._ocr("Hello", "World"), translator).run(
            image=np.zeros((64, 20, 3), dtype=np.uint8), translate=True, target_language="简体中文"
        )
        self.assertEqual(result.source, "Hello\nWorld")
        self.assertEqual(result.translation, "你好\nWorld")
        self.assertEqual([c.args[0] for c in translator.translate.call_args_list], ["Hello", "World"])

    def test_cancel_between_lines_stops_remaining_lines(self):
        state = {"cancel": False}
        translator = mock.Mock()

        def translate(text, *a, **k):
            state["cancel"] = True
            return "你好"

        translator.translate.side_effect = translate
        result = OneShotPipeline(self._ocr("Hello", "World"), translator).run(
            image=np.zeros((64, 20, 3), dtype=np.uint8), translate=True,
            target_language="简体中文", cancelled=lambda: state["cancel"],
        )
        self.assertIsNone(result)
        translator.translate.assert_called_once()

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


class WatchIntervalTests(unittest.TestCase):
    def test_configured_interval_becomes_deep_idle_interval(self):
        cfg = {"region_watch_interval_ms": 2000, "region_watch_diff_threshold": 0.9,
               "target_language": "简体中文"}
        watcher = WindowWatcher(mock.Mock(), mock.Mock(), cfg, region=(0, 0, 1, 1), profile="region")
        watcher._grab = mock.Mock(return_value=((0, 0, 1, 1), np.zeros((1, 1, 3), np.uint8)))
        watcher.start()
        deadline = time.time() + 3
        while not watcher._grab.called and time.time() < deadline:
            time.sleep(0.01)
        watcher.stop()
        self.assertTrue(watcher.wait(3000))
        controller = watcher.polling_controller
        self.assertEqual(controller.deep_idle_interval, 2.0)
        # 长时间静止后放慢到设置值；画面一变化立即回到快速轮询
        for _ in range(controller.deep_idle_threshold):
            controller.on_frame(False)
        self.assertEqual(controller.current_interval, 2.0)
        self.assertEqual(controller.on_frame(True), controller.active_interval)


class LiveTranslationStateTests(unittest.TestCase):
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
