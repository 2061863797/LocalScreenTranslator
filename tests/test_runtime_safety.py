import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PySide6.QtCore import QCoreApplication, QMimeData

from app import capture
from app.llama_server import LlamaServer
from app.main import (
    App,
    _PreloadSignals,
    _SHUTDOWN_ABORT_SECONDS,
    _SHUTDOWN_HARD_LIMIT_SECONDS,
    _clone_mime_data,
)
from app.ui.overlays import AnnotationOverlay
from app.window_watcher import WindowWatcher, _frame_changed


class _FakeProcess:
    def __init__(self):
        self.pid = 123
        self.stdout = []
        self.returncode = None
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminate()


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
    def test_llama_device_selects_gpu_layers_without_another_runtime(self):
        server = object.__new__(LlamaServer)
        server._cfg = {"llama_device": "cpu", "n_gpu_layers": 99}
        server._cuda_available = None
        server._has_cuda_device = Mock(return_value=True)
        self.assertEqual(server._gpu_layers(Path("llama-server.exe")), 0)
        server._has_cuda_device.assert_not_called()

        server._cfg["llama_device"] = "auto"
        self.assertEqual(server._gpu_layers(Path("llama-server.exe")), 99)

        server._has_cuda_device.return_value = False
        self.assertEqual(server._gpu_layers(Path("llama-server.exe")), 0)

        server._cfg["llama_device"] = "gpu"
        with self.assertRaisesRegex(RuntimeError, "CUDA"):
            server._gpu_layers(Path("llama-server.exe"))

    def test_llama_timeout_reaps_spawned_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "llama-server.exe").write_bytes(b"x")
            model = root / "model.gguf"
            model.write_bytes(b"GGUF")
            server = LlamaServer({
                "llama_dir": str(root),
                "model_path": str(model),
                "server_host": "127.0.0.1",
                "server_port": 18080,
                "llama_device": "cpu",
            })
            proc = _FakeProcess()
            server.is_healthy = Mock(return_value=False)
            with patch("app.llama_server.subprocess.Popen", return_value=proc):
                with self.assertRaises(TimeoutError):
                    server.start(wait_seconds=0)
            self.assertTrue(proc.terminated)
            self.assertIsNone(server._proc)

    def test_llama_cold_start_can_be_cancelled_without_waiting_for_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "llama-server.exe").write_bytes(b"x")
            model = root / "model.gguf"
            model.write_bytes(b"GGUF")
            server = LlamaServer({
                "llama_dir": str(root),
                "model_path": str(model),
                "server_host": "127.0.0.1",
                "server_port": 18080,
                "llama_device": "cpu",
            })
            proc = _FakeProcess()
            server.is_healthy = Mock(return_value=False)

            def launch(*_args, **_kwargs):
                server._stop_requested.set()
                return proc

            with patch("app.llama_server.subprocess.Popen", side_effect=launch):
                with self.assertRaisesRegex(InterruptedError, "取消"):
                    server.start(wait_seconds=180)

            self.assertTrue(proc.terminated)
            self.assertIsNone(server._proc)

    def test_health_check_rejects_unrelated_http_service(self):
        server = object.__new__(LlamaServer)
        server.host, server.port = "127.0.0.1", 18080
        unrelated = Mock(status_code=200)
        unrelated.json.return_value = {"hello": "world"}
        with patch("app.llama_server.requests.get", return_value=unrelated):
            self.assertFalse(server.is_healthy())

    def test_llama_server_rejects_unmatched_model_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "llama-server.exe").write_bytes(b"x")
            model = root / "target_model.gguf"
            model.write_bytes(b"GGUF")
            server = LlamaServer({
                "llama_dir": str(root),
                "model_path": str(model),
                "server_host": "127.0.0.1",
                "server_port": 18080,
                "llama_device": "cpu",
            })
            server.is_healthy = Mock(return_value=True)
            server.check_model_match = Mock(return_value=False)
            with patch("app.llama_server.is_port_in_use", return_value=True):
                with patch("app.llama_server.subprocess.Popen") as mock_popen:
                    with self.assertRaisesRegex(RuntimeError, "不匹配"):
                        server.start()
                    mock_popen.assert_not_called()

    def test_llama_server_rejects_occupied_port_by_unrelated_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "llama-server.exe").write_bytes(b"x")
            model = root / "target_model.gguf"
            model.write_bytes(b"GGUF")
            server = LlamaServer({
                "llama_dir": str(root),
                "model_path": str(model),
                "server_host": "127.0.0.1",
                "server_port": 18080,
                "llama_device": "cpu",
            })
            server.is_healthy = Mock(return_value=False)
            with patch("app.llama_server.is_port_in_use", return_value=True):
                with patch("app.llama_server.subprocess.Popen") as mock_popen:
                    with self.assertRaisesRegex(RuntimeError, "已被其它本地程序占用"):
                        server.start()
                    mock_popen.assert_not_called()

    def test_llama_server_reuses_matching_model_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "llama-server.exe").write_bytes(b"x")
            model = root / "target_model.gguf"
            model.write_bytes(b"GGUF")
            server = LlamaServer({
                "llama_dir": str(root),
                "model_path": str(model),
                "server_host": "127.0.0.1",
                "server_port": 18080,
                "llama_device": "cpu",
            })
            server.is_healthy = Mock(return_value=True)
            server.check_model_match = Mock(return_value=True)
            with patch("app.llama_server.subprocess.Popen") as mock_popen:
                server.start()
                mock_popen.assert_not_called()

    def test_check_model_match_supports_v1_models_and_props(self):
        server = object.__new__(LlamaServer)
        server.host, server.port = "127.0.0.1", 18080
        server.model_path = Path("D:/models/HY-MT1.5-1.8B.gguf")

        # 1. 命中 /v1/models
        resp_v1 = Mock(status_code=200)
        resp_v1.json.return_value = {"data": [{"id": "HY-MT1.5-1.8B"}]}
        with patch("app.llama_server.requests.get", return_value=resp_v1):
            self.assertTrue(server.check_model_match())

        # 2. 命中 /props 的 default_generation_settings
        resp_props = Mock(status_code=200)
        resp_props.json.return_value = {"default_generation_settings": {"model": "hy-mt1.5-1.8b.gguf"}}
        with patch("app.llama_server.requests.get", side_effect=[Mock(status_code=404), Mock(status_code=404), resp_props]):
            self.assertTrue(server.check_model_match())

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

    def test_region_annotation_mask_restores_previous_clean_pixels(self):
        watcher = WindowWatcher(
            Mock(),
            Mock(),
            {},
            region=(10, 20, 30, 40),
            profile="region",
            display_mode="annotate",
        )
        clean = np.full((40, 30, 3), 20, dtype=np.uint8)
        watcher._remove_annotation_overlay(clean)

        mask = np.zeros((40, 30), dtype=np.uint8)
        mask[15:20, 10:15] = 255
        watcher.set_annotation_mask(mask)
        captured = clean.copy()
        captured[13:22, 8:17] = 240
        captured[0, 0] = 99

        restored = watcher._remove_annotation_overlay(captured)
        self.assertTrue(np.all(restored[13:22, 8:17] == 20))
        self.assertTrue(np.all(restored[0, 0] == 99))

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
                "window_annotate_skip_target_lang": False,
                "target_language": "简体中文",
            },
            hwnd=404,
            profile="window",
        )
        image = np.zeros((20, 30, 3), dtype=np.uint8)
        frames = 0

        def _grab():
            nonlocal frames
            frames += 1
            if frames >= 8:
                watcher._running = False
            return ((0, 0, 30, 20), image)

        watcher._grab = _grab
        ocr.recognize.return_value = []
        watcher.run()

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
                "window_annotate_skip_target_lang": False,
                "target_language": "简体中文",
            },
            hwnd=404,
            profile="window",
        )
        blank = np.zeros((20, 30, 3), dtype=np.uint8)
        changed = np.full((20, 30, 3), 255, dtype=np.uint8)
        frames = 0

        def _grab():
            nonlocal frames
            frames += 1
            if frames >= 3:
                watcher._running = False
            return ((0, 0, 30, 20), changed if frames == 3 else blank)

        watcher._grab = _grab
        ocr.recognize.return_value = []
        watcher.run()

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
                "window_annotate_skip_target_lang": False,
                "target_language": "简体中文",
            },
            hwnd=404,
            profile="window",
        )
        still = np.zeros((40, 60, 3), dtype=np.uint8)
        with_text = still.copy()
        with_text[10:24, 12:48] = 255 - with_text[10:24, 12:48]
        frames = 0

        def _grab():
            nonlocal frames
            frames += 1
            if frames >= 4:
                watcher._running = False
                return ((0, 0, 60, 40), with_text)
            return ((0, 0, 60, 40), still)

        watcher._grab = _grab
        watcher.run()
        # 第 1 帧识别；第 2、3 帧画面未变被跳过；第 4 帧出现文字立即识别
        self.assertEqual(ocr.recognize.call_count, 2)

    def test_annotation_mask_sync_is_limited_to_active_region_notes(self):
        app = App.__new__(App)
        app._watcher = Mock()
        app.annotation = Mock()
        mask = np.ones((20, 30), dtype=np.uint8)
        app.annotation.capture_mask.return_value = mask
        app._watch_region = (0, 0, 300, 200)
        app._watch_annotate = True
        app.cfg = {"annotate_capture_visible": True}

        app._sync_annotation_mask()
        app._watcher.set_annotation_mask.assert_called_once_with(mask)

        app._watcher.set_annotation_mask.reset_mock()
        app._watch_annotate = False
        app._sync_annotation_mask()
        app._watcher.set_annotation_mask.assert_called_once_with(None)

        # 浮层被排除捕获（默认）时不生成遮罩：OCR 抓屏看不到译文
        app._watcher.set_annotation_mask.reset_mock()
        app.annotation.capture_mask.reset_mock()
        app._watch_annotate = True
        app.cfg = {"annotate_capture_visible": False}
        app._sync_annotation_mask()
        app._watcher.set_annotation_mask.assert_called_once_with(None)
        app.annotation.capture_mask.assert_not_called()

    def test_annotation_overlay_capture_affinity_follows_setting(self):
        overlay = Mock()
        with (
            patch("app.ui.overlays._exclude_from_capture") as exclude,
            patch("app.ui.overlays._allow_capture") as allow,
        ):
            overlay._capture_visible = False
            AnnotationOverlay._apply_capture_affinity(overlay)
            exclude.assert_called_once_with(overlay)
            allow.assert_not_called()

            overlay._capture_visible = True
            AnnotationOverlay._apply_capture_affinity(overlay)
            allow.assert_called_once_with(overlay)

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
        app = QCoreApplication.instance() or QCoreApplication([])
        bridge = _PreloadSignals()
        received = []
        bridge.status.connect(lambda *args: received.append(args))
        thread = threading.Thread(target=lambda: bridge.status.emit("ocr", "ok", ""))
        thread.start()
        thread.join()
        deadline = time.time() + 1
        while not received and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertEqual(received, [("ocr", "ok", "")])

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
