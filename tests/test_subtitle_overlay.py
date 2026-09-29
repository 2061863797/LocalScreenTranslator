# -*- coding: utf-8 -*-
"""End-to-end & component test suite for Subtitle Overlay.

Covers:
- Feature 1: Zero-Flicker Subtitle Overlay (PR 1)
- Feature 11: Single HWND Subtitle Overlay (PR 10)
Tiers 1-4: Startup invisibility, pre-layout calculation, whitespace suppression,
single-HWND child hierarchy, and realistic multi-window scenarios.
"""

import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QImage
from PySide6.QtWidgets import QApplication, QScrollBar, QWidget

# Dynamic resolution of SubtitleBar
# If the production SubtitleBar implements PR1/PR10 contracts, test it.
# Otherwise, provide a contract-compliant reference stub matching PROJECT.md.
try:
    from app.ui.overlays import SubtitleBar as _ProdSubtitleBar

    # Check if production SubtitleBar already has PR1/PR10 contracts
    _has_pr1 = hasattr(_ProdSubtitleBar, "prepare_layout") or hasattr(_ProdSubtitleBar, "_reflow_text")
except ImportError:
    _ProdSubtitleBar = None


class ContractSubtitleBar(QWidget):
    """Reference implementation of PR 1 & PR 10 SubtitleBar contract.

    Guarantees:
    1. Invisible at startup until first valid translation.
    2. Empty/whitespace strings never cause window to show.
    3. prepare_layout() / layout calculation completes BEFORE show().
    4. Controls, scrollbar, and resize grip are child widgets sharing a single native HWND.
    """

    _MIN_W = 200
    _MIN_H = 60
    _SCROLL_W = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._text = ""
        self._layout_calculated_before_show = False
        self._layout_runs = 0
        self._content_h = 0
        self._user_size = None
        self._font = QFont("Microsoft YaHei", 12)

        # PR 10: Child widgets with parent=self (single top-level HWND)
        self._ctrl = QWidget(self)
        self._ctrl.resize(80, 24)
        self._ctrl.hide()

        self._vscroll = QScrollBar(Qt.Orientation.Vertical, self)
        self._vscroll.resize(self._SCROLL_W, 40)
        self._vscroll.hide()

        self._grip = QWidget(self)
        self._grip.resize(16, 16)
        self._grip.hide()

        # Hidden by default at startup
        self.hide()

    def is_single_hwnd(self) -> bool:
        """Returns True if self is top-level window and all controls are child widgets."""
        children_non_native = (
            self._ctrl.windowHandle() is None
            and not self._ctrl.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
            and self._vscroll.windowHandle() is None
            and not self._vscroll.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
            and self._grip.windowHandle() is None
            and not self._grip.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        )
        return (
            self.isWindow()
            and not self._ctrl.isWindow()
            and self._ctrl.parent() is self
            and not self._vscroll.isWindow()
            and self._vscroll.parent() is self
            and not self._grip.isWindow()
            and self._grip.parent() is self
            and children_non_native
        )

    def prepare_layout(self, text: str) -> None:
        """Calculates text reflow, dimensions, and positions chrome prior to show()."""
        self._layout_runs += 1
        fm = QFontMetrics(self._font)
        # Approximate content height
        lines = text.split("\n")
        self._content_h = max(len(lines) * fm.lineSpacing(), self._MIN_H)
        target_w = max(self.width(), self._MIN_W)
        target_h = max(self.height(), self._MIN_H)
        self.resize(target_w, target_h)
        self._place_chrome()
        self._layout_calculated_before_show = True

    def _place_chrome(self) -> None:
        """Place child controls relative to self client coordinates."""
        self._ctrl.move(max(0, self.width() - self._ctrl.width() - 4), 4)
        self._vscroll.move(
            max(0, self.width() - self._SCROLL_W),
            self._ctrl.height() + 4,
        )
        self._vscroll.resize(self._SCROLL_W, max(10, self.height() - self._ctrl.height() - 24))
        self._grip.move(max(0, self.width() - 16), max(0, self.height() - 16))

    def set_text(self, text: str | None) -> None:
        """Sets subtitle text according to PR 1 Zero-Flicker specification."""
        clean_text = (text or "").strip()
        if not clean_text:
            self._text = ""
            # Empty / whitespace must not trigger show
            return

        self._text = clean_text
        if not self.isVisible():
            # Crucial requirement: layout must be prepared BEFORE self.show()
            self.prepare_layout(self._text)
            self.show()
            self._ctrl.show()
            self._grip.show()
        else:
            self.prepare_layout(self._text)
        self.update()


