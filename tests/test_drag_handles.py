"""三个“⠿”拖动手柄共用基类后的行为回归测试。"""

import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from app.ui.annotation_overlay import _RegionDragHandle
from app.ui.region_frame import _RegionFrameDragHandle
from app.ui.subtitle_bar import _SubtitleDragHandle


def _mouse(kind, x, y, button=Qt.MouseButton.LeftButton):
    held = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
    return QMouseEvent(kind, QPointF(0, 0), QPointF(x, y), button, held, Qt.KeyboardModifier.NoModifier)


def drag(handle, start, end):
    handle.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, *start))
    handle.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, *end))
    handle.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, *end))


class DragHandleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def _frame(self, pinned=False, visible=True):
        frame = Mock(pinned=pinned)
        frame.isVisible.return_value = visible
        frame.content_rect.return_value = (100, 200, 50, 40)
        return frame

    def test_region_frame_handle_moves_frame_and_reports_move_on_release(self):
        frame = self._frame()
        handle = _RegionFrameDragHandle(frame)
        drag(handle, (110, 210), (140, 260))
        frame.move_content_to.assert_called_once_with(130, 250)
        frame._emit_moved.assert_called_once()

    def test_pinned_region_frame_cannot_be_dragged(self):
        frame = self._frame(pinned=True)
        drag(_RegionFrameDragHandle(frame), (110, 210), (140, 260))
        frame.move_content_to.assert_not_called()
        frame._emit_moved.assert_not_called()

    def _owner(self, frame):
        owner = QWidget()  # 生产中为备注控制条控件
        owner._region_frame = frame
        self.addCleanup(owner.deleteLater)
        return owner

    def test_annotation_handle_drags_visible_region_frame(self):
        owner = self._owner(self._frame())
        drag(_RegionDragHandle(owner), (110, 210), (90, 230))
        owner._region_frame.move_content_to.assert_called_once_with(80, 220)

    def test_annotation_handle_ignores_hidden_or_missing_frame(self):
        owner = self._owner(self._frame(visible=False))
        drag(_RegionDragHandle(owner), (110, 210), (90, 230))
        owner._region_frame.move_content_to.assert_not_called()
        owner._region_frame = None
        drag(_RegionDragHandle(owner), (110, 210), (90, 230))

    def test_subtitle_handle_switches_to_free_mode_then_moves_bar(self):
        bar = Mock(mode="follow")
        bar.pos.return_value = QPoint(300, 400)
        bar.set_mode.side_effect = lambda mode, emit=False: setattr(bar, "mode", mode)
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        drag(_SubtitleDragHandle(bar, parent=parent), (310, 405), (360, 455))
        bar.set_mode.assert_called_once_with("free", emit=True)
        bar.move_to.assert_called_once_with(350, 450)

    def test_right_button_does_not_start_drag(self):
        frame = self._frame()
        handle = _RegionFrameDragHandle(frame)
        handle.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, 110, 210, Qt.MouseButton.RightButton))
        handle.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, 140, 260))
        frame.move_content_to.assert_not_called()


if __name__ == "__main__":
    unittest.main()
