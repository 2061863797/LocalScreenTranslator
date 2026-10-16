"""区域持续翻译首条结果的队列时序回归测试。"""

import unittest
from unittest.mock import Mock

import numpy as np

from app.main import App
from app.latest_frame_buffer import FramePacket
from app.ocr_engine import OcrLine
from app.window_watcher import WindowWatcher


class TestRegionWatchStartup(unittest.TestCase):
    def test_static_frame_cannot_invalidate_queued_first_translation(self):
        translator = Mock()
        translator.translate.return_value = "你好"
        watcher = WindowWatcher(
            Mock(), translator, {"region_watch_diff_threshold": 0.9},
            region=(0, 0, 64, 32), profile="region", display_mode="replace",
        )
        watcher._content_revision = 1
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
        watcher._result_manager.dispatch_replacement = Mock(return_value=True)
        watcher._result_manager.dispatch_history = Mock()

        watcher._inference_loop()

        dispatch = watcher._result_manager.dispatch_replacement
        # 文字一出现先派发空数据撤下旧译文，翻完再派发译文
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(dispatch.call_args_list[0].args[0].blocks, ())
        payload, queued_gen = dispatch.call_args.args[:2]
        self.assertEqual([block.text for block in payload.blocks], ["你好"])
        self.assertTrue(watcher.generation_tracker.is_active(queued_gen))

        app = App.__new__(App)
        app._watcher = watcher
        app._watch_session_id = 1
        app._watch_paused = False
        app._watch_region = (0, 0, 64, 32)
        app._watch_rect = None
        app._watch_display_mode = "replace"
        app.cfg = {}
        app.replacement = Mock()
        app.log = Mock()
        App._on_watch_replacement_guarded(app, payload, 1, watcher, queued_gen)
        app.replacement.set_payload.assert_called_once_with(payload)

    def test_blank_reply_shows_source_without_retry(self):
        translator = Mock()
        translator.translate.return_value = ""
        watcher = WindowWatcher(
            Mock(), translator, {"region_watch_diff_threshold": 0.9},
            region=(0, 0, 64, 32), profile="region", display_mode="replace",
        )
        watcher._content_revision = 1
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
        watcher._result_manager.dispatch_replacement = Mock(return_value=True)
        watcher._result_manager.dispatch_history = Mock()

        watcher._inference_loop()

        # 空译文是模型的确定结果：显示原文，不重试，不记历史
        self.assertEqual(translator.translate.call_count, 1)
        dispatch = watcher._result_manager.dispatch_replacement
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual([block.text for block in dispatch.call_args.args[0].blocks], ["Hello"])
        watcher._result_manager.dispatch_history.assert_not_called()

if __name__ == "__main__":
    unittest.main()
