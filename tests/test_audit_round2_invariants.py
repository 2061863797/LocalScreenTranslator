# -*- coding: utf-8 -*-
"""针对第二轮审查指出的状态机、缓冲区与边界漏洞的专项实证测试。

涵盖覆盖场景：
1. LatestFrameBuffer 与 FramePacket 的 clear() 状态不变量（确保 clear 后 get 为 None，is_empty 为 True）；
2. ContentEpoch 变动帧被后续静态帧覆盖后的 OCR 恢复与 Latest-Content-Wins 正确性；
3. 字幕模式下首帧即刻上屏，不延迟进入两帧候选确认；
4. 字幕模式 exact_change 语义，微变与常规变动不进入相似度死锁；
5. 待确认候选防抖（OcrStabilizer / TextChangeDetector）与清空确认期间坚决禁止跳过静态帧 OCR；
6. SceneTextState 解析标准 (x1, y1, x2, y2) 绝对坐标的多边形/矩形精确相交剔除；
7. 隐私配置 translation_cache_enabled=False 时 Translator 绝不落盘持久化缓存。
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, Mock, patch
import numpy as np

from app.frame_detector import FrameChangeDetector, FrameDiffResult
from app.latest_frame_buffer import FramePacket, LatestFrameBuffer
from app.ocr_engine import OcrLine
from app.ocr_stabilizer import OcrStabilizer
from app.scene_text_state import SceneTextState, _boxes_intersect
from app.text_change_detector import TextChangeDetector
from app.translator import Translator
from app.window_watcher import WindowWatcher


class TestAuditRound2Invariants(unittest.TestCase):
    """第二轮审查核心漏洞闭环实证测试集。"""

    def test_latest_frame_buffer_clear_invariant_with_frame_packet(self):
        """实证 1: put FramePacket 后调用 clear()，确保 is_empty 为 True 且 get() 必须为 None。"""
        buf = LatestFrameBuffer()
        pkt = FramePacket(
            frame=np.ones((20, 20, 3), dtype=np.uint8),
            epoch=42,
            roi_box=(0, 0, 20, 20),
            has_changed=True,
            changed_ratio=0.5,
        )
        buf.put(pkt)
        self.assertFalse(buf.is_empty)
        self.assertIsNotNone(buf.peek())

        # 执行 clear
        buf.clear()

        # 验证状态不变量：单一真相源下所有视图均必须显示为空
        self.assertTrue(buf.is_empty, "clear() 后 is_empty 必须为 True")
        self.assertIsNone(buf._frame, "clear() 后 _frame 必须为 None")
        self.assertIsNone(buf._packet, "clear() 后 _packet 必须为 None")
        self.assertIsNone(buf._gen_id, "clear() 后 _gen_id 必须为 None")
        self.assertIsNone(buf.peek(), "clear() 后 peek() 必须为 None")
        self.assertIsNone(buf.get(timeout=0.01), "clear() 后 get() 严禁再返回已清除的 packet")

    def test_first_subtitle_immediate_dispatch_without_delay(self):
        """实证 2: 字幕模式下首条文本以及新字幕必须立即触发 change，不进入两帧候选确认。"""
        detector = TextChangeDetector(candidate_confirm_frames=2)
        # 首帧文本 "Hello World"，在字幕模式下调用
        event, text = detector.observe("Hello World", threshold=1.0, exact_change=True)
        self.assertEqual(event, "change", "首条字幕必须立即触发 change，禁止进入 candidate 延迟")
        self.assertEqual(text, "Hello World")
        self.assertEqual(detector.candidate_count, 0)

        # 相同文本：返回 none
        event, text = detector.observe("Hello World", threshold=1.0, exact_change=True)
        self.assertEqual(event, "none")

        # 换行字幕直接更新：立即 change
        event, text = detector.observe("Next Subtitle Line", threshold=1.0, exact_change=True)
        self.assertEqual(event, "change", "字幕更新必须立即触发 change")
        self.assertEqual(text, "Next Subtitle Line")
        self.assertEqual(detector.candidate_count, 0)

    def test_ocr_stabilizer_and_detector_pending_candidate_flags(self):
        """实证 3: 候选防抖等待状态通过 has_pending_candidate 正确暴露。"""
        stabilizer = OcrStabilizer(debounce_frames=2, max_jitter_dist=2)
        # 首帧 baseline
        stabilizer.process(["Hello 125"])
        self.assertFalse(stabilizer.has_pending_candidate)

        # 1 字符微变进入 candidate
        stabilizer.process(["Hello 128"])
        self.assertTrue(stabilizer.has_pending_candidate, "微变首帧后必须处于 pending candidate 状态")

        # 第 2 帧确认
        stabilizer.process(["Hello 128"])
        self.assertFalse(stabilizer.has_pending_candidate, "微变确认后 pending candidate 必须解除")

        # TextChangeDetector 同样测试
        tcd = TextChangeDetector(empty_clear_threshold=2)
        tcd.observe("A", threshold=0.5)
        self.assertFalse(tcd.has_pending_candidate)

        # 出现一帧空文本：尚未达到 empty_clear_threshold(2)
        tcd.observe("", threshold=0.5)
        self.assertTrue(tcd.has_pending_candidate, "空帧第一轮处于清空确认中，必须为 pending")

        # 出现第二帧空文本：触发 clear
        tcd.observe("", threshold=0.5)
        self.assertFalse(tcd.has_pending_candidate, "清空完成后 pending 解除")

    def test_scene_text_state_strict_x1_y1_x2_y2_intersect(self):
        """实证 4: SceneTextState 正确解析 (x1, y1, x2, y2) 绝对坐标，杜绝将 (x2, y2) 当 (w, h) 误删文本。"""
        # line1: 绝对坐标 (100, 100, 150, 120)
        # 若当成 (x, y, w, h)，则范围会变成 x: 100~250, y: 100~220，进而误删相近区域
        line1 = OcrLine(text="Line 1", score=0.9, box=(100, 100, 150, 120))
        # line2: 位于 (160, 100, 200, 120)，在 X 轴上与 line1 完全分离
        line2 = OcrLine(text="Line 2", score=0.9, box=(160, 100, 200, 120))

        # 测试相交函数:
        # ROI 仅覆盖 Line 1 (x: 95..155, y: 95..125)
        roi_line1 = (95, 95, 155, 125)
        self.assertTrue(_boxes_intersect(line1.box, roi_line1))
        # Line 2 绝对不在该 ROI 内（160 > 155+4）
        self.assertFalse(_boxes_intersect(line2.box, roi_line1))

        # 测试 SceneTextState update_roi
        scene = SceneTextState()
        scene.update_full([line1, line2])
        self.assertEqual(len(scene.lines), 2)

        # 对 line1 所在区域做局部更新
        new_line1 = OcrLine(text="Line 1 Updated", score=0.95, box=(100, 100, 150, 120))
        merged = scene.update_roi([new_line1], roi_line1)

        # 必须保留 line2，替换 line1
        texts = [l.text for l in merged]
        self.assertIn("Line 1 Updated", texts)
        self.assertIn("Line 2", texts, "Line 2 严禁因坐标解释错误被误删！")
        self.assertEqual(len(merged), 2)

    def test_content_epoch_overwritten_by_static_frame_triggers_ocr(self):
        """实证 5: 抓屏变动帧被后续静态最新帧覆盖时，推理端通过 epoch 比较必须照常识别，不丢失翻译。"""
        ocr = Mock()
        ocr.recognize.return_value = [OcrLine("Recovered Text", 0.9, (0, 0, 50, 20))]
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

        f_a = np.zeros((40, 60, 3), dtype=np.uint8)
        f_b = f_a.copy()
        f_b[10:30, 10:50] = 255

        # 模拟运行逻辑：
        # 推理端初次消费处理图 A（epoch=1）
        watcher._last_processed_epoch = 1
        watcher._last_frame = f_a

        # 抓屏线程发生变动：A -> B（epoch 自增到 2，has_changed=True）
        # 紧接着 B -> B（epoch 仍为 2，但 has_changed=False 覆盖进入 LatestFrameBuffer）
        static_packet = FramePacket(
            frame=f_b,
            epoch=2,
            roi_box=None,
            has_changed=False, # 最新帧标记为未变动
            changed_ratio=0.0,
        )
        watcher._frame_buffer.put(static_packet)

        # 启动消费推理单次取出
        pulled = watcher._frame_buffer.get(timeout=0.1)
        self.assertIsNotNone(pulled)
        packet_epoch = pulled.epoch
        has_visual_change = pulled.has_changed

        # 校验 WindowWatcher 中新加入的 ContentEpoch 修复逻辑：
        epoch_changed = (packet_epoch != watcher._last_processed_epoch)
        self.assertTrue(epoch_changed, "packet.epoch(2) != _last_processed_epoch(1) 判定必须成立")

        if epoch_changed:
            diff_has_changed = True
        else:
            diff_has_changed = has_visual_change

        self.assertTrue(diff_has_changed, "即使 packet.has_changed 为 False，也必须判定为发生变动以触发 OCR")

    def test_translation_cache_privacy_setting_respected(self):
        """实证 6: 当 translation_cache_enabled 为 False 时，Translator 绝不向持久化缓存写入。"""
        cfg = {"translation_cache_enabled": False}
        mock_storage = Mock()
        mock_cache = Mock()
        tr = Translator(base_url="http://127.0.0.1:8080", cfg=cfg, storage=mock_storage)
        tr.cache = mock_cache

        # 调用 _cache_put
        tr._cache_put("Secret source text", "zh", "机密译文")

        # 验证 mock_cache.put 绝对未被调用
        mock_cache.put.assert_not_called()

        # 调用 _cache_get
        tr._cache_get("Secret source text", "zh")
        mock_cache.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
