# -*- coding: utf-8 -*-
"""Empirical concurrency verification for non-blocking abort_inflight and session isolation."""

from __future__ import annotations

import time
import threading
import unittest
from unittest.mock import patch, MagicMock

from app.translator import Translator


class TestAbortInflightNonBlocking(unittest.TestCase):
    def test_abort_inflight_returns_immediately_while_translate_is_hung(self):
        """P0 核心验证：当 translate() 阻塞在慢网络时，UI 线程调用 abort_inflight() 必须在毫秒级返回且打破阻塞。"""
        translator = Translator(base_url="http://127.0.0.1:18080", timeout=10.0, cfg={"db_path": ":memory:"})
        translator._cache_get = lambda *args, **kwargs: None

        hung_event = threading.Event()
        request_interrupted = threading.Event()
        translate_exited = threading.Event()

        session = translator._get_session("watcher")

        # 模拟 session.post 阻塞在网络 I/O，当 session.close() 被调用时抛出 ConnectionError
        def mock_post(*args, **kwargs):
            hung_event.set()
            # 循环直到 session 被关闭
            while not request_interrupted.is_set():
                time.sleep(0.01)
                # 检查 session 连接池是否已关闭
                if getattr(session, "_is_closed", False):
                    raise ConnectionError("Connection aborted by client session close")
            return MagicMock(ok=True, json=lambda: {"choices": [{"message": {"content": "ok"}}]})

        # 劫持 close
        orig_close = session.close
        def mock_close():
            session._is_closed = True
            orig_close()

        session.close = mock_close
        session.post = mock_post

        def run_translation():
            try:
                translator.translate(f"Hung test text {time.time()}", target_language="简体中文", session_tag="watcher")
            except Exception:
                pass
            finally:
                translate_exited.set()

        t_worker = threading.Thread(target=run_translation, daemon=True)
        t_worker.start()

        # 等待 worker 线程进入慢网络阻塞并持有锁
        self.assertTrue(hung_event.wait(timeout=2.0), "Worker must enter hung post state")

        # 此时 worker 正在持有 translator._lock 并阻塞在 post
        # UI 线程调用 abort_inflight("watcher")
        t0 = time.time()
        translator.abort_inflight(tag="watcher")
        elapsed_ms = (time.time() - t0) * 1000

        # UI 线程绝不能被阻塞在 translator._lock 上（耗时必须严格小于 50ms）
        self.assertLess(elapsed_ms, 50.0, f"abort_inflight must return immediately (<50ms), took {elapsed_ms:.2f}ms")

        # worker 线程应该被打破阻塞并快速退出（耗时 < 500ms）
        self.assertTrue(translate_exited.wait(timeout=1.0), "Worker thread should exit promptly after abort")

    def test_session_isolation_does_not_abort_default_session(self):
        """P0/P1 会话隔离：中止 watcher 的推理请求绝不波及默认 session（划词/截图翻译）。"""
        translator = Translator(base_url="http://127.0.0.1:18080", timeout=10.0)

        default_session = translator._get_session("default")
        watcher_session = translator._get_session("watcher")

        default_closed = False
        watcher_closed = False

        def close_default():
            nonlocal default_closed
            default_closed = True

        def close_watcher():
            nonlocal watcher_closed
            watcher_closed = True

        default_session.close = close_default
        watcher_session.close = close_watcher

        # 仅中断 watcher
        translator.abort_inflight(tag="watcher")

        self.assertTrue(watcher_closed, "Watcher session must be closed")
        self.assertFalse(default_closed, "Default session must remain open and intact")


    def test_abort_cancels_queued_request_without_calling_http(self):
        """P0 核心竞态测试：在等锁期间被 abort 的请求，获得锁后必须立刻短路退出，绝不发 HTTP 请求。"""
        translator = Translator(base_url="http://127.0.0.1:18080", timeout=10.0)
        post_called = False

        session = translator._get_session("watcher")
        def mock_post(*args, **kwargs):
            nonlocal post_called
            post_called = True
            return MagicMock(ok=True, json=lambda: {"choices": [{"message": {"content": "ok"}}]})
        session.post = mock_post

        lock_held = threading.Event()
        release_lock = threading.Event()
        queued_started = threading.Event()
        result_returned = threading.Event()
        final_result = None

        # 1. 模拟划词翻译先占着 translator._lock
        def hold_lock():
            with translator._lock:
                lock_held.set()
                release_lock.wait()

        t_holder = threading.Thread(target=hold_lock, daemon=True)
        t_holder.start()
        self.assertTrue(lock_held.wait(timeout=2.0))

        # 2. Watcher 发起请求，进入等锁排队状态
        def run_watcher():
            nonlocal final_result
            queued_started.set()
            final_result = translator.translate("Waiting test", target_language="简体中文", session_tag="watcher")
            result_returned.set()

        t_watcher = threading.Thread(target=run_watcher, daemon=True)
        t_watcher.start()
        self.assertTrue(queued_started.wait(timeout=2.0))
        time.sleep(0.05)  # 确保 watcher 已经卡在 with self._lock 之前

        # 3. 用户此时停止 watcher，调用 abort_inflight("watcher")
        translator.abort_inflight("watcher")

        # 4. 划词翻译释放锁
        release_lock.set()
        t_holder.join(timeout=2.0)

        # 5. 等待 watcher 返回
        self.assertTrue(result_returned.wait(timeout=2.0), "Watcher must return after acquiring lock")
        self.assertEqual(final_result, "", "Cancelled request must return empty string without executing")
        self.assertFalse(post_called, "Cancelled request must NEVER invoke session.post()")

    def test_localhost_session_disables_trust_env(self):
        """P1 安全测试：所有本地 Session 必须禁用 trust_env，防止继承系统环境代理。"""
        translator = Translator(base_url="http://127.0.0.1:18080", timeout=10.0)
        session = translator._get_session("watcher")
        self.assertFalse(session.trust_env, "Local translator session must have trust_env=False")


if __name__ == "__main__":
    unittest.main()
