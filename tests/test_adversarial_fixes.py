"""对抗审核发现的取消、完整译文、上下文和 Qt 生命周期回归。"""

import ctypes as C
import gc
import os
import threading
import unittest
import warnings
import weakref
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from app.config import DEFAULTS
from app.latest_frame_buffer import FramePacket
from app.main import App
from app.ocr_engine import OcrLine
from app.storage import Storage
from app.translation_runtime.contracts import (
    IncompleteTranslationError, TranslationContextError, TranslationRequest, TranslationResult,
)
from app.translation_runtime.llama_native import LlamaNativeBackend
from app.translation_runtime.prompts import clean_output
from app.translation_runtime.router import TranslationRouter
from app.window_watcher import WindowWatcher


class NativeContextTests(unittest.TestCase):
    def backend(self, max_tokens, token_count):
        backend = LlamaNativeBackend("runtime/llama-native", dict(DEFAULTS, max_tokens=max_tokens))
        backend._model = backend._ctx = 1
        backend._prompt = Mock(return_value=b"prompt")
        dll = Mock()
        dll.llama_n_ctx.return_value = 2048
        dll.llama_tokenize.side_effect = (
            lambda vocab, text, length, tokens, capacity, *args:
            token_count if capacity >= token_count else -token_count
        )
        dll.llama_decode.return_value = 0
        dll.llama_sampler_sample.return_value = 1
        dll.llama_vocab_is_eog.return_value = False

        def token_piece(vocab, token, buffer, *args):
            C.memmove(buffer, b"x", 1)
            return 1

        dll.llama_token_to_piece.side_effect = token_piece
        backend._dll = dll
        return backend

    def test_large_output_presets_use_remaining_context(self):
        request = TranslationRequest("Hello", "自动", "简体中文", "ai", "model", 0, lambda: False)
        for max_tokens in (512, 2048, 4096, 8192):
            with self.subTest(max_tokens=max_tokens):
                backend = self.backend(max_tokens, 2046)
                self.assertEqual(backend.translate(request).text, "xx")
                self.assertEqual(backend._dll.llama_sampler_sample.call_count, 2)

    def test_prompt_without_output_space_raises_typed_context_error(self):
        backend = self.backend(8192, 2048)
        request = TranslationRequest("Hello", "自动", "简体中文", "ai", "model", 0, lambda: False)
        with self.assertRaises(TranslationContextError):
            backend.translate(request)
        backend._dll.llama_decode.assert_not_called()


class _RouterTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        self.storage = Storage(":memory:")
        self.router = TranslationRouter(dict(DEFAULTS), self.storage)
        self.router._ai.preload = Mock()
        self.router._ai.close = Mock()
        self.router.preload()

    def tearDown(self):
        self.router.close()
        self.storage.close()

    @staticmethod
    def result(request, text):
        return TranslationResult(text, request.source_language, request.target_language,
                                 request.mode, request.model_id, request.session_version)


class RouterCompletenessTests(_RouterTestCase):
    def test_chinese_context_error_splits_source(self):
        seen = []

        def infer(request):
            seen.append(request.text)
            if len(request.text) > 256:
                raise TranslationContextError("翻译提示超过 llama.cpp 上下文上限")
            return self.result(request, "part")

        self.router._ai.translate = Mock(side_effect=infer)
        source = "汉" * 500
        self.assertEqual(self.router.translate(source), "part\npart")
        self.assertEqual(seen, [source, source[:250], source[250:]])

    def test_empty_fragment_is_not_cached_as_whole_source(self):
        source = "A" * 350 + " " + "B" * 350
        self.router._ai.translate = Mock(side_effect=lambda request: self.result(
            request, "甲" if request.text.startswith("A") else ""
        ))
        with self.assertRaises(IncompleteTranslationError):
            self.router.translate(source)
        self.assertEqual(self.storage.count_translation_cache(), 0)
        self.router._ai.translate.side_effect = lambda request: self.result(
            request, "甲" if request.text.startswith("A") else "乙"
        )
        self.assertEqual(self.router.translate(source), "甲\n乙")
        calls = self.router._ai.translate.call_count
        self.assertEqual(self.router.translate(source), "甲\n乙")
        self.assertEqual(self.router._ai.translate.call_count, calls)

    def test_old_incomplete_cache_namespace_is_not_reused(self):
        model = f"{self.router.model_id}:source=自动"
        self.router.cache.put("Hello", "简体中文", model, "local-ai-v1", "残缺旧结果")
        self.router._ai.translate = Mock(side_effect=lambda request: self.result(request, "完整新结果"))
        self.assertEqual(self.router.translate("Hello"), "完整新结果")
        self.router._ai.translate.assert_called_once()


