# -*- coding: utf-8 -*-
"""屏幕捕获：全屏/区域截图（mss），指定窗口捕获（Win32 PrintWindow）。"""

import ctypes
import threading
from abc import ABC, abstractmethod
from ctypes import wintypes

import numpy as np

# 进程级声明 DPI 感知，保证高分屏下坐标与像素一致
ctypes.windll.shcore.SetProcessDpiAwareness(2)

# 每项：(Qt 逻辑 x,y,w,h, mss 原生 x,y,w,h)。只保存整数，不跨线程访问 QScreen。
_SCREEN_LAYOUT: list[tuple[int, int, int, int, int, int, int, int]] = []
_LAYOUT_LOCK = threading.RLock()

# 备注译文浮层默认 WDA_EXCLUDEFROMCAPTURE：自家 OCR 抓屏看不到译文，
# 无需遮罩还原；用户在设置里打开「出现在截屏/录屏中」才切回 WDA_NONE
# 并启用遮罩还原。框选遮罩始终排除捕获（显示前已完成桌面抓取）。
_WDA_NONE = 0x00000000
_WDA_EXCLUDEFROMCAPTURE = 0x00000011
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_SetWindowDisplayAffinity = _user32.SetWindowDisplayAffinity
_SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_SetWindowDisplayAffinity.restype = wintypes.BOOL

# mss 实例按线程缓存：持续监视每轮都要抓屏，反复创建实例会重复申请
# 桌面 DC / 兼容 DC / DIB（本机实测每次多花约 1ms），且句柄反复开关。
# mss 实例非线程安全，故按线程隔离；抓屏只用绝对坐标、不读它的 monitors，
# 显示器拓扑变化不影响抓屏正确性。线程退出前由调用方 release_mss() 释放。
_mss_local = threading.local()


def _get_mss():
    """取本线程的 mss 实例（首次使用时创建）。"""
    import mss

    instance = getattr(_mss_local, "instance", None)
    if instance is None:
        instance = mss.MSS()
        _mss_local.instance = instance
    return instance


def release_mss() -> None:
    """释放本线程缓存的 mss 实例（线程退出前调用，避免 DC 泄漏）。"""
    instance = getattr(_mss_local, "instance", None)
    if instance is None:
        return
    _mss_local.instance = None
    try:
        instance.close()
    except Exception:
        pass


def set_window_capture_excluded(hwnd: int, excluded: bool) -> bool:
    """设置本进程顶层窗口是否从屏幕捕获中排除。"""
    try:
        handle = int(hwnd)
    except (TypeError, ValueError):
        return False
    if not handle:
        return False
    try:
        affinity = _WDA_EXCLUDEFROMCAPTURE if excluded else _WDA_NONE
        return bool(_SetWindowDisplayAffinity(handle, affinity))
    except Exception:
        # 旧版 Windows 或不支持的窗口类型会失败；屏幕捕获仍可继续。
        return False


def configure_qt_screens(screens) -> None:
    """在 Qt 主线程刷新逻辑坐标到原生像素的显示器映射。"""
    import mss

    try:
        with mss.MSS() as sct:
            native = [
                (int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"]))
                for m in sct.monitors[1:]
            ]
    except Exception:
        native = []
    unused = list(native)
    layout = []
    for screen in screens:
        geo = screen.geometry()
        lx, ly, lw, lh = geo.x(), geo.y(), geo.width(), geo.height()
        dpr = max(0.5, float(screen.devicePixelRatio()))
        expected = (lx, ly, round(lw * dpr), round(lh * dpr))
        if unused:
            chosen = min(
                unused,
                key=lambda m: (
                    abs(m[0] - expected[0]) + abs(m[1] - expected[1])
                    + abs(m[2] - expected[2]) + abs(m[3] - expected[3])
                ),
            )
            unused.remove(chosen)
        else:
            chosen = expected
        layout.append((lx, ly, lw, lh, *chosen))
    with _LAYOUT_LOCK:
        _SCREEN_LAYOUT[:] = layout


def _resize_bgr(img: np.ndarray, width: int, height: int) -> np.ndarray:
    if img.shape[1] == width and img.shape[0] == height:
        return img
    try:
        import cv2

        return cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)
    except Exception:
        ys = np.linspace(0, img.shape[0] - 1, height).astype(np.int32)
        xs = np.linspace(0, img.shape[1] - 1, width).astype(np.int32)
        return img[ys][:, xs]


def _native_rect_to_logical(
    x: int, y: int, width: int, height: int
) -> tuple[int, int, int, int]:
    with _LAYOUT_LOCK:
        layout = list(_SCREEN_LAYOUT)
    cx, cy = x + width / 2, y + height / 2
    for lx, ly, lw, lh, nx, ny, nw, nh in layout:
        if nx <= cx < nx + nw and ny <= cy < ny + nh:
            sx, sy = lw / max(1, nw), lh / max(1, nh)
            return (
                round(lx + (x - nx) * sx),
                round(ly + (y - ny) * sy),
                max(1, round(width * sx)),
                max(1, round(height * sy)),
            )
    return x, y, width, height


