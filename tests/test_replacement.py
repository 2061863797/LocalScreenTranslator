import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.ocr_engine import OcrLine
from app.pipelines import OneShotPipeline
from app.replacement import (
    build_payload,
    estimate_text_color,
    fit_font_size,
    group_blocks,
    join_block_text,
)

# 模拟 GitHub 深色页面：背景 #0d1117
_BG_BGR = (0x17, 0x11, 0x0D)


def _page(height=120, width=300):
    return np.full((height, width, 3), _BG_BGR, dtype=np.uint8)


def _draw_text_row(frame, box, color_bgr):
    """用竖条模拟一行文字：墨迹占满行框中间，四周留 3px 背景。"""
    x1, y1, x2, y2 = box
    for x in range(x1 + 3, x2 - 3, 4):
        frame[y1 + 3:y2 - 3, x:x + 2] = color_bgr


class GroupingTests(unittest.TestCase):
    def test_wrapped_description_lines_merge_into_one_paragraph(self):
        title = OcrLine("XiaoDuoYa / codex-with-chatgpt", 0.99, (14, 117, 298, 140))
        line1 = OcrLine("ChatGPT thinks. Use ChatGPT as the planning brain while", 0.99, (14, 151, 430, 169))
        line2 = OcrLine("keeping the Codex harness.", 0.99, (14, 171, 180, 189))
        meta = OcrLine("TypeScript 7,377 688 Updated 2 weeks ago", 0.99, (16, 205, 250, 220))
        blocks = group_blocks([title, line1, line2, meta])
        self.assertEqual([[line.text for line in block] for block in blocks], [
            [title.text], [line1.text, line2.text], [meta.text],
        ])

    def test_different_text_colors_are_not_merged(self):
        # 蓝色标题紧挨着白色描述、高度也接近：只有颜色能分开
        frame = _page(120, 400)
        _draw_text_row(frame, (10, 10, 300, 32), (0xF8, 0x93, 0x44))
        _draw_text_row(frame, (10, 36, 360, 56), (0xD9, 0xD1, 0xC9))
        title = OcrLine("acme / pixel-forge", 0.99, (10, 10, 300, 32))
        desc = OcrLine("A fast image toolkit.", 0.99, (10, 36, 360, 56))
        self.assertEqual(len(group_blocks([title, desc])), 1)  # 不给截图时只看位置
        self.assertEqual(len(group_blocks([title, desc], frame)), 2)
    def test_far_apart_rows_stay_separate(self):
        a = OcrLine("first", 0.99, (10, 10, 100, 28))
        b = OcrLine("second", 0.99, (10, 80, 100, 98))
        self.assertEqual(len(group_blocks([a, b])), 2)

    def test_join_block_text_uses_spaces_only_between_latin_words(self):
        self.assertEqual(join_block_text(["planning brain while", "keeping the"]),
                         "planning brain while keeping the")
        self.assertEqual(join_block_text(["这是第一行", "第二行"]), "这是第一行第二行")
        self.assertEqual(join_block_text(["hyphen-", "ated"]), "hyphenated")


def _rotated_quad(cx, cy, length, thickness, angle):
    """以 (cx, cy) 为中心、按 angle 度（顺时针）旋转的矩形四角。"""
    import math

    t = math.radians(angle)
    c, s = math.cos(t), math.sin(t)
    corners = []
    for dx, dy in ((-length / 2, -thickness / 2), (length / 2, -thickness / 2),
                   (length / 2, thickness / 2), (-length / 2, thickness / 2)):
        corners.append((cx + dx * c - dy * s, cy + dx * s + dy * c))
    return tuple(corners)


def _line_from_quad(text, quad):
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    return OcrLine(text, 0.99, (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), quad)


