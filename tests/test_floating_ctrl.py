"""备注栏与识别框控制条共用 _FloatingCtrlMixin 后的定位与层级行为。"""

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.annotation_overlay import AnnotateCtrl
from app.ui.region_frame import _RegionCtrl


class FloatingCtrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        for target in ("app.ui.topmost.set_overlay_layer", "app.ui.topmost.restack_above_owner",
                       "app.ui.overlay_base.set_window_capture_excluded"):
            patcher = patch(target)
            setattr(self, target.rsplit(".", 1)[1], patcher.start())
            self.addCleanup(patcher.stop)

    def ctrls(self):
        frame = Mock(pinned=False)
        for ctrl in (AnnotateCtrl(), _RegionCtrl(frame)):
            self.addCleanup(ctrl.deleteLater)
            yield ctrl

    def test_place_above_shows_ctrl_at_top_left_of_target(self):
        for ctrl in self.ctrls():
            with self.subTest(ctrl=type(ctrl).__name__):
                ctrl.place_above((100, 300, 400, 200))
                self.assertTrue(ctrl.isVisible())
                self.assertEqual((ctrl.x(), ctrl.y()), (100, 300 - ctrl.height() - 4))

    def test_restack_follows_owner_window_or_falls_back_to_topmost(self):
        for ctrl in self.ctrls():
            with self.subTest(ctrl=type(ctrl).__name__):
                ctrl.place_above((0, 100, 100, 50))
                self.restack_above_owner.reset_mock()
                self.set_overlay_layer.reset_mock()
                ctrl.set_layer_owner(1234)
                self.set_overlay_layer.assert_called_with(ctrl, 1234)
                ctrl.restack_layer()
                self.restack_above_owner.assert_called_with(ctrl, 1234)
                ctrl.set_layer_owner(None)
                ctrl.restack_layer()
                self.set_overlay_layer.assert_called_with(ctrl, None)

    def test_hidden_ctrl_does_not_restack(self):
        for ctrl in self.ctrls():
            with self.subTest(ctrl=type(ctrl).__name__):
                ctrl.set_layer_owner(1234)
                self.restack_above_owner.reset_mock()
                ctrl.restack_layer()
                self.restack_above_owner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
