# -*- coding: utf-8 -*-
"""Empirical test suite verifying fourth-audit P1 issues."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import numpy as np

from app.frame_detector import FrameChangeDetector
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


if __name__ == "__main__":
    unittest.main()