class LiveCompletenessTests(_RouterTestCase):
    def run_frames(self, mode, texts, on_create=None):
        watcher = WindowWatcher(Mock(), self.router, dict(DEFAULTS), region=(0, 0, 64, 32),
                                profile="region", display_mode=mode)
        self.addCleanup(watcher.deleteLater)
        if on_create is not None:
            on_create(watcher)
        frames = iter(texts)
        current = {"text": ""}

        def packet(timeout=None):
            try:
                text, revision = next(frames)
            except StopIteration:
                watcher._running = False
                watcher._consumer_stop_event.set()
                return None
            current["text"] = text
            watcher._content_revision = watcher._content_epoch = revision
            frame = np.full((32, 64, 3), revision, dtype=np.uint8)
            return FramePacket(frame, epoch=revision, has_changed=True, changed_ratio=1.0,
                               work_revision=revision, content_revision=revision)

        watcher._frame_buffer.get = packet
        watcher._recognize_live_lines = Mock(side_effect=lambda *args: [
            OcrLine(line, .99, (0, i * 12, 20, i * 12 + 10))
            for i, line in enumerate(current["text"].splitlines())
        ])
        watcher._result_manager.dispatch_annotations = Mock(return_value=True)
        watcher._result_manager.dispatch_subtitle = Mock(return_value=True)
        watcher._result_manager.dispatch_history = Mock(return_value=True)
        watcher._inference_loop()
        return watcher

    def dispatcher(self, watcher, mode):
        manager = watcher._result_manager
        return manager.dispatch_annotations if mode == "annotate" else manager.dispatch_subtitle

    def test_lines_are_shown_one_by_one_and_empty_line_falls_back_to_source(self):
        for mode in ("annotate", "subtitle"):
            with self.subTest(mode=mode):
                self.router.clear_cache()
                self.router._ai.translate = Mock(side_effect=lambda request: self.result(
                    request, "你好" if request.text == "Hello" else ""
                ))
                watcher = self.run_frames(mode, [("Hello\nWorld", 1)] * 10)
                # 每行只翻译一次，空译文不重试
                self.assertEqual([c.args[0].text for c in self.router._ai.translate.call_args_list],
                                 ["Hello", "World"])
                shown = [c.args[0] for c in self.dispatcher(watcher, mode).call_args_list]
                if mode == "annotate":
                    self.assertEqual(shown, [[((0, 0, 20, 10), "你好")],
                                             [((0, 0, 20, 10), "你好"), ((0, 12, 20, 22), "World")]])
                else:
                    self.assertEqual(shown, ["你好", "你好\nWorld"])
                history = watcher._result_manager.dispatch_history
                history.assert_called_once()
                self.assertEqual(history.call_args.args[:2], ("Hello\nWorld", "你好\nWorld"))

    def test_untranslated_frame_shows_source_without_history(self):
        self.router._ai.translate = Mock(side_effect=lambda request: self.result(
            request, "旧译文" if request.text == "Previous" else ""
        ))
        watcher = self.run_frames("annotate", [("Previous", 1)] + [("Untranslated", 2)] * 10)
        self.assertEqual([c.args[0].text for c in self.router._ai.translate.call_args_list],
                         ["Previous", "Untranslated"])
        self.assertEqual(watcher._result_manager.dispatch_annotations.call_args.args[0],
                         [((0, 0, 20, 10), "Untranslated")])
        self.assertEqual(watcher._result_manager.dispatch_history.call_count, 1)

    def test_pause_mid_batch_drops_remaining_lines_and_retranslates(self):
        for mode in ("annotate", "subtitle"):
            with self.subTest(mode=mode):
                self.router.clear_cache()
                holder, seen = {}, []

                def infer(request):
                    seen.append(request.text)
                    if len(seen) == 1:
                        # 翻译第一行途中用户暂停/切换语言：会话被轮换
                        holder["watcher"]._rotate_translation_session()
                    return self.result(request, {"Hello": "你好", "World": "世界"}[request.text])

                self.router._ai.translate = Mock(side_effect=infer)
                watcher = self.run_frames(mode, [("Hello\nWorld", 1)] * 10,
                                          on_create=lambda w: holder.update(watcher=w))
                # 被取消的第一批不显示；下一帧重新翻译并完整显示
                self.assertEqual(seen, ["Hello", "Hello", "World"])
                last = self.dispatcher(watcher, mode).call_args.args[0]
                expected = ([((0, 0, 20, 10), "你好"), ((0, 12, 20, 22), "世界")]
                            if mode == "annotate" else "你好\n世界")
                self.assertEqual(last, expected)
                self.assertEqual(self.dispatcher(watcher, mode).call_count, 2)
                watcher._result_manager.dispatch_history.assert_called_once()

    def test_translation_error_shows_source_then_retries_next_frame(self):
        calls = []

        def infer(request):
            calls.append(request.text)
            if len(calls) == 1:
                raise RuntimeError("模型尚未加载完")
            return self.result(request, "你好")

        self.router._ai.translate = Mock(side_effect=infer)
        watcher = self.run_frames("subtitle", [("Hello", 1)] * 10)
        self.assertEqual(calls, ["Hello", "Hello"])
        shown = [c.args[0] for c in watcher._result_manager.dispatch_subtitle.call_args_list]
        self.assertEqual(shown, ["Hello", "你好"])
        watcher._result_manager.dispatch_history.assert_called_once()


class WatcherLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def test_stopped_watcher_and_internal_bridges_are_destroyed(self):
        app = App.__new__(App)
        app._retired_watchers = []
        app._stopping_watch = False
        app.qapp = self.qapp
        app.log = Mock()
        watcher = WindowWatcher(Mock(), Mock(), {}, region=(0, 0, 64, 32))
        seen = []
        watcher.subtitle_ready[str, int, int].connect(
            lambda text, gen, rev, w=watcher: seen.append(text)
        )
        watcher._running = False
        watcher.start()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            self.assertTrue(app._join_watcher(watcher))
        watcher.subtitle_ready[str, int, int].emit("late", 1, 1)
        self.assertEqual(seen, [])
        watcher_ref, result_ref = weakref.ref(watcher), weakref.ref(watcher.result_manager)
        del watcher
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qapp.processEvents()
        gc.collect()
        self.assertIsNone(watcher_ref())
        self.assertIsNone(result_ref())

    def test_retired_watcher_is_destroyed_after_thread_finishes(self):
        entered, release = threading.Event(), threading.Event()

        class HeldWatcher(WindowWatcher):
            def run(self):
                entered.set()
                release.wait(3)

        app = App.__new__(App)
        app._retired_watchers = []
        watcher = HeldWatcher(Mock(), Mock(), {}, region=(0, 0, 64, 32))
        watcher.start()
        try:
            self.assertTrue(entered.wait(1))
            app._retain_watcher(watcher)
            self.assertIn(watcher, app._retired_watchers)
            self.assertIsNot(getattr(watcher, "_delete_scheduled", False), True)
        finally:
            release.set()
            self.assertTrue(watcher.wait(3000))
        watcher_ref = weakref.ref(watcher)
        self.qapp.processEvents()
        self.assertEqual(app._retired_watchers, [])
        del watcher
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()
        self.assertIsNone(watcher_ref())


class DialogueCleaningTests(unittest.TestCase):
    def test_normal_dialogue_prefixes_are_preserved(self):
        for text in ("Of course I can help you.", "没问题，我明天会到。", "Sure, I'll come.",
                     "当然可以。", "好的\n我们明天见。", "Sure translation is difficult."):
            with self.subTest(text=text):
                self.assertEqual(clean_output(text), text)