class OrientationTests(unittest.TestCase):
    def test_tilted_and_vertical_lines_report_reading_direction(self):
        from app.replacement import line_orientation

        tilted = _line_from_quad("Fastest web agent", _rotated_quad(200, 100, 300, 28, 25))
        self.assertAlmostEqual(line_orientation(tilted).angle, 25, delta=1)
        upward = _line_from_quad("Fastest web agent", _rotated_quad(200, 100, 300, 28, -15))
        self.assertAlmostEqual(line_orientation(upward).angle, -15, delta=1)
        column = _line_from_quad("床前明月光", _rotated_quad(300, 110, 167, 31, 90))
        self.assertAlmostEqual(line_orientation(column).angle, 90, delta=1)
        # 近乎水平、或近乎方块的单字，都按水平处理
        flat = _line_from_quad("hello", _rotated_quad(100, 50, 200, 20, 1.5))
        self.assertEqual(line_orientation(flat).angle, 0.0)
        single = _line_from_quad("字", _rotated_quad(100, 50, 30, 28, 45))
        self.assertEqual(line_orientation(single).angle, 0.0)

    def test_vertical_columns_read_right_to_left_as_one_paragraph(self):
        # OCR 按从左到右给出两列；竖排应从右边那列读起
        left = _line_from_quad("疑是地上霜", ((239, 26), (267, 26), (267, 191), (239, 191)))
        right = _line_from_quad("床前明月光", ((298, 25), (329, 25), (329, 192), (298, 192)))
        blocks = group_blocks([left, right])
        self.assertEqual([[line.text for line in block] for block in blocks],
                         [["床前明月光", "疑是地上霜"]])

    def test_tilted_lines_group_in_their_own_direction(self):
        first = _line_from_quad("ChatGPT thinks. Use ChatGPT", _rotated_quad(250, 160, 380, 24, -15))
        second = _line_from_quad("as the planning brain.", _rotated_quad(250 + 26 * 0.2588, 160 + 26 * 0.9659, 380, 24, -15))
        flat = OcrLine("Footer", 0.99, (10, 300, 80, 318))
        blocks = group_blocks([first, flat, second])
        self.assertEqual([[line.text for line in block] for block in blocks],
                         [[first.text, second.text], ["Footer"]])

    def test_rotated_block_covers_tilted_polygon(self):
        frame = np.full((300, 400, 3), 230, dtype=np.uint8)
        quad = _rotated_quad(200, 150, 240, 30, 25)
        line = _line_from_quad("Fastest and cheapest web agent", quad)
        block = build_payload(frame, [([line], "最快的网络代理")]).blocks[0]
        self.assertAlmostEqual(block.angle, 25, delta=1)
        self.assertAlmostEqual(block.length, 240, delta=3)
        self.assertAlmostEqual(block.thickness, 30, delta=3)
        self.assertIsNotNone(block.lines[0].polygon)
        self.assertFalse(block.vertical)
        self.assertEqual(block.lines[0].fill[:3], (230, 230, 230))

class AnalysisTests(unittest.TestCase):
    def test_font_size_follows_ink_height_and_letter_shapes(self):
        box = (0, 0, 200, 24)
        # 含上伸和下伸字母：墨迹高度≈字号
        self.assertEqual(fit_font_size(box, 18, "image-blaster"), 18)
        # 只有 x 高度的小写字母：墨迹约为字号一半
        self.assertEqual(fit_font_size(box, 7, "summer"), 13)
        # 拿不到墨迹时按行框估计，且不超过行框
        self.assertEqual(fit_font_size(box, 0, "text"), 17)
        self.assertLessEqual(fit_font_size(box, 40, "Tag"), 24)

    def test_auto_color_falls_back_to_contrast(self):
        self.assertEqual(estimate_text_color(None, np.full(3, 20, np.uint8)), (245, 245, 245, 255))
        self.assertEqual(estimate_text_color(None, np.full(3, 240, np.uint8)), (24, 24, 24, 255))

    def test_payload_keeps_background_and_original_text_colors(self):
        frame = _page()
        cyan = (0xF7, 0xC3, 0x4F)  # BGR of #4FC3F7
        gray = (0x9E, 0x94, 0x8B)
        _draw_text_row(frame, (10, 10, 200, 32), cyan)
        _draw_text_row(frame, (10, 60, 160, 76), gray)
        title = OcrLine("neilsonnn / image-blaster", 0.99, (10, 10, 200, 32))
        meta = OcrLine("Updated on May 15", 0.99, (10, 60, 160, 76))
        payload = build_payload(frame, [([title], "标题"), ([meta], "更新于5月15日")], frozen=True)

        self.assertEqual(payload.size, (300, 120))
        self.assertIsNotNone(payload.base_image)
        first, second = payload.blocks
        self.assertEqual(first.lines[0].fill, (0x0D, 0x11, 0x17, 255))
        self.assertIsNone(first.lines[0].patch)  # 纯色背景直接铺色，不修补
        self.assertEqual(first.text_color[:3], (0x4F, 0xC3, 0xF7))
        self.assertEqual(second.text_color[:3], (0x8B, 0x94, 0x9E))
        # 遮盖区域比 OCR 框外扩 2px，盖住抗锯齿边缘
        self.assertEqual(first.lines[0].box, (8, 8, 202, 34))

    def test_noisy_background_uses_inpainted_patch(self):
        rng = np.random.default_rng(0)
        frame = rng.integers(0, 255, (80, 200, 3), dtype=np.uint8)
        line = OcrLine("noise", 0.9, (20, 20, 120, 40))
        block = build_payload(frame, [([line], "噪点")]).blocks[0]
        self.assertTrue(block.complex_background)
        self.assertIsNotNone(block.lines[0].patch)
        self.assertEqual(block.lines[0].patch.shape[:2], (24, 104))


