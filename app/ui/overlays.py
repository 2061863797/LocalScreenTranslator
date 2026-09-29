# -*- coding: utf-8 -*-
"""翻译结果显示组件（统一门面模块与公共接口导出）：

本模块已解耦重构拆分为如下单一职责子模块：
- app.ui.overlay_base: 基础混入类、Win32 辅助函数及几何更新工具
- app.ui.language_popup: 现代化 Fluent 风格语言切换弹窗与胶囊按钮
- app.ui.subtitle_bar: 持续翻译悬浮字幕条（单一 HWND、多级自适应、缩放与穿透）
- app.ui.region_frame: 区域翻译识别框（带外侧拖动与固定控制条）
- app.ui.annotation_overlay: 备注模式浮层与控制条（原文就近标注显示）

为保持对外部调用者与既有测试 100% 向后兼容，本模块对所有核心符号进行统一 re-export。
"""

from __future__ import annotations

from .annotation_overlay import (
    AnnotateCtrl,
    AnnotationOverlay,
    _RegionDragHandle,
)
from .language_popup import (
    LanguagePairButton,
    LanguageSelectPopup,
)
from .overlay_base import (
    _FLAGS_TOP,
    _STATUS_BAR_HEIGHT,
    _allow_capture,
    _CaptureAllowedMixin,
    _DraggableMixin,
    _exclude_from_capture,
    _move_if_changed,
    _set_geo_if_changed,
    _show_once,
    MSG,
)
from .region_frame import (
    RegionWatchFrame,
    _RegionCtrl,
    _RegionFrameDragHandle,
)
from .subtitle_bar import (
    SubtitleBar,
    _SubtitleCtrl,
    _SubtitleDragHandle,
    _SubtitleResizeGrip,
    _SubtitleVScroll,
)
from .topmost import restack_above_owner, set_overlay_layer

__all__ = [
    # Base & Win32 Helpers
    "MSG",
    "_FLAGS_TOP",
    "_STATUS_BAR_HEIGHT",
    "_exclude_from_capture",
    "_allow_capture",
    "_show_once",
    "_set_geo_if_changed",
    "_move_if_changed",
    "_CaptureAllowedMixin",
    "_DraggableMixin",
    "set_overlay_layer",
    "restack_above_owner",
    # Language components
    "LanguageSelectPopup",
    "LanguagePairButton",
    # Subtitle components
    "SubtitleBar",
    "_SubtitleCtrl",
    "_SubtitleDragHandle",
    "_SubtitleVScroll",
    "_SubtitleResizeGrip",
    # Region frame components
    "RegionWatchFrame",
    "_RegionCtrl",
    "_RegionFrameDragHandle",
    # Annotation components
    "AnnotateCtrl",
    "AnnotationOverlay",
    "_RegionDragHandle",
]
