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


if __name__ == "__main__":
    unittest.main()