class PipelineTests(unittest.TestCase):
    def test_one_shot_replacement_translates_each_block_once(self):
        frame = _page()
        _draw_text_row(frame, (10, 10, 120, 28), (240, 240, 240))
        calls = []

        class FakeOcr:
            def recognize(self, _image):
                return [OcrLine("hello", 0.99, (10, 10, 120, 28))]

        class FakeTranslator:
            def translate(self, source, _target, **_kwargs):
                calls.append(source)
                return "你好"

        result = OneShotPipeline(FakeOcr(), FakeTranslator()).run(
            image=frame, translate=True, target_language="中文", replacement=True,
        )
        self.assertEqual(calls, ["hello"])
        self.assertEqual(result.translation, "你好")
        self.assertEqual([block.text for block in result.replacement.blocks], ["你好"])
        self.assertIsNotNone(result.replacement.base_image)

    def test_result_window_mode_defers_payload_but_keeps_blocks(self):
        class FakeOcr:
            def recognize(self, _image):
                return [OcrLine("hello", 0.99, (10, 10, 120, 28))]

        class FakeTranslator:
            def translate(self, _source, _target, **_kwargs):
                return "你好"

        result = OneShotPipeline(FakeOcr(), FakeTranslator()).run(
            image=_page(), translate=True, target_language="中文",
        )
        self.assertIsNone(result.replacement)
        self.assertEqual([[line.text for line in block] for block in result.blocks], [["hello"]])
        self.assertEqual(result.block_translations, ("你好",))

    def test_text_input_has_no_payload(self):
        class FakeTranslator:
            def translate(self, _source, _target, **_kwargs):
                return "你好"

        result = OneShotPipeline(Mock(), FakeTranslator()).run(
            text="hello", translate=True, target_language="中文",
        )
        self.assertEqual(result.translation, "你好")
        self.assertIsNone(result.replacement)

    def test_ocr_line_keeps_optional_quad_without_breaking_old_constructor(self):
        old = OcrLine("source", 0.9, (2, 3, 20, 14))
        self.assertIsNone(old.quad)
        quad = ((2.0, 3.0), (20.0, 3.0), (20.0, 14.0), (2.0, 14.0))
        self.assertEqual(OcrLine("source", 0.9, (2, 3, 20, 14), quad).quad, quad)


