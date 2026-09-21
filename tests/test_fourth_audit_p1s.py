# -*- coding: utf-8 -*-
"""Empirical test suite verifying fourth-audit P1 issues."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import numpy as np

from app.frame_detector import FrameChangeDetector, FrameDiffResult
from app.translation_manager import TranslationManager
from app.llama_server import LlamaServer


class TestFourthAuditP1s(unittest.TestCase):
    def test_dynamic_noise_does_not_invalidate_content(self):
        """P1 验证：背景微粒噪点绝不标记 invalidates_content，彻底根除 Result Starvation。"""
        detector = FrameChangeDetector(step=4, pixel_threshold=16, min_changed_pixels=40, min_changed_ratio=0.0002)

        # 1080p 纯灰底图
        frame1 = np.full((1080, 1920, 3), 128, dtype=np.uint8)
        # 仅修改 5 个像素（模拟微粒动画或光标闪烁）
        frame2 = frame1.copy()
        frame2[100:105, 100:101] = 255

        res = detector.detect(frame1, frame2)
        self.assertFalse(res.has_changed, "微粒噪点不应判定为视觉变动")
        self.assertFalse(res.invalidates_content, "微粒噪点坚决不应使内容失效，否则在途翻译将被误删导致饥饿")

        # 大面积切屏（改动 50% 画面）
        frame3 = frame1.copy()
        frame3[0:540, :] = 0
        res_major = detector.detect(frame1, frame3)
        self.assertTrue(res_major.has_changed, "大面积变动应判定为视觉变动")
        self.assertTrue(res_major.invalidates_content, "大面积切屏（ratio>=0.35）应正确判定内容失效")

    def test_translation_manager_cache_isolation_by_target(self):
        """P1 验证：行缓存必须按 (source, target) 复合键隔离，防止多语言热切换串味。"""
        fake_translator = MagicMock()
        def mock_translate_lines(lines, target, session_tag="default"):
            return [f"[{target}]{line}" for line in lines]
        fake_translator.translate_lines = mock_translate_lines

        tm = TranslationManager(fake_translator)

        # 步骤 1: 翻译为简体中文
        lines_zh = [MagicMock(text="Save File", box=[0, 0, 100, 20])]
        items_zh, _ = tm.translate_annotations(lines_zh, "简体中文")
        self.assertEqual(items_zh[0][1], "[简体中文]Save File")

        # 步骤 2: 热切换目标语言为日语，必须返回日语译文，不能复用旧的中文译文
        lines_ja = [MagicMock(text="Save File", box=[0, 0, 100, 20])]
        items_ja, _ = tm.translate_annotations(lines_ja, "日语")
        self.assertEqual(items_ja[0][1], "[日语]Save File", "切换目标语后行缓存不得返回旧语言译文！")

    def test_llama_server_is_ready_validates_both_health_and_model(self):
        """P1 验证：is_ready() 必须同时核验健康与模型身份。"""
        server = LlamaServer(cfg={
            "server_port": 18080,
            "llama_dir": "runtime/llama",
            "model_path": "runtime/models/HY-MT1.5-1.8B-Q4_K_M.gguf",
        })

        with patch.object(server, "is_healthy", return_value=True), \
             patch.object(server, "check_model_match", return_value=False):
            self.assertFalse(server.is_ready(), "健康但模型不匹配时，is_ready 必须返回 False 阻断误发请求")

        with patch.object(server, "is_healthy", return_value=False), \
             patch.object(server, "check_model_match", return_value=True):
            self.assertFalse(server.is_ready(), "不健康时必须返回 False")

        with patch.object(server, "is_healthy", return_value=True), \
             patch.object(server, "check_model_match", return_value=True):
            self.assertTrue(server.is_ready(), "健康且模型匹配时返回 True")

    def test_shape_mismatch_invalidates_content(self):
        """P1 验证：窗口尺寸改变（shape mismatch）必须 100% 标记 invalidates_content=True。"""
        detector = FrameChangeDetector()
        prev = np.zeros((400, 600, 3), dtype=np.uint8)
        curr = np.zeros((500, 700, 3), dtype=np.uint8)
        res = detector.detect(prev, curr)
        self.assertTrue(res.has_changed)
        self.assertTrue(res.invalidates_content, "窗口尺寸变化必须强制宣告内容失效，防止旧翻译被当 fresh 上屏")

    def test_dual_revisions_and_text_box_intersection(self):
        """P0/P1 双版本号核心验证：变动 ROI 命中字幕行淘汰旧译文，背景晃动保留在途字幕。"""
        from app.window_watcher import WindowWatcher
        from app.scene_text_state import SceneTextState

        watcher = WindowWatcher(
            ocr=MagicMock(),
            translator=MagicMock(),
            cfg={},
            profile="region",
            hwnd=None,
            region=(0, 0, 1920, 1080),
        )
        # 预先注入上一轮识别到的字幕行（位于底部 y=900~950）
        mock_line = MagicMock()
        mock_line.box = [400, 900, 1200, 950]
        watcher.scene_text_state.update_full([mock_line])

        # 场景 A: 字幕区域发生变动（ROI: [450, 910, 800, 940]）
        diff_sub = FrameDiffResult(
            has_changed=True,
            changed_ratio=0.03,  # 仅占 3% 画面
            changed_pixels=5000,
            roi_box=(450, 910, 800, 940),
            invalidates_content=False,
        )
        self.assertTrue(
            watcher._is_content_invalidated(diff_sub, True),
            "变动 ROI 与字幕行相交时，必须判定 invalidates_content=True（作废旧字幕在途请求）"
        )

        # 场景 B: 仅背景动画变动（ROI: [100, 100, 300, 300]，完全远离字幕行）
        diff_bg = FrameDiffResult(
            has_changed=True,
            changed_ratio=0.05,  # 占 5% 画面
            changed_pixels=8000,
            roi_box=(100, 100, 300, 300),
            invalidates_content=False,
        )
        self.assertFalse(
            watcher._is_content_invalidated(diff_bg, True),
            "背景晃动且未触碰字幕行时，绝不作废在途字幕，彻底根除 Result Starvation"
        )

    def test_pause_freezes_generation_and_bumps_content_revision(self):
        """P2 验证：暂停时立即作废在途世代与内容版本，恢复时清空底图以强制刷新。"""
        from app.window_watcher import WindowWatcher

        watcher = WindowWatcher(
            ocr=MagicMock(),
            translator=MagicMock(),
            cfg={},
            profile="region",
            hwnd=None,
            region=(0, 0, 1920, 1080),
        )
        rev0 = watcher.content_revision
        gen0 = watcher.generation_tracker.next_generation()

        # 暂停
        watcher.set_paused(True)
        self.assertGreater(watcher.content_revision, rev0, "暂停时必须自增 content_revision 以作废在途结果")
        self.assertFalse(watcher.generation_tracker.is_active(gen0), "暂停时必须重置世代使得在途请求变 stale")

        # 恢复
        watcher._last_captured_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        watcher.set_paused(False)
        self.assertIsNone(watcher._last_captured_frame, "恢复时必须清空上一捕获帧，强制执行全量刷新")

    def test_disjoint_animations_large_bounding_box_does_not_starve_subtitle(self):
        """P1 验证：左上角与右下角动画形成覆盖全屏的巨型 bounding box 时，字幕框内无变动绝不误作废在途结果。"""
        from app.window_watcher import WindowWatcher

        watcher = WindowWatcher(
            ocr=MagicMock(),
            translator=MagicMock(),
            cfg={},
            profile="region",
            hwnd=None,
            region=(0, 0, 1920, 1080),
        )
        # 底部字幕框 (y: 900~950, x: 400~1200)
        mock_line = MagicMock()
        mock_line.box = [400, 900, 1200, 950]
        watcher.scene_text_state.update_full([mock_line])

        # 构造 diff_mask (降采样 480x270)，仅在左上角 (0,0) 和右下角 (269, 479) 有变动
        mask = np.zeros((270, 480), dtype=bool)
        mask[5:15, 5:15] = True       # 左上角动画
        mask[250:260, 460:470] = True # 右下角动画

        diff_res = FrameDiffResult(
            has_changed=True,
            changed_ratio=0.01,
            changed_pixels=200,
            roi_box=(0, 0, 1920, 1080),  # 虚假的整屏大 bounding box
            invalidates_content=False,
            diff_mask=mask,
        )

        # 验证：虽然 roi_box 与字幕行相交，但 has_change_in_boxes 准确判定字幕内部像素未被改动！
        self.assertFalse(
            watcher._is_content_invalidated(diff_res, True),
            "分散动画形成虚假大 ROI 时，只要字幕框内无变动，绝不作废在途翻译，彻底杜绝 Result Starvation！"
        )

    def test_small_font_or_hud_sensitive_detection(self):
        """P1/P2 验证：微小数字/HUD 变化未达到全局 40 像素噪点门槛，若发生在既有文字框内，依然灵敏触发变动。"""
        detector = FrameChangeDetector(step=4, min_changed_pixels=40)
        prev = np.zeros((400, 600, 3), dtype=np.uint8)
        curr = prev.copy()
        # 仅修改 12 个物理像素（对应降采样后约 2~3 个采样像素）
        curr[100:104, 100:103] = 255
        text_box = (90, 90, 120, 120)

        # 无 prior text boxes 时，被全局 40 像素过滤
        res_no_box = detector.detect(prev, curr)
        self.assertFalse(res_no_box.has_changed, "微小改动在全图模式下被正常当噪点过滤")

        # 传入该文字框后，针对已知文字区域提升灵敏度，立即捕获
        res_with_box = detector.detect(prev, curr, prior_text_boxes=[text_box])
        self.assertTrue(res_with_box.has_changed, "文字框内部的微小数值变化必须即时捕获，不需等 5 帧周期轮询！")

    def test_sticky_cancellation_and_unique_session_tags(self):
        """P1 验证：cancellation 必须具粘性（已 abort 的 tag 绝不可被新调用重置为未取消），且 watcher 拥有唯一 session tag。"""
        from app.translator import Translator
        from app.window_watcher import WindowWatcher

        tr = Translator(base_url="http://127.0.0.1:18080")
        tag = "test_tag_1"
        tr.abort_inflight(tag)

        # 粘性测试：abort 后再次获取 cancel_event，必须依然是 is_set() == True！
        evt = tr._get_cancel_event(tag)
        self.assertTrue(evt.is_set(), "已 abort 的 tag 必须保持永久粘性取消，旧任务绝不可重新建立未取消状态")

        # 独立 session tag 测试
        w1 = WindowWatcher(MagicMock(), tr, {}, profile="region", hwnd=None, region=(0, 0, 100, 100))
        w2 = WindowWatcher(MagicMock(), tr, {}, profile="region", hwnd=None, region=(0, 0, 100, 100))
        self.assertNotEqual(w1._translation_session_tag, w2._translation_session_tag, "每个 watcher 必须拥有独立唯一的 session tag")

    def test_pause_resume_then_translate_succeeds(self):
        """P0 修复验证：同一个 watcher 在暂停并恢复后，绝不能因为旧 tag 的 Sticky Cancel 导致翻译永久失效。"""
        from app.translator import Translator
        from app.window_watcher import WindowWatcher

        fake_resp = MagicMock()
        fake_resp.ok = True
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "世界你好"}}]
        }

        tr = Translator(base_url="http://127.0.0.1:18080", cfg={"db_path": ":memory:"})
        tr._cache_get = lambda *args, **kwargs: None
        w = WindowWatcher(MagicMock(), tr, {}, profile="region", hwnd=None, region=(0, 0, 100, 100))
        tag0 = w._translation_session_tag

        # 暂停 watcher
        w.set_paused(True)
        # 旧 tag 必须被粘性取消
        self.assertTrue(tr._get_cancel_event(tag0).is_set())

        # 恢复 watcher
        w.set_paused(False)
        tag1 = w._translation_session_tag
        self.assertNotEqual(tag0, tag1, "恢复后必须生成全新的 session tag")
        self.assertFalse(tr._get_cancel_event(tag1).is_set(), "新 tag 绝对不能处于已取消状态")

        # 验证新 tag 的翻译请求必须能够成功执行并返回译文
        with patch("requests.Session.post", return_value=fake_resp):
            res = tr.translate("Hello world", session_tag=tag1)
            self.assertEqual(res, "世界你好", "恢复后的新 session 必须能顺利完成翻译")

    def test_target_language_switch_then_translate_succeeds(self):
        """P0/P1 修复验证：运行中切换目标语言后，同一个 watcher 能够继续成功发起翻译。"""
        from app.translator import Translator
        from app.window_watcher import WindowWatcher

        fake_resp = MagicMock()
        fake_resp.ok = True
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "こんにちは世界"}}]
        }

        tr = Translator(base_url="http://127.0.0.1:18080", cfg={"db_path": ":memory:"})
        tr._cache_get = lambda *args, **kwargs: None
        w = WindowWatcher(MagicMock(), tr, {}, profile="region", hwnd=None, region=(0, 0, 100, 100))
        old_tag = w._translation_session_tag

        w.on_target_language_changed()
        new_tag = w._translation_session_tag
        self.assertNotEqual(old_tag, new_tag)
        self.assertTrue(tr._get_cancel_event(old_tag).is_set())
        self.assertFalse(tr._get_cancel_event(new_tag).is_set())

        with patch("requests.Session.post", return_value=fake_resp):
            res = tr.translate("Hello world", target_language="日语", session_tag=new_tag)
            self.assertEqual(res, "こんにちは世界")

    def test_rotate_translation_session_isolates_stale_requests(self):
        """P1 验证：_rotate_translation_session 释放旧 session tag 阻断旧任务并开启新任务。"""
        from app.translator import Translator
        from app.window_watcher import WindowWatcher

        tr = Translator(base_url="http://127.0.0.1:18080")
        w = WindowWatcher(MagicMock(), tr, {}, profile="region", hwnd=None, region=(0, 0, 100, 100))
        t1 = w._translation_session_tag

        t2 = w._rotate_translation_session()
        self.assertNotEqual(t1, t2)
        self.assertTrue(tr._get_cancel_event(t1).is_set())
        self.assertFalse(tr._get_cancel_event(t2).is_set())

    def test_subtitle_new_line_bottom_expansion(self):
        """P1 验证：字幕模式下向下扩展一行行高，精准捕捉原字幕下方新出现的下一行字幕。"""
        boxes = [(100, 200, 500, 240)]  # 第一行字幕 y=200~240，高度 40px
        diff_mask = np.zeros((200, 300), dtype=bool)

        # 在下一行位置出现新字幕像素 (y=250~270，对应 step=4 下的 sy=62~67)
        diff_mask[63:66, 30:60] = True

        res = FrameDiffResult(
            has_changed=True,
            changed_ratio=0.01,
            changed_pixels=90,
            roi_box=(120, 252, 240, 268),
            invalidates_content=False,
            diff_mask=diff_mask,
        )

        # 默认不扩展时，不命中
        self.assertFalse(res.has_change_in_boxes(boxes, step=4, expand_bottom_ratio=0.0, margin_bottom_px=0))

        # 字幕模式下向下扩一行（expand_bottom_ratio=1.0, margin_bottom_px=24），命中！
        self.assertTrue(res.has_change_in_boxes(boxes, step=4, expand_bottom_ratio=1.0, margin_bottom_px=24))

    def test_model_copy_worker_interruption_and_cleanup(self):
        """P2 验证：ModelCopyWorker 支持 isInterruptionRequested()，并安全清理临时文件。"""
        from pathlib import Path
        import tempfile
        import time
        from app.ui.windows import ModelCopyWorker

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "source.gguf"
            dest = Path(td) / "dest.gguf"
            # 写入 8MB 数据
            src.write_bytes(b"x" * (8 * 1024 * 1024))

            worker = ModelCopyWorker(src, dest)
            finished_args = []
            worker.copy_finished.connect(lambda s, p, e: finished_args.append((s, p, e)))

            # 启动并在 5ms 后请求中断
            worker.start()
            time.sleep(0.005)
            worker.requestInterruption()
            worker.wait(2000)

            # 验证：临时文件被清理，copy_finished 发射 False
            temp_dest = dest.with_name(f"{dest.stem}.importing.gguf")
            self.assertFalse(temp_dest.exists(), "中断后临时文件必须被彻底清理删除")
            if finished_args:
                self.assertFalse(finished_args[0][0], "中断后必须发射失败/取消信号")


if __name__ == "__main__":
    unittest.main()