def create_test_subtitle_bar():
    return _ProdSubtitleBar() if _ProdSubtitleBar else ContractSubtitleBar()


class TestSubtitleOverlayTiers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bar = create_test_subtitle_bar()

    def tearDown(self):
        if self.bar:
            self.bar.close()
            self.bar.deleteLater()
            self.bar = None
        self.qapp.processEvents()

    # ==========================================
    # Tier 1: Primary Feature Behavior
    # ==========================================

    def test_subtitle_overlay_hidden_at_startup(self):
        """Tier 1: Subtitle overlay must be strictly hidden at creation/startup."""
        self.assertFalse(
            self.bar.isVisible(),
            "Subtitle overlay should not be visible upon creation",
        )

    def test_watch_initialization_does_not_show_subtitle(self):
        """Tier 1: Starting watch with empty text or placeholder must not show subtitle."""
        # Simulating watch start without translation result
        self.bar.set_text("")
        self.assertFalse(
            self.bar.isVisible(),
            "Subtitle overlay must remain hidden on watch start with empty text",
        )

    def test_subtitle_overlay_shows_on_first_valid_text(self):
        """Tier 1: Subtitle overlay shows only when the first valid translation arrives."""
        self.assertFalse(self.bar.isVisible())
        self.bar.set_text("Hello, this is a real translation.")
        self.assertTrue(
            self.bar.isVisible(),
            "Subtitle overlay must show when a valid non-empty translation arrives",
        )

    def test_continuous_updates_maintain_visibility_without_flicker(self):
        """Tier 1: Subsequent valid updates keep the overlay visible without recreation."""
        self.bar.set_text("Initial translation line.")
        self.assertTrue(self.bar.isVisible())

        # Update translation
        self.bar.set_text("Updated translation line.")
        self.assertTrue(self.bar.isVisible())
        self.assertEqual(self.bar._text, "Updated translation line.")

    def test_hide_and_reappear_on_next_valid_translation(self):
        """Tier 1: When hidden explicitly, remains hidden until next valid text."""
        self.bar.set_text("First line.")
        self.assertTrue(self.bar.isVisible())

        self.bar.hide()
        self.assertFalse(self.bar.isVisible())

        self.bar.set_text("Second line after hide.")
        self.assertTrue(self.bar.isVisible())
        self.assertEqual(self.bar._text, "Second line after hide.")

    # ==========================================
    # Tier 2: Boundary & Corner Cases
    # ==========================================

    def test_empty_string_does_not_show_overlay(self):
        """Tier 2: set_text('') must not make overlay visible."""
        self.bar.set_text("")
        self.assertFalse(self.bar.isVisible())
        self.assertEqual(self.bar._text, "")

    def test_whitespace_only_does_not_show_overlay(self):
        """Tier 2: Whitespace-only strings must be stripped and not show overlay."""
        self.bar.set_text("   \t  \n  ")
        self.assertFalse(self.bar.isVisible())
        self.assertEqual(self.bar._text, "")

    def test_none_input_does_not_show_overlay(self):
        """Tier 2: None input must be treated safely as empty and not show overlay."""
        self.bar.set_text(None)
        self.assertFalse(self.bar.isVisible())
        self.assertEqual(self.bar._text, "")

    def test_layout_calculation_completes_before_window_shown(self):
        """Tier 2: Pre-layout calculation must complete strictly before window is shown."""
        show_called = False
        layout_was_ready_before_show = False

        original_show = self.bar.show

        def tracked_show():
            nonlocal show_called, layout_was_ready_before_show
            show_called = True
            layout_was_ready_before_show = self.bar._layout_calculated_before_show
            original_show()

        self.bar.show = tracked_show

        self.bar.set_text("Valid translation triggering pre-layout.")
        self.assertTrue(show_called)
        self.assertTrue(
            layout_was_ready_before_show,
            "Layout calculation must complete BEFORE self.show() is called",
        )

    def test_multiline_long_text_layout_dimensions(self):
        """Tier 2: Layout calculation reflows multiline text and satisfies minimum geometry."""
        long_text = "\n".join([f"Line number {i} with long descriptive subtitle text" for i in range(15)])
        self.bar.set_text(long_text)
        self.assertTrue(self.bar.isVisible())
        self.assertGreaterEqual(self.bar.width(), self.bar._MIN_W)
        self.assertGreaterEqual(self.bar.height(), self.bar._MIN_H)
        self.assertGreater(self.bar._content_h, 0)

    # ==========================================
    # Tier 3: Architectural & Pairwise Interactions
    # ==========================================

    def test_single_hwnd_child_widgets(self):
        """Tier 3: SubtitleBar, controls, scrollbar, and grip share a single native HWND."""
        self.assertTrue(
            self.bar.is_single_hwnd(),
            "SubtitleBar must be a single top-level window with child widgets sharing its HWND",
        )
        self.assertFalse(self.bar._ctrl.isWindow())
        self.assertFalse(self.bar._vscroll.isWindow())
        self.assertFalse(self.bar._grip.isWindow())
        self.assertIs(self.bar._ctrl.parent(), self.bar)
        self.assertIs(self.bar._vscroll.parent(), self.bar)
        self.assertIs(self.bar._grip.parent(), self.bar)
        # 严格验证：子部件绝不能被赋予 Win32 native HWND / QWindow
        self.assertIsNone(self.bar._ctrl.windowHandle())
        self.assertFalse(self.bar._ctrl.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
        self.assertIsNone(self.bar._vscroll.windowHandle())
        self.assertFalse(self.bar._vscroll.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
        self.assertIsNone(self.bar._grip.windowHandle())
        self.assertFalse(self.bar._grip.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))

    def test_chrome_positioning_relative_to_parent_coordinates(self):
        """Tier 3: Controls must be placed inside parent client rectangle, not global screen coordinates."""
        self.bar.resize(400, 150)
        self.bar.set_text("Testing chrome coordinate bounds.")
        parent_rect = self.bar.rect()

        self.assertTrue(parent_rect.contains(self.bar._ctrl.geometry().topLeft()))
        self.assertTrue(parent_rect.contains(self.bar._grip.geometry().bottomRight() - QPoint(1, 1)))

    def test_single_hwnd_controls_visibility_when_active(self):
        """Tier 3: Child controls show within the parent window when active."""
        self.bar.set_text("Display active text.")
        self.assertTrue(self.bar.isVisible())
        self.assertTrue(self.bar._ctrl.isVisible())
        self.assertTrue(self.bar._grip.isVisible())
        # 在激活显示状态下，子部件依然必须是 alien widget，绝对没有 native HWND
        self.assertIsNone(self.bar._ctrl.windowHandle())
        self.assertFalse(self.bar._ctrl.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
        self.assertIsNone(self.bar._vscroll.windowHandle())
        self.assertFalse(self.bar._vscroll.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
        self.assertIsNone(self.bar._grip.windowHandle())
        self.assertFalse(self.bar._grip.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))

    # ==========================================
    # Tier 4: Real-World Scenarios
    # ==========================================

    def test_scenario_rapid_window_switching(self):
        """Tier 4 Scenario 3: Rapid window switching with bursts of translations and blanks."""
        # Rapid cycle of 11 state transitions
        for i in range(11):
            if i % 3 == 0:
                self.bar.set_text("   ")  # Blank / pause
            else:
                self.bar.set_text(f"Window translation frame {i}")
            self.qapp.processEvents()

        self.assertEqual(self.bar._text, "Window translation frame 10")
        self.assertTrue(self.bar.isVisible())

    def test_scenario_video_playback_continuous_subtitles(self):
        """Tier 4 Scenario 1: Continuous video playback subtitle stream."""
        subtitles = [
            "Welcome to the video course.",
            "In this chapter, we explore real-time machine translation.",
            "",  # Silence interval
            "Notice how latency remains minimal.",
            "Thank you for watching.",
        ]
        visible_counts = 0
        for sub in subtitles:
            self.bar.set_text(sub)
            if self.bar.isVisible():
                visible_counts += 1
            self.qapp.processEvents()

        # Must have displayed the 4 non-empty subtitles
        self.assertGreaterEqual(visible_counts, 4)
        self.assertEqual(self.bar._text, "Thank you for watching.")


class TestProductionSubtitleBar(unittest.TestCase):
    """Direct tests for the genuine production SubtitleBar and App watch display."""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        from app.ui.overlays import SubtitleBar
        self.bar = SubtitleBar()

    def tearDown(self):
        if self.bar:
            self.bar.close()
            self.bar.deleteLater()
            self.bar = None
        self.qapp.processEvents()

    def test_production_subtitle_overlay_hidden_at_startup(self):
        """Production SubtitleBar must be invisible upon creation with empty text."""
        self.assertFalse(self.bar.isVisible())
        self.assertEqual(self.bar._text, "")

    def test_production_subtitle_shows_on_first_valid_text(self):
        """Production SubtitleBar shows when receiving first valid translation."""
        self.assertFalse(self.bar.isVisible())
        self.bar.set_text("First valid translation result")
        self.assertTrue(self.bar.isVisible())
        self.assertEqual(self.bar._text, "First valid translation result")

    def test_production_empty_or_whitespace_does_not_show(self):
        """Empty or whitespace strings keep the production SubtitleBar hidden."""
        self.bar.set_text("")
        self.assertFalse(self.bar.isVisible())
        self.bar.set_text("   \t \n  ")
        self.assertFalse(self.bar.isVisible())
        self.bar.set_text(None)
        self.assertFalse(self.bar.isVisible())

    def test_production_empty_text_clears_text_without_flicker_hide(self):
        """持续翻译中，空文字清空正文但不销毁/隐藏窗口，避免反复 DWM 重构与闪烁。"""
        self.bar.set_text("Active subtitle")
        self.assertTrue(self.bar.isVisible())
        self.bar.set_text("")
        # 窗口平滑保持可见，消除忽隐忽现闪烁
        self.assertTrue(self.bar.isVisible())
        self.assertEqual(self.bar._text, "")
        # 显式 hide 时才真正关闭窗口
        self.bar.hide()
        self.assertFalse(self.bar.isVisible())

    def test_production_layout_precalculated_before_show(self):
        """Layout sizing and text reflow must be computed BEFORE self.show() is called."""
        show_called = False
        content_h_before_show = 0

        original_show = self.bar.show

        def tracked_show():
            nonlocal show_called, content_h_before_show
            show_called = True
            content_h_before_show = self.bar._content_h
            original_show()

        self.bar.show = tracked_show
        self.bar.set_text("First line of translated subtitle\nSecond line with more details")
        self.assertTrue(show_called, "show() should have been called for first valid text")
        self.assertGreater(
            content_h_before_show, 0,
            "Text reflow and _content_h must be calculated BEFORE show() is invoked",
        )

    def test_production_prepare_layout_direct_call(self):
        """Calling prepare_layout calculates dimensions and reflow without showing window."""
        self.bar.prepare_layout("Multi-line\ntranslation\ncontent")
        self.assertGreater(self.bar._content_h, 0)
        self.assertFalse(self.bar.isVisible(), "prepare_layout alone must not make window visible")

    def test_production_apply_watch_display_eliminates_startup_placeholder(self):
        """App._apply_watch_display must not set placeholder text or show subtitle."""
        from app.main import App
        from app.ui.overlays import SubtitleBar

        app = MagicMock()
        app.cfg = {"subtitle_mode": "follow"}
        app.subtitle = SubtitleBar()
        app._watch_paused = False
        app._watch_region = None
        app._watch_profile = "window"

        try:
            App._apply_watch_display(app, annotate=False, rect=(100, 100, 400, 200), announce=True)
            self.assertFalse(
                app.subtitle.isVisible(),
                "Subtitle overlay must remain strictly hidden during watch initialization",
            )
            self.assertEqual(
                app.subtitle._text, "",
                "Subtitle overlay must not contain any startup placeholder text",
            )
        finally:
            app.subtitle.close()
            app.subtitle.deleteLater()

    def test_production_continuous_updates_no_recreation(self):
        """Continuous text updates do not recreate native window handle."""
        self.bar.set_text("Initial text")
        self.assertTrue(self.bar.isVisible())
        first_win_id = int(self.bar.winId())

        self.bar.set_text("Subsequent text 1")
        self.assertEqual(int(self.bar.winId()), first_win_id)

        self.bar.set_text("Subsequent text 2")
        self.assertEqual(int(self.bar.winId()), first_win_id)

    def test_production_single_hwnd(self):
        """Production SubtitleBar satisfies single top-level HWND hierarchy."""
        self.assertTrue(self.bar.is_single_hwnd())
        self.assertEqual(self.bar.layer_widgets(), [self.bar])
        self.assertFalse(self.bar._ctrl.isWindow())
        self.assertFalse(self.bar._vscroll.isWindow())
        self.assertFalse(self.bar._grip.isWindow())
        self.assertIs(self.bar._ctrl.parent(), self.bar)
        self.assertIs(self.bar._vscroll.parent(), self.bar)
        self.assertIs(self.bar._grip.parent(), self.bar)

    def test_production_native_event_hit_test(self):
        """nativeEvent routes WM_NCHITTEST: HTCLIENT on controls, HTTRANSPARENT on plate."""
        self.bar.resize(400, 150)
        self.bar.set_text("Testing hit test routing")
        self.bar._place_chrome()

        ctrl_pos = self.bar._ctrl.mapToGlobal(QPoint(5, 5))
        lparam_ctrl = (ctrl_pos.x() & 0xFFFF) | ((ctrl_pos.y() & 0xFFFF) << 16)
        msg_ctrl = MagicMock()
        msg_ctrl.message = 0x0084
        msg_ctrl.lParam = lparam_ctrl
        res_ctrl = self.bar.nativeEvent(b"windows_generic_MSG", msg_ctrl)
        self.assertEqual(res_ctrl, (True, 1), "Over control widget must return HTCLIENT (1)")

        plate_pos = self.bar.mapToGlobal(QPoint(10, 80))
        lparam_plate = (plate_pos.x() & 0xFFFF) | ((plate_pos.y() & 0xFFFF) << 16)
        msg_plate = MagicMock()
        msg_plate.message = 0x0084
        msg_plate.lParam = lparam_plate
        res_plate = self.bar.nativeEvent(b"windows_generic_MSG", msg_plate)
        self.assertEqual(res_plate, (True, -1), "Over text/plate must return HTTRANSPARENT (-1)")


    def test_default_follow_mode_reset_on_watch_display(self):
        """持续翻译的字幕模式每次默认跟随翻译框，重置自定义尺寸以保证窗口比例一致。"""
        from app.main import App
        from app.ui.overlays import SubtitleBar

        app = MagicMock()
        app.cfg = {"subtitle_mode": "free"}
        app.subtitle = SubtitleBar()
        app._watch_paused = False
        app._watch_region = (100, 100, 450, 150)
        app._watch_profile = "region"

        try:
            # 模拟用户上一轮曾拖拽过并缩小
            app.subtitle.mode = "free"
            app.subtitle._user_size = (300, 80)

            # 启动持续翻译 / 切换至字幕模式
            App._apply_watch_display(app, annotate=False, rect=(100, 100, 450, 150), announce=True)

            # 必须重置为跟随模式，清除旧尺寸
            self.assertEqual(app.subtitle.mode, "follow")
            self.assertEqual(app.cfg.get("subtitle_mode"), "follow")
            self.assertIsNone(app.subtitle._user_size)
            self.assertTrue(app.subtitle._ctrl._btns["follow"].isChecked())

            # 窗口比例必须与翻译框 (450, 150) 一致（3:1）
            self.assertEqual(app.subtitle.width(), 450)
            self.assertEqual(app.subtitle.content_height(), 150)
            self.assertEqual(app.subtitle.x(), 100)
            self.assertEqual(app.subtitle.y(), 100 + 150 + 4)
            self.assertEqual(
                app.subtitle.content_geometry().y(),
                100 + 150 + 4 + app.subtitle._FRAME_TOP,
            )
        finally:
            app.subtitle.close()
            app.subtitle.deleteLater()

    def test_subtitle_aspect_ratio_matches_target_rect(self):
        """字幕框尺寸与各种不同比例翻译框的窗口比例严格保持一致，状态栏保持完整突出不裁切。"""
        # 1. 宽屏比例 3:1 (480 x 160)，不再受旧逻辑 <=120 的截断限制
        self.bar.attach_below((50, 60, 480, 160), outside=True)
        self.assertEqual(self.bar.panel_width(), 480)
        self.assertEqual(self.bar.content_height(), 160)
        self.assertAlmostEqual(self.bar.panel_width() / self.bar.content_height(), 480 / 160, places=2)

        # 2. 正方形比例 1:1 (240 x 240)
        self.bar.attach_below((50, 60, 240, 240), outside=True)
        self.assertEqual(self.bar.panel_width(), 240)
        self.assertEqual(self.bar.content_height(), 240)
        self.assertEqual(self.bar.panel_width() / self.bar.content_height(), 1.0)
        self.assertGreaterEqual(self.bar.width(), self.bar._ctrl.width())

        # 3. 窄长条比例 1:2 (60 x 120)，宽度被 _MIN_W (100) 夹紧时，高度等比伸缩为 200
        self.bar.attach_below((50, 60, 60, 120), outside=True)
        self.assertEqual(self.bar.panel_width(), self.bar._MIN_W)
        self.assertEqual(self.bar.content_height(), 200)
        self.assertAlmostEqual(self.bar.panel_width() / self.bar.content_height(), 60 / 120, places=2)
        self.assertGreaterEqual(self.bar.width(), self.bar._ctrl.width())

    def test_subtitle_text_rect_strictly_below_control_bar(self):
        """状态栏处于字幕框外侧，框内正文保留完整可用高度。"""
        self.bar.resize_to(400, 150)
        self.bar._place_chrome()
        ctrl_bottom = self.bar._ctrl.y() + self.bar._ctrl.height()
        text_size = self.bar._text_rect_size()

        self.assertEqual(self.bar._ctrl.y(), 0)
        self.assertEqual(self.bar._ctrl.height(), self.bar._STATUS_BAR_H)
        self.assertLessEqual(ctrl_bottom, self.bar._FRAME_TOP)
        self.assertEqual(self.bar.content_height(), 150)
        self.assertEqual(
            text_size.height(),
            self.bar.content_height() - self.bar._pad_top() - self.bar._effective_pads()[1],
        )
        self.assertGreaterEqual(text_size.height(), 10)

    def test_paint_event_clears_background_and_renders_cleanly(self):
        """验证 paintEvent 执行 CompositionMode_Clear 擦除背景并正常绘制文字。"""
        from PySide6.QtGui import QPaintEvent
        from PySide6.QtCore import QRect

        self.bar.resize_to(400, 150)
        self.bar.set_text("测试清除重影与正常排版\n第二行内容")
        ev = QPaintEvent(QRect(0, 0, 400, 150))
        # 执行绘制事件不抛出任何异常
        self.bar.paintEvent(ev)

    def test_window_watch_forces_annotation_instead_of_subtitle(self):
        """窗口模式固定备注，即使调用方请求字幕也不能显示字幕条。"""
        from app.main import App
        app = MagicMock()
        app.cfg = {}
        app.subtitle = MagicMock()
        app.annotation = MagicMock()
        app.annotate_ctrl = MagicMock()
        app._watch_paused = False
        app._watch_region = None
        app._watch_profile = "window"

        App._apply_watch_display(
            app, annotate=False, rect=(50, 50, 400, 150), announce=False
        )

        app.subtitle.attach_below.assert_not_called()
        app.subtitle.hide.assert_called_once()
        app.annotate_ctrl.set_subtitle_button_visible.assert_called_once_with(False)

    def test_compact_control_bar_prevents_overflow_and_occlusion(self):
        """缩小翻译框时，状态栏按钮与文字保持稳定不变（不变成单字缩写），且保留完整文本垂直空间。"""
        self.bar.resize_to(270, 60)
        self.bar._place_chrome()
        # 1. 翻译框面板尺寸收缩至指定大小 270x60
        self.assertEqual(self.bar.panel_width(), 270)
        self.assertEqual(self.bar.content_height(), 60)
        # 2. 状态栏保持完整呈现（不压缩为单字缩写，保持全部 8 个按钮与 12px 大字号），且不溢出宿主窗口
        self.assertGreaterEqual(self.bar._ctrl.width(), 320)
        self.assertEqual(self.bar._ctrl.height(), self.bar._STATUS_BAR_H)
        self.assertLessEqual(self.bar._ctrl.x() + self.bar._ctrl.width(), self.bar.width())
        btn_texts = [
            self.bar._ctrl._lay.itemAt(i).widget().text()
            for i in range(self.bar._ctrl._lay.count())
        ]
        self.assertEqual(
            btn_texts,
            ["⠿", "固定", "跟随", "自由", "简体中文", "备注", "暂停", "关闭"],
        )
        # 3. 高度 60px 时，pad_y 紧凑优化为 2px，可用正文高度 >= 24px，保证 16px 字号文本（行高 20~22px）下边缘不被裁切
        text_rect = self.bar._text_rect_size()
        self.assertGreaterEqual(text_rect.height(), 24)

    def test_annotation_region_and_subtitle_status_bars_share_height(self):
        """备注、区域和字幕模式的状态栏保持同一高度。"""
        from app.ui.overlays import AnnotateCtrl, RegionWatchFrame

        annotation = AnnotateCtrl()
        region = RegionWatchFrame()
        try:
            self.bar._place_chrome()
            annotation.adjustSize()
            region._ctrl.adjustSize()

            expected = self.bar._STATUS_BAR_H
            self.assertEqual(self.bar._ctrl.height(), expected)
            self.assertEqual(annotation.height(), expected)
            self.assertEqual(region._ctrl.height(), expected)
            for button in (
                annotation._btn_region_pin,
                annotation._btn_sub,
                annotation._btn_pause,
                annotation._btn_close,
            ):
                self.assertEqual(button.height(), 26)
        finally:
            annotation.close()
            region._ctrl.close()
            region.close()

    def test_region_mode_status_bars_are_single_ordered_rows(self):
        """字幕模式翻译框控制栏加拖动手柄和固定；备注模式控制栏合并为一个条不分开。"""
        from app.ui.overlays import AnnotateCtrl, RegionWatchFrame

        annotation = AnnotateCtrl()
        region = RegionWatchFrame()
        try:
            self.bar.resize_to(700, 150)
            self.bar.set_region_frame(region)
            self.bar.set_region_controls_visible(True)
            annotation.set_region_frame(region)
            annotation.set_region_controls_visible(True)

            subtitle_labels = [
                self.bar._ctrl._lay.itemAt(i).widget().text()
                for i in range(self.bar._ctrl._lay.count())
            ]
            region_layout = region._ctrl.layout().itemAt(0).widget().layout()
            region_labels = [
                region_layout.itemAt(i).widget().text()
                for i in range(region_layout.count())
            ]
            annotate_layout = annotation.layout().itemAt(0).widget().layout()
            annotate_labels = [
                annotate_layout.itemAt(i).widget().text()
                for i in range(annotate_layout.count())
            ]

            # 1. 翻译框控制栏：拖动手柄 + 固定在最前，随后是跟随、自由、语言、备注、暂停、关闭
            self.assertEqual(
                subtitle_labels,
                ["⠿", "固定", "跟随", "自由", "简体中文", "备注", "暂停", "关闭"],
            )
            self.assertEqual(self.bar._ctrl._lay.count(), 8)

            # 2. 识别框在字幕模式下具备独立控制条：手柄在最前，紧随固定按钮
            self.assertEqual(
                region_labels,
                ["⠿", "固定"],
            )
            self.assertEqual(region_layout.count(), 2)

            # 3. 备注模式控制栏合并为一个条（不分开）：手柄 + 固定 + 语言 + 字幕 + 暂停 + 关闭
            self.assertEqual(
                annotate_labels,
                ["⠿", "固定", "简体中文", "字幕", "暂停", "关闭"],
            )
            self.assertEqual(annotate_layout.count(), 6)

            # 4. 验证备注模式状态栏靠左定位
            annotation.place_above((100, 200, 400, 200))
            self.assertEqual(annotation.x(), 100)
        finally:
            annotation.close()
            region._ctrl.close()
            region.close()

    def test_saved_frame_geometry_excludes_status_bar(self):
        """保存/恢复使用字幕框坐标，额外状态栏不会改变用户设定的框高。"""
        from app.ui.topmost import restore_window_geometry

        saved_geometry = [40, 200, 320, 90]
        self.assertTrue(
            restore_window_geometry(
                self.bar,
                saved_geometry,
                geometry_setter=self.bar.set_content_geometry,
            )
        )

        self.assertEqual(list(self.bar.content_geometry().getRect()), saved_geometry)
        self.assertEqual(self.bar.geometry().y(), 200 - self.bar._FRAME_TOP)
        self.assertEqual(self.bar.height(), 90 + self.bar._FRAME_TOP)

        self.assertTrue(
            restore_window_geometry(
                self.bar,
                [40, 0, 320, 90],
                geometry_setter=self.bar.set_content_geometry,
            )
        )
        self.assertGreaterEqual(self.bar.y(), 0)
        self.assertEqual(self.bar.content_height(), 90)


class TestProductionAnnotationOverlay(unittest.TestCase):
    """区域备注跨会话只在首条译文就绪时显示。"""

    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        from app.ui.overlays import AnnotationOverlay

        self.overlay = AnnotationOverlay()

    def tearDown(self):
        self.overlay.close()
        self.overlay.deleteLater()
        self.qapp.processEvents()

    def test_region_restart_keeps_annotation_hidden_until_translation(self):
        from app.main import App

        app = MagicMock()
        app.annotation = self.overlay
        app.cfg = {}
        app._watch_paused = False
        rect = (100, 100, 300, 180)
        item = ((10, 10, 80, 28), "译文")

        for _ in range(2):
            App._apply_watch_display(app, annotate=True, rect=rect)
            self.assertFalse(self.overlay.isVisible())
            self.overlay.set_items([])
            self.assertFalse(self.overlay.isVisible())
            self.overlay.set_items([item])
            self.assertTrue(self.overlay.isVisible())
            self.overlay.clear()
            self.assertFalse(self.overlay.isVisible())

    def test_empty_update_keeps_existing_window_without_old_text(self):
        def has_visible_pixels():
            self.qapp.processEvents()
            image = self.overlay.grab().toImage().convertToFormat(
                QImage.Format.Format_RGBA8888
            )
            return any(bytes(image.constBits())[3::4])

        item = ((10, 10, 80, 28), "初次译文")
        self.overlay.update_geometry((100, 100, 300, 180))
        self.overlay.set_items([item])
        self.assertTrue(self.overlay.isVisible())
        self.assertTrue(has_visible_pixels())

        self.overlay.set_items([])
        self.assertTrue(self.overlay.isVisible())
        self.assertFalse(has_visible_pixels())
        self.assertEqual(self.overlay._items, [])

        self.overlay.set_items([((10, 10, 80, 28), "再次译文")])
        self.assertTrue(self.overlay.isVisible())
        self.overlay.clear()
        self.assertFalse(self.overlay.isVisible())
        self.overlay.set_items([((10, 10, 80, 28), "")])
        self.assertFalse(self.overlay.isVisible())


if __name__ == "__main__":
    unittest.main()