class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_long_translation_shrinks_before_eliding(self):
        from PySide6.QtCore import QRectF
        from app.ui.replacement_overlay import fit_block

        rect = QRectF(0, 0, 120, 40)
        layout, size = fit_block("一二三四五六七八九十一二三四五六七八", rect, 16, 20)
        self.assertLess(size, 16)
        self.assertGreaterEqual(size, 16 * 0.7 - 0.01)
        shown = layout.text()
        self.assertFalse(shown.endswith("…"))

    def test_short_translation_keeps_source_size(self):
        from PySide6.QtCore import QRectF
        from app.ui.replacement_overlay import fit_block

        _, size = fit_block("你好", QRectF(0, 0, 120, 22), 16, 22)
        self.assertEqual(size, 16)

    def test_vertical_translation_fills_columns_right_to_left(self):
        from PySide6.QtCore import QRectF
        from app.ui.replacement_overlay import fit_vertical

        rect = QRectF(240, 25, 90, 167)
        font, glyphs, size = fit_vertical("明亮的月光洒在床前好像地上泛起了一层霜", rect, 26, 62)
        self.assertEqual("".join(ch for ch, _ in glyphs), "明亮的月光洒在床前好像地上泛起了一层霜")
        self.assertGreaterEqual(size, 26 * 0.7 - 0.01)
        first, last = glyphs[0][1], glyphs[-1][1]
        self.assertGreater(first.x(), last.x())  # 第一列在最右边
        self.assertTrue(all(rect.left() - 2 <= p.x() <= rect.right() for _, p in glyphs))
    def test_compare_hides_live_overlay_and_keeps_payload(self):
        from app.ui.replacement_overlay import ReplacementOverlay

        frame = _page()
        _draw_text_row(frame, (10, 10, 120, 28), (240, 240, 240))
        payload = build_payload(frame, [([OcrLine("hello", 0.99, (10, 10, 120, 28))], "你好")])
        overlay = ReplacementOverlay()
        try:
            overlay.update_geometry((0, 0, 300, 120))
            overlay.set_payload(payload)
            self.assertTrue(overlay.isVisible())
            overlay.set_comparing(True)
            self.assertFalse(overlay.isVisible())
            overlay.set_comparing(False)
            self.assertTrue(overlay.isVisible())
            self.assertIs(overlay.payload, payload)
            # 持续翻译没有冻结底图，不能保存图片
            self.assertIsNone(overlay.snapshot())
            # 截屏：保存的图片保持截图原始像素尺寸（高分屏下窗口比截图小）
            frozen = build_payload(frame, [([OcrLine("hello", 0.99, (10, 10, 120, 28))], "你好")], frozen=True)
            overlay.update_geometry((0, 0, 200, 80))
            overlay.set_payload(frozen)
            self.assertEqual((overlay.snapshot().width(), overlay.snapshot().height()), (300, 120))
        finally:
            overlay.close()
            overlay.deleteLater()
            self.app.processEvents()


class ScreenshotSwitchTests(unittest.TestCase):
    """截屏结果窗口 ↔ 原位替换的快捷切换。"""

    def _app(self, mode):
        from app.main import App, ScreenshotJob
        from app.pipelines import OneShotResult

        frame = _page()
        _draw_text_row(frame, (10, 10, 120, 28), (240, 240, 240))
        payload = build_payload(frame, [([OcrLine("hello", 0.99, (10, 10, 120, 28))], "你好")])
        app = App.__new__(App)
        app.cfg = {"screenshot_display_mode": mode}
        app.log = Mock()
        app.hotkeys = Mock()
        app.translate_win = Mock()
        app.translate_win.target_language = "简体中文"
        app.screenshot_replacement = Mock()
        app.screenshot_ctrl = Mock()
        app._translate_screenshot = Mock()
        line = OcrLine("hello", 0.99, (10, 10, 120, 28))
        result = OneShotResult("hello", "你好", (line,), payload, ((line,),), ("你好",))
        app._screenshot_job = ScreenshotJob((5, 6, 300, 120), frame, result, "简体中文")
        return app, payload

    def test_overlay_mode_hides_result_window(self):
        app, payload = self._app("replace")
        app._present_screenshot("replace")
        app.translate_win.hide.assert_called_once_with()
        app.screenshot_replacement.set_payload.assert_called_once_with(payload)
        app.screenshot_ctrl.place_above.assert_called_once_with((5, 6, 300, 120))

    def test_switch_to_window_and_back_is_remembered(self):
        app, payload = self._app("replace")
        with patch("app.main.config.save") as save:
            app._switch_screenshot_display("window")
            self.assertEqual(app.cfg["screenshot_display_mode"], "window")
            app.screenshot_ctrl.hide.assert_called_once_with()
            app.translate_win.show_result.assert_called_once_with(
                "hello", "你好", 5, 6 + 120 + 8, inplace=True,
            )
            app._switch_screenshot_display("replace")
            self.assertEqual(app.cfg["screenshot_display_mode"], "replace")
            app.screenshot_replacement.set_payload.assert_called_with(payload)
            self.assertEqual(save.call_count, 2)

    def test_copy_follows_what_is_shown(self):
        app, _ = self._app("replace")
        clipboard = Mock()
        with patch("app.main.QApplication.clipboard", return_value=clipboard):
            app.screenshot_replacement.comparing = False
            app._copy_screenshot_text()
            clipboard.setText.assert_called_with("你好")
            app.screenshot_replacement.comparing = True
            app._copy_screenshot_text()
            clipboard.setText.assert_called_with("hello")

    def test_payload_is_built_on_first_switch_from_result_window(self):
        from dataclasses import replace

        app, _ = self._app("window")
        job = app._screenshot_job
        app._screenshot_job = replace(job, result=replace(job.result, replacement=None))
        with patch("app.main.config.save"):
            app._switch_screenshot_display("replace")
        payload = app.screenshot_replacement.set_payload.call_args.args[0]
        self.assertEqual([block.text for block in payload.blocks], ["你好"])
        self.assertIsNotNone(payload.base_image)
        self.assertIs(app._screenshot_job.result.replacement, payload)

    def test_stale_screenshot_failure_is_not_shown(self):
        app, _ = self._app("replace")
        app._show_error = Mock()
        app._screenshot_request_id = 3
        app._on_screenshot_failed("旧请求出错", 2)
        app._show_error.assert_not_called()
        app._on_screenshot_failed("当前请求出错", 3)
        app._show_error.assert_called_once_with("当前请求出错")

    def test_dragged_overlay_position_is_remembered(self):
        app, _ = self._app("replace")
        app._on_screenshot_moved(40, 50, 300, 120)
        self.assertEqual(app._screenshot_job.region, (40, 50, 300, 120))

    def test_switch_back_after_language_change_retranslates(self):
        app, _ = self._app("window")
        app.translate_win.target_language = "English"
        with patch("app.main.config.save"):
            app._switch_screenshot_display("replace")
        app._translate_screenshot.assert_called_once()
        self.assertEqual(app._translate_screenshot.call_args.args[2], "English")
        app.screenshot_replacement.set_payload.assert_not_called()


