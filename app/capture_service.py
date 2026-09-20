# -*- coding: utf-8 -*-
"""屏幕与窗口捕获服务 (PR 9)。

封装 Win32 / MSS 窗口与区域捕获，处理窗口句柄验证、窗口位移跟踪与 DPI 归一化。
"""

from __future__ import annotations

import ctypes
import threading
from typing import Any, Callable, Optional, Tuple
import numpy as np

from . import capture
from .applog import get_logger

_log = get_logger("capture_service")


def is_invalid_window_handle_error(exc: BaseException) -> bool:
    """目标窗在检查与截图之间关闭时，pywin32 返回错误 1400 (ERROR_INVALID_WINDOW_HANDLE)。"""
    code = getattr(exc, "winerror", None)
    if code is None and hasattr(exc, "args") and exc.args:
        code = exc.args[0]
    return code == 1400


class CaptureService:
    """屏幕与窗口捕获协调服务。

    职责：
    1. 封装 grab_window 与 grab_region 捕获逻辑；
    2. 目标窗口有效性验证与异常检测；
    3. 目标区域/窗口位移检测 (has_moved)；
    4. 混合 DPI 下的坐标与尺寸归一化；
    5. 捕获资源释放 (release_mss)。
    """

    def __init__(
        self,
        hwnd: int | None = None,
        region: tuple[int, int, int, int] | None = None,
        *,
        backend: capture.CaptureBackend | None = None,
        grab_window_fn: Callable[[int], np.ndarray | None] | None = None,
        grab_region_fn: Callable[[int, int, int, int], np.ndarray] | None = None,
        get_window_rect_fn: Callable[[int], tuple[int, int, int, int]] | None = None,
    ) -> None:
        self._hwnd = hwnd
        self._region = region
        self._target_lock = threading.Lock()
        self._last_rect: tuple[int, int, int, int] | None = None

        self._backend = backend or capture.MssCaptureBackend()
        self._grab_window_fn = grab_window_fn or (
            self._backend.grab_window if backend is not None else capture.grab_window
        )
        self._grab_region_fn = grab_region_fn or self._backend.grab_region
        self._get_window_rect_fn = get_window_rect_fn or capture.get_window_rect

    @property
    def backend(self) -> capture.CaptureBackend:
        return self._backend

    @property
    def hwnd(self) -> int | None:
        with self._target_lock:
            return self._hwnd

    @property
    def region(self) -> tuple[int, int, int, int] | None:
        with self._target_lock:
            return self._region

    @property
    def last_rect(self) -> tuple[int, int, int, int] | None:
        with self._target_lock:
            return self._last_rect

    def set_target(
        self,
        hwnd: int | None = None,
        region: tuple[int, int, int, int] | None = None,
    ) -> None:
        """更新捕获目标（窗口或区域）。"""
        with self._target_lock:
            self._hwnd = hwnd
            self._region = region
            self._last_rect = None

    def set_region(self, region: tuple[int, int, int, int] | None) -> None:
        """更新区域目标。"""
        with self._target_lock:
            self._region = region
            self._last_rect = None

    def set_hwnd(self, hwnd: int | None) -> None:
        """更新窗口句柄目标。"""
        with self._target_lock:
            self._hwnd = hwnd
            self._last_rect = None

    def reset_moved_tracking(self) -> None:
        """重置位移检测基准。"""
        with self._target_lock:
            self._last_rect = None

    def has_moved(self, current_rect: tuple[int, int, int, int] | None) -> bool:
        """检查目标位置是否发生位移或初次确定位置。

        Args:
            current_rect: 当前帧的目标矩形 (x, y, w, h)。

        Returns:
            若矩形发生改变则更新内部基准并返回 True；否则返回 False。
        """
        if current_rect is None:
            return False
        with self._target_lock:
            if current_rect != self._last_rect:
                self._last_rect = current_rect
                return True
            return False

    @staticmethod
    def is_invalid_window_handle_error(exc: BaseException) -> bool:
        """检查异常是否为窗口句柄失效错误 (winerror 1400)。"""
        return is_invalid_window_handle_error(exc)

    def is_valid_window(self, hwnd: int | None = None) -> bool:
        """验证指定窗口句柄是否有效且存活。"""
        target_hwnd = hwnd if hwnd is not None else self._hwnd
        if target_hwnd is None:
            return False
        try:
            h = int(target_hwnd)
            if h <= 0:
                return False
            # Win32 IsWindow API
            if hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32"):
                return bool(ctypes.windll.user32.IsWindow(h))
            return True
        except Exception:
            return False

    def grab(
        self,
        hwnd: int | None = None,
        region: tuple[int, int, int, int] | None = None,
    ) -> tuple[tuple[int, int, int, int] | None, np.ndarray | None]:
        """按配置或指定参数捕获目标帧。

        Args:
            hwnd: 可选覆盖窗口句柄。
            region: 可选覆盖区域坐标 (x, y, w, h)。

        Returns:
            (rect, frame): rect 为逻辑像素矩形，frame 为 BGR 图像 numpy ndarray。
            若目标无效或已关闭则返回 (None, None) 或 (rect, None)。
        """
        target_hwnd = hwnd
        target_region = region

        if target_hwnd is None and target_region is None:
            with self._target_lock:
                target_hwnd = self._hwnd
                target_region = self._region

        if target_region is not None:
            x, y, w, h = target_region
            if w <= 0 or h <= 0:
                return None, None
            frame = self._grab_region_fn(x, y, w, h)
            return target_region, frame

        if target_hwnd is not None:
            rect = self._get_window_rect_fn(target_hwnd)
            frame = self._grab_window_fn(target_hwnd)
            return rect, frame

        return None, None

    def release(self) -> None:
        """释放屏幕捕获资源."""
        if getattr(self, "_backend", None) is not None:
            self._backend.release()
        else:
            capture.release_mss()
