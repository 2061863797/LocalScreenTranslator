"""区域持续翻译首条结果的队列时序回归测试。"""

import unittest
from unittest.mock import Mock

import numpy as np

from app.main import App
from app.latest_frame_buffer import FramePacket
from app.ocr_engine import OcrLine
from app.translation_manager import TranslationManager
from app.window_watcher import WindowWatcher


class TestRegionWatchStartup(unittest.TestCase):
    def test_static_frame_cannot_invalidate_queued_first_translation(self):
        watcher = WindowWatcher(
            Mock(), Mock(), {"region_watch_diff_threshold": 0.9},
            region=(0, 0, 64, 32), profile="region", display_mode="annotate",
        )
        watcher._content_revision = 1
        watcher._content_epoch = 1
        frame = np.zeros((32, 64, 3), dtype=np.uint8)
        first = FramePacket(frame, epoch=1, has_changed=True, changed_ratio=1.0,
                            work_revision=1, content_revision=1)
        still = FramePacket(frame, epoch=1, has_changed=False, changed_ratio=0.0,
                            work_revision=1, content_revision=1)
        packets = iter((first, still))

        def next_packet(timeout=None):
            try:
                return next(packets)
            except StopIteration:
                watcher._running = False
                watcher._consumer_stop_event.set()
                return None

        watcher._frame_buffer.get = next_packet
        line = OcrLine("Hello", 0.99, (4, 4, 30, 20))
        watcher._recognize_live_lines = Mock(return_value=[line])
        items = [(line.box, "你好")]
        watcher._translation_manager.translate_annotations = Mock(
            return_value=(items, "你好")
        )
        watcher._result_manager.dispatch_annotations = Mock(return_value=True)
        watcher._result_manager.dispatch_history = Mock()

        watcher._inference_loop()

        watcher._result_manager.dispatch_annotations.assert_called_once()
        queued_gen = watcher._result_manager.dispatch_annotations.call_args.args[1]
        self.assertTrue(watcher.generation_tracker.is_active(queued_gen))

        app = App.__new__(App)
        app._watcher = watcher
        app._watch_session_id = 1
        app._watch_paused = False
        app._watch_region = (0, 0, 64, 32)
        app._watch_annotate = True
        app.cfg = {"annotate_capture_visible": False}
        app.annotation = Mock()
        app.log = Mock()
        App._on_watch_annotations_guarded(app, items, 1, watcher, queued_gen, 1)
        app.annotation.set_items.assert_called_once_with(items)

    def test_blank_reply_retries_without_moving_region(self):
        translator = Mock()
        translator.translate_lines.side_effect = [[""], ["你好"]]
        watcher = WindowWatcher(
            Mock(), translator, {"region_watch_diff_threshold": 0.9},
            region=(0, 0, 64, 32), profile="region", display_mode="annotate",
        )
        watcher._content_revision = 1
        watcher._content_epoch = 1
        frame = np.zeros((32, 64, 3), dtype=np.uint8)
        packets = iter(
            [FramePacket(frame, epoch=1, has_changed=True, changed_ratio=1.0,
                         work_revision=1, content_revision=1)]
            + [FramePacket(frame, epoch=1, has_changed=False, changed_ratio=0.0,
                           work_revision=1, content_revision=1) for _ in range(6)]
        )

        def next_packet(timeout=None):
            try:
                return next(packets)
            except StopIteration:
                watcher._running = False
                watcher._consumer_stop_event.set()
                return None

        watcher._frame_buffer.get = next_packet
        line = OcrLine("Hello", 0.99, (4, 4, 30, 20))
        watcher._recognize_live_lines = Mock(return_value=[line])
        watcher._result_manager.dispatch_annotations = Mock(return_value=True)
        watcher._result_manager.dispatch_history = Mock()

        watcher._inference_loop()

        self.assertEqual(translator.translate_lines.call_count, 2)
        watcher._result_manager.dispatch_annotations.assert_called_once()
        self.assertEqual(
            watcher._result_manager.dispatch_annotations.call_args.args[0],
            [(line.box, "你好")],
        )
        self.assertTrue(watcher.generation_tracker.is_active(
            watcher._result_manager.dispatch_annotations.call_args.args[1]
        ))

    def test_empty_model_reply_is_retried_for_same_source(self):
        translator = Mock()
        translator.translate_lines.side_effect = [[""], ["你好"]]
        manager = TranslationManager(translator)
        lines = [OcrLine("Hello", 0.99, (4, 4, 30, 20))]

        with self.assertRaisesRegex(RuntimeError, "缺少有效结果"):
            manager.translate_annotations(lines, "简体中文")
        items, translation = manager.translate_annotations(lines, "简体中文")

        self.assertEqual(items, [((4, 4, 30, 20), "你好")])
        self.assertEqual(translation, "你好")
        self.assertEqual(translator.translate_lines.call_count, 2)


if __name__ == "__main__":
    unittest.main()
