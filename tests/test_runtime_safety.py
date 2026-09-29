import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication

from app import capture
from app.main import (
    App,
    _PreloadSignals,
    _SHUTDOWN_ABORT_SECONDS,
    _SHUTDOWN_HARD_LIMIT_SECONDS,
    _clone_mime_data,
)
from app.ui.overlays import AnnotationOverlay
from app.window_watcher import WindowWatcher, _frame_changed




class _FakeMss:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def grab(self, box):
        image = np.zeros((box["height"], box["width"], 4), dtype=np.uint8)
        image[:, :, 0] = 10 if box["left"] < 100 else 20
        return image


class RuntimeSafetyTests(unittest.TestCase):
    def _run_watcher_frames(self, watcher, ocr, images, progress):
        """逐帧等待推理线程处理，避免停止监视时清空尚未消费的测试帧。"""
        self.assertEqual(len(images), len(progress))
        height, width = images[0].shape[:2]
        frame_index = 0
        failure = None

        def _grab():
            nonlocal frame_index, failure
            if frame_index:
                kind, expected = progress[frame_index - 1]
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if kind == "skip" and watcher._skipped_frames >= expected:
                        break
                    if kind == "ocr" and ocr.recognize.call_count >= expected:
                        if expected != 1 or watcher._last_processed_work_rev >= 1:
                            break
                    time.sleep(0.005)
                else:
                    failure = f"第 {frame_index} 帧未完成预期处理：{kind} {expected}"
                    watcher._running = False
                    return ((0, 0, width, height), images[frame_index - 1])

            frame_index += 1
            if frame_index > len(images):
                watcher._running = False
            return ((0, 0, width, height), images[min(frame_index, len(images)) - 1])

        watcher._grab = _grab
        watcher.run()
        self.assertIsNone(failure, failure)









    def test_native_rect_is_mapped_to_qt_logical_coordinates(self):
        old = list(capture._SCREEN_LAYOUT)
        try:
            capture._SCREEN_LAYOUT[:] = [(0, 0, 100, 100, 0, 0, 200, 200)]
            self.assertEqual(capture._native_rect_to_logical(20, 40, 100, 80), (10, 20, 50, 40))
        finally:
            capture._SCREEN_LAYOUT[:] = old

    def test_region_capture_stitches_mixed_dpi_screens(self):
        old = list(capture._SCREEN_LAYOUT)
        try:
            capture._SCREEN_LAYOUT[:] = [
                (0, 0, 100, 100, 0, 0, 100, 100),
                (100, 0, 100, 100, 100, 0, 200, 200),
            ]
            # 抓屏实例按线程缓存：先清掉，确保这段用被测的假 mss
            capture.release_mss()
            with patch("mss.MSS", return_value=_FakeMss()):
                image = capture.grab_region(50, 0, 100, 50)
            self.assertEqual(image.shape, (50, 100, 3))
            self.assertTrue(np.all(image[:, :50, 0] == 10))
            self.assertTrue(np.all(image[:, 50:, 0] == 20))
        finally:
            capture._SCREEN_LAYOUT[:] = old
            capture.release_mss()

    def test_capture_instance_is_reused_within_a_thread(self):
        capture.release_mss()
        try:
            first = capture._get_mss()
            self.assertIs(first, capture._get_mss())
            capture.release_mss()
            self.assertIsNot(first, capture._get_mss())
        finally:
            capture.release_mss()

    def test_continuous_ocr_clears_stale_text_after_two_empty_frames(self):
        watcher = WindowWatcher(
            Mock(), Mock(), {}, hwnd=404, profile="window"
        )
        watcher._last_text = "previous subtitle"
        cleared = Mock()
        watcher.content_cleared.connect(cleared)

        watcher._observe_empty_ocr_frame()
        cleared.assert_not_called()
        self.assertEqual(watcher._last_text, "previous subtitle")

        watcher._observe_empty_ocr_frame()
        cleared.assert_called_once_with()
        self.assertEqual(watcher._last_text, "")

        watcher._observe_empty_ocr_frame()
        cleared.assert_called_once_with()

    def test_identical_frames_skip_ocr_with_periodic_recheck(self):
        ocr = Mock()
        watcher = WindowWatcher(
            ocr,
            Mock(),
            {
                "window_watch_interval_ms": 20,
                "window_watch_diff_threshold": 0.8,
                "target_language": "简体中文",
            },
            hwnd=404,
            profile="window",
        )
        image = np.zeros((20, 30, 3), dtype=np.uint8)
        ocr.recognize.return_value = []
        self._run_watcher_frames(
            watcher, ocr, [image] * 7,
            [("ocr", 1), *[("skip", count) for count in range(1, 6)], ("ocr", 2)],
        )

        # 8 帧逐字节相同：第 1 帧识别，连续跳过 5 帧后第 7 帧强制复检
        self.assertEqual(ocr.recognize.call_count, 2)

    def test_changed_frames_are_recognized_immediately(self):
        ocr = Mock()
        watcher = WindowWatcher(
            ocr,
            Mock(),
            {
                "window_watch_interval_ms": 20,
                "window_watch_diff_threshold": 0.8,
                "target_language": "简体中文",
            },
            hwnd=404,
            profile="window",
        )
        blank = np.zeros((20, 30, 3), dtype=np.uint8)
        changed = np.full((20, 30, 3), 255, dtype=np.uint8)
        ocr.recognize.return_value = []
        self._run_watcher_frames(
            watcher, ocr, [blank, blank, changed],
            [("ocr", 1), ("skip", 1), ("ocr", 2)],
        )

        # 第 1 帧与内容变化的第 3 帧各识别一次；相同的第 2 帧被跳过
        self.assertEqual(ocr.recognize.call_count, 2)

    def test_frame_gate_ignores_noise_but_detects_text_changes(self):
        base = np.zeros((40, 60, 3), dtype=np.uint8)
        self.assertFalse(_frame_changed(base, base.copy()))

        # 容差内的抖动（即使正好落在采样网格上）不应触发整轮 OCR
        dimmed = base.copy()
        for y in range(0, base.shape[0], 4):
            for x in range(0, base.shape[1], 4):
                dimmed[y, x] = np.clip(base[y, x].astype(np.int16) + 8, 0, 255)
        self.assertFalse(_frame_changed(base, dimmed))

        # 落在网格上的大幅变化必须触发（防止容差/采样被写坏后静默失效）
        hot = base.copy()
        hot[4, 8] = 255
        hot[8, 12] = 255
        self.assertTrue(_frame_changed(base, hot))

        # 真正的文字变化：一小片反色必须被识别
        text_changed = base.copy()
        text_changed[10:26, 12:50] = 255 - text_changed[10:26, 12:50]
        self.assertTrue(_frame_changed(base, text_changed))

        # 整体亮度微变在容差内；尺寸变化一律算有变化
        dimmer = np.clip(base.astype(np.int16) + 8, 0, 255).astype(np.uint8)
        self.assertFalse(_frame_changed(base, dimmer))
        self.assertTrue(_frame_changed(base, base[:20]))

    def test_small_text_change_breaks_static_skip(self):
        """静止画面跳过 OCR 后，小片文字变化必须立刻恢复识别。"""
        ocr = Mock()
        ocr.recognize.return_value = []
        watcher = WindowWatcher(
            ocr,
            Mock(),
            {
                "window_watch_interval_ms": 20,
                "window_watch_diff_threshold": 0.8,
                "target_language": "简体中文",
            },
            hwnd=404,
            profile="window",
        )
        still = np.zeros((40, 60, 3), dtype=np.uint8)
        with_text = still.copy()
        with_text[10:24, 12:48] = 255 - with_text[10:24, 12:48]
        self._run_watcher_frames(
            watcher, ocr, [still, still, still, with_text],
            [("ocr", 1), ("skip", 1), ("skip", 2), ("ocr", 2)],
        )
        # 第 1 帧识别；第 2、3 帧画面未变被跳过；第 4 帧出现文字立即识别
        self.assertEqual(ocr.recognize.call_count, 2)

    def test_annotation_overlay_is_always_excluded_from_capture(self):
        overlay = Mock()
        with (
            patch("app.ui.overlays._exclude_from_capture") as exclude,
            patch("app.ui.overlays._allow_capture") as allow,
        ):
            AnnotationOverlay._apply_capture_affinity(overlay)
            exclude.assert_called_once_with(overlay)
            allow.assert_not_called()

    def test_region_watch_restacks_every_visible_layer(self):
        app = App.__new__(App)
        app._watch_hwnd = None
        app._watch_region = (10, 20, 300, 180)
        app.subtitle = Mock()
        app.annotation = Mock()
        app.annotate_ctrl = Mock()
        app.region_frame = Mock()

        app._restack_watch_layer()

        app.subtitle.restack_layer.assert_called_once_with()
        app.annotation.restack_layer.assert_called_once_with()
        app.annotate_ctrl.restack_layer.assert_called_once_with()
        app.region_frame.restack_layer.assert_called_once_with()

    def test_ownerless_annotation_reasserts_topmost_without_focus(self):
        overlay = Mock()
        overlay._layer_owner = None
        overlay.isVisible.return_value = True
        with patch("app.ui.overlays.set_overlay_layer") as set_layer:
            AnnotationOverlay.restack_layer(overlay)
        set_layer.assert_called_once_with(overlay, None)

    def test_window_overlay_is_stacked_above_target_not_below(self):
        """SetWindowPos 的 hWndInsertAfter 是「插到其下方」；直接传目标
        句柄会把译文浮层压到被译窗口底下看不见，必须插到目标前驱之后。"""
        from PySide6.QtCore import Qt

        from app.ui import topmost

        widget = Mock()
        widget.windowFlags.return_value = Qt.WindowType.FramelessWindowHint
        widget.winId.return_value = 1111
        with (
            patch.object(topmost, "_GetWindow", return_value=2222),
            patch.object(topmost, "_set_window_owner", return_value=True),
            patch.object(topmost, "_set_window_pos", return_value=True) as swp,
        ):
            topmost.set_overlay_layer(widget, 3333)
        inserted = [call.args[1] for call in swp.call_args_list]
        self.assertIn(2222, inserted)      # 插到目标前驱之后 = 目标正上方
        self.assertNotIn(3333, inserted)   # 不得插到目标之后（下方）

    def test_restack_uses_predecessor_and_falls_back_to_top(self):
        from app.ui import topmost

        widget = Mock()
        widget.winId.return_value = 1111
        widget.isVisible.return_value = True
        with (
            patch.object(topmost, "_GetWindow", return_value=2222),
            patch.object(topmost, "_set_window_pos", return_value=True) as swp,
        ):
            topmost.restack_above_owner(widget, 3333)
        self.assertEqual(swp.call_args.args[1], 2222)

        # 目标已在最前（无前驱）或前驱就是浮层自身 → HWND_TOP
        for prev in (0, 1111):
            with (
                patch.object(topmost, "_GetWindow", return_value=prev),
                patch.object(topmost, "_set_window_pos", return_value=True) as swp,
            ):
                topmost.restack_above_owner(widget, 3333)
            self.assertEqual(swp.call_args.args[1], topmost._HWND_TOP)

    def test_clipboard_mime_clone_preserves_all_formats(self):
        source = QMimeData()
        source.setText("plain")
        source.setHtml("<b>rich</b>")
        source.setData("application/x-screen-translator-test", b"payload")
        cloned = _clone_mime_data(source)
        self.assertEqual(cloned.text(), "plain")
        self.assertEqual(cloned.html(), "<b>rich</b>")
        self.assertEqual(bytes(cloned.data("application/x-screen-translator-test")), b"payload")

    def test_preload_signal_crosses_from_python_thread(self):
        app = QApplication.instance() or QApplication([])
        bridge = _PreloadSignals()
        received = []
        bridge.status.connect(lambda *args: received.append(args))
        thread = threading.Thread(target=lambda: bridge.status.emit("ocr", "ok", "", 2))
        thread.start()
        thread.join()
        deadline = time.time() + 1
        while not received and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertEqual(received, [("ocr", "ok", "", 2)])

    def test_shutdown_has_abort_and_hard_deadlines(self):
        app = App.__new__(App)
        worker = Mock()
        worker.isRunning.return_value = True
        app._workers = [worker]
        app._retired_watchers = []
        app._preload_threads = []
        app.translate_win = Mock()
        app.translate_win.active_worker.return_value = None
        app.resources = Mock()
        app.log = Mock()
        app.qapp = Mock()
        app._shutdown_started_at = 100.0
        app._shutdown_abort_thread = None
        app._shutdown_server_thread = None
        app._shutdown_resources_closed = False
        abort_thread = Mock()
        abort_thread.is_alive.return_value = True

        with (
            patch("app.main.time.monotonic", return_value=100.0 + _SHUTDOWN_ABORT_SECONDS),
            patch("app.main.threading.Thread", return_value=abort_thread) as make_thread,
            patch("app.main.QTimer.singleShot") as schedule,
        ):
            app._poll_shutdown()

        make_thread.assert_called_once()
        abort_thread.start.assert_called_once_with()
        schedule.assert_called_once()
        app.qapp.quit.assert_not_called()

        with (
            patch("app.main.time.monotonic", return_value=100.0 + _SHUTDOWN_HARD_LIMIT_SECONDS),
            patch("app.main.QTimer.singleShot") as schedule,
        ):
            app._poll_shutdown()

        app.qapp.quit.assert_called_once_with()
        schedule.assert_not_called()

        app.qapp.quit.reset_mock()
        app._workers = []
        app._shutdown_abort_thread = None
        app._shutdown_resources_closed = True
        app._shutdown_server_thread = Mock()
        app._shutdown_server_thread.is_alive.return_value = True
        with (
            patch("app.main.time.monotonic", return_value=100.0 + _SHUTDOWN_HARD_LIMIT_SECONDS),
            patch("app.main.QTimer.singleShot") as schedule,
        ):
            app._poll_shutdown()
        app.qapp.quit.assert_called_once_with()
        schedule.assert_not_called()

    def test_cancelled_selection_clears_every_mode(self):
        app = App.__new__(App)
        app.log = Mock()
        for mode in ("screenshot", "region_watch"):
            app._pending_mode = mode
            app._on_region_cancelled()
            self.assertIsNone(app._pending_mode)

    def test_selection_start_failure_resets_pending_mode(self):
        app = App.__new__(App)
        app._quitting = False
        app._pending_mode = None
        app.selector = Mock()
        app.selector.isVisible.return_value = False
        app.selector.start.side_effect = OSError("capture unavailable")
        app.qapp = Mock()
        app.qapp.screens.return_value = []
        app.log = Mock()
        app._show_error = Mock()
        with patch("app.main.capture.configure_qt_screens"):
            app._start_select("region_watch")
        self.assertIsNone(app._pending_mode)
        app._show_error.assert_called_once_with("capture unavailable")

    def test_copy_shortcut_failure_does_not_abort_fallback_flow(self):
        app = App.__new__(App)
        app.log = Mock()
        app._word_marker = "marker"
        app._word_copy_kinds = ("ctrl_c",)
        app._word_copy_phase = 0
        clipboard = Mock()
        with (
            patch("app.main.QApplication.clipboard", return_value=clipboard),
            patch(
                "app.selection.send_copy_shortcut",
                side_effect=OSError("blocked"),
            ),
        ):
            app._word_fire_copy_phase()
        clipboard.setText.assert_called_once_with("marker")
        app.log.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