def grab_region(x: int, y: int, width: int, height: int) -> np.ndarray:
    """按 Qt 逻辑坐标截屏；混合 DPI 时分屏抓取并归一到逻辑像素。"""
    if width <= 0 or height <= 0:
        return np.empty((0, 0, 3), dtype=np.uint8)
    with _LAYOUT_LOCK:
        layout = list(_SCREEN_LAYOUT)
    if not layout:
        # 布局未知（尚未 configure_qt_screens）：单次探测，直接按原坐标抓
        import mss

        with mss.MSS() as sct:
            shot = sct.grab({"left": x, "top": y, "width": width, "height": height})
            return np.asarray(shot)[:, :, :3].copy()

    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    sct = _get_mss()
    try:
        for lx, ly, lw, lh, nx, ny, nw, nh in layout:
            ix1, iy1 = max(x, lx), max(y, ly)
            ix2, iy2 = min(x + width, lx + lw), min(y + height, ly + lh)
            if ix2 <= ix1 or iy2 <= iy1:
                continue
            sx, sy = nw / max(1, lw), nh / max(1, lh)
            px1 = round(nx + (ix1 - lx) * sx)
            py1 = round(ny + (iy1 - ly) * sy)
            px2 = round(nx + (ix2 - lx) * sx)
            py2 = round(ny + (iy2 - ly) * sy)
            shot = sct.grab({
                "left": px1, "top": py1,
                "width": max(1, px2 - px1), "height": max(1, py2 - py1),
            })
            part = np.asarray(shot)[:, :, :3].copy()
            part = _resize_bgr(part, ix2 - ix1, iy2 - iy1)
            canvas[iy1 - y:iy2 - y, ix1 - x:ix2 - x] = part
    except Exception:
        # 实例可能已失效（显示器拓扑变化等）：丢弃以便下次重建。注意只有会
        # 继续调用 grab_region 的调用方才谈得上自愈——监视线程遇到抓屏失败
        # 会直接结束会话，走不到这里说的「下次抓屏」。
        release_mss()
        raise
    return canvas


def list_windows() -> list[tuple[int, str]]:
    """枚举可见的顶层窗口，返回 (hwnd, 标题) 列表。"""
    import win32gui

    windows = []

    def _enum(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title.strip():
                windows.append((hwnd, title))

    win32gui.EnumWindows(_enum, None)
    return windows


def grab_window(hwnd: int) -> np.ndarray | None:
    """通过 PrintWindow 捕获指定窗口（含被遮挡部分）。失败返回 None。"""
    import win32gui
    import win32ui

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bmp)
        # PW_RENDERFULLCONTENT(=2)：支持 DirectX/硬件加速窗口
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        if not ok:
            return None
        info = bmp.GetInfo()
        data = bmp.GetBitmapBits(True)
        img = np.frombuffer(data, dtype=np.uint8).reshape(
            (info["bmHeight"], info["bmWidth"], 4)
        )
        img = img[:, :, :3].copy()
        px, py = win32gui.ClientToScreen(hwnd, (left, top))
        _, _, logical_w, logical_h = _native_rect_to_logical(px, py, width, height)
        return _resize_bgr(img, logical_w, logical_h)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """返回窗口客户区在屏幕上的位置 (x, y, w, h)，用于放置字幕层。"""
    import win32gui

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    x, y = win32gui.ClientToScreen(hwnd, (left, top))
    return _native_rect_to_logical(x, y, right - left, bottom - top)


class CaptureBackend(ABC):
    """屏幕与窗口捕获后端抽象基类 (PR 12)."""

    @abstractmethod
    def grab_region(self, x: int, y: int, width: int, height: int) -> np.ndarray:
        """根据屏幕坐标捕获区域画面 (BGR uint8 格式)."""
        pass

    @abstractmethod
    def grab_window(self, hwnd: int) -> np.ndarray | None:
        """根据窗口句柄捕获指定窗口客户区画面 (含遮挡区域). 失败返回 None."""
        pass

    @abstractmethod
    def release(self) -> None:
        """释放后端所持有的显存、线程局部 DC 或 COM 句柄."""
        pass

    def is_supported(self) -> bool:
        """探针：检查当前环境是否支持该后端."""
        return True


class MssCaptureBackend(CaptureBackend):
    """基于 MSS 的多显示器 DPI 归一化屏幕捕获后端 (PR 12)."""

    def grab_region(self, x: int, y: int, width: int, height: int) -> np.ndarray:
        return grab_region(x, y, width, height)

    def grab_window(self, hwnd: int) -> np.ndarray | None:
        try:
            x, y, w, h = get_window_rect(hwnd)
            if w <= 0 or h <= 0:
                return None
            return self.grab_region(x, y, w, h)
        except Exception:
            return None

    def release(self) -> None:
        release_mss()


class Win32PrintWindowBackend(CaptureBackend):
    """基于 Win32 PrintWindow (PW_RENDERFULLCONTENT) 的窗口捕获后端 (PR 12)."""

    def grab_region(self, x: int, y: int, width: int, height: int) -> np.ndarray:
        return grab_region(x, y, width, height)

    def grab_window(self, hwnd: int) -> np.ndarray | None:
        return grab_window(hwnd)

    def release(self) -> None:
        release_mss()


class DxgiCaptureBackend(CaptureBackend):
    """基于 DirectX 11 / DXGI Desktop Duplication 的硬件级高速捕获后端桩 (PR 12)."""

    def is_supported(self) -> bool:
        return False

    def grab_region(self, x: int, y: int, width: int, height: int) -> np.ndarray:
        raise NotImplementedError("DxgiCaptureBackend is not yet supported in current environment")

    def grab_window(self, hwnd: int) -> np.ndarray | None:
        raise NotImplementedError("DxgiCaptureBackend is not yet supported in current environment")

    def release(self) -> None:
        pass


class WgcCaptureBackend(CaptureBackend):
    """基于 Windows Graphics Capture (Windows.Graphics.Capture) 的现代低延迟捕获后端桩 (PR 12)."""

    def is_supported(self) -> bool:
        return False

    def grab_region(self, x: int, y: int, width: int, height: int) -> np.ndarray:
        raise NotImplementedError("WgcCaptureBackend is not yet supported in current environment")

    def grab_window(self, hwnd: int) -> np.ndarray | None:
        raise NotImplementedError("WgcCaptureBackend is not yet supported in current environment")

    def release(self) -> None:
        pass