class LiveReplacementTests(unittest.TestCase):
    """持续翻译的原位替换：旧译文及时撤下、位置跟随、背景在动时不重复计算。"""

    def test_stabilizer_keeps_latest_coordinates_for_same_text(self):
        from app.ocr_stabilizer import OcrStabilizer

        stabilizer = OcrStabilizer()
        stabilizer.process([OcrLine("hello", 0.99, (10, 10, 120, 28))])
        changed, lines = stabilizer.process([OcrLine("hello", 0.99, (10, 60, 120, 78))])
        self.assertFalse(changed)
        self.assertEqual(lines[0].box, (10, 60, 120, 78))

    def test_refresh_only_when_text_positions_move(self):
        from app.config import DEFAULTS
        from app.window_watcher import WindowWatcher

        watcher = WindowWatcher(Mock(), Mock(), dict(DEFAULTS), region=(0, 0, 300, 120),
                                profile="region", display_mode="replace")
        self.addCleanup(watcher.deleteLater)
        watcher._cached_translation = lambda _source, _target: "你好"
        dispatch = watcher._result_manager.dispatch_replacement = Mock(return_value=True)
        frame = _page()
        here = [OcrLine("hello", 0.99, (10, 10, 120, 28))]
        moved = [OcrLine("hello", 0.99, (10, 60, 120, 78))]
        watcher._refresh_cached_replacement(here, frame, 1)
        watcher._refresh_cached_replacement(here, frame, 1)  # 只是背景在动：不重算
        self.assertEqual(dispatch.call_count, 1)
        watcher._refresh_cached_replacement(moved, frame, 1)
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(dispatch.call_args.args[0].blocks[0].rect, (10, 60, 120, 78))

    def test_empty_payload_clears_previous_translation(self):
        from app.main import App
        from app.replacement import ReplacementPayload

        app = App.__new__(App)
        app._watch_display_mode = "replace"
        app._watch_rect = None
        app._accept_watch_result = Mock(return_value=True)
        app.replacement = Mock()
        empty = ReplacementPayload((0, 0), (300, 120))
        app._on_watch_replacement_guarded(empty, 1, Mock(), 1)
        app.replacement.set_payload.assert_called_once_with(empty)

if __name__ == "__main__":
    unittest.main()
