# -*- coding: utf-8 -*-
"""全局热键：键盘（pynput GlobalHotKeys）+ 鼠标侧键（pynput mouse.Listener）。

配置串约定：
- 键盘：pynput 格式，如 <alt>+q
- 鼠标侧键：mouse.x1（后退侧键）、mouse.x2（前进侧键）
  可与修饰键组合：<ctrl>+mouse.x1
"""

from __future__ import annotations

import threading

from pynput import keyboard, mouse
from PySide6.QtCore import QObject, Signal

from .applog import get_logger

_log = get_logger("hotkeys")

_HOTKEY_KEYS = (
    "hotkey_screenshot",
    "hotkey_word",
    "hotkey_window",
    "hotkey_region_watch",
)

def _hotkey_label(cfg_key: str) -> str:
    from .i18n import t

    m = {
        "hotkey_screenshot": "hk_shot",
        "hotkey_word": "hk_word",
        "hotkey_window": "hk_win_full",
        "hotkey_region_watch": "hk_region_full",
    }
    return t(m.get(cfg_key, cfg_key))

# 配置串中的鼠标侧键 token
_MOUSE_TOKENS = {
    "mouse.x1": mouse.Button.x1,
    "mouse.x2": mouse.Button.x2,
    # 兼容别名
    "mouse.button4": mouse.Button.x1,
    "mouse.button5": mouse.Button.x2,
    "mouse.back": mouse.Button.x1,
    "mouse.forward": mouse.Button.x2,
}

_MOD_TOKENS = ("<ctrl>", "<alt>", "<shift>", "<cmd>")


def is_mouse_hotkey(spec: str) -> bool:
    """是否含鼠标侧键（可单独或与修饰键组合）。"""
    s = (spec or "").strip().lower()
    if not s:
        return False
    for part in s.split("+"):
        if part.strip() in _MOUSE_TOKENS:
            return True
    return False


def parse_mouse_hotkey(spec: str) -> tuple[frozenset[str], object] | None:
    """解析为 (修饰键集合, pynput.Button)；失败返回 None。"""
    s = (spec or "").strip().lower()
    if not s:
        return None
    mods: set[str] = set()
    button = None
    for part in s.split("+"):
        p = part.strip()
        if p in _MOD_TOKENS:
            mods.add(p)
        elif p in _MOUSE_TOKENS:
            button = _MOUSE_TOKENS[p]
        else:
            return None
    if button is None:
        return None
    return frozenset(mods), button


def find_hotkey_conflicts(cfg: dict) -> list[str]:
    """返回冲突描述列表；无冲突返回空列表。"""
    seen: dict[str, str] = {}
    conflicts: list[str] = []
    for key in _HOTKEY_KEYS:
        val = (cfg.get(key) or "").strip().lower()
        if not val:
            continue
        label = _hotkey_label(key)
        if val in seen:
            conflicts.append(f"{seen[val]} / {label}: {cfg[key]}")
        else:
            seen[val] = label
    return conflicts


class HotkeyManager(QObject):
    screenshot_triggered = Signal()
    word_triggered = Signal()
    window_triggered = Signal()
    region_watch_triggered = Signal()
    escape_pressed = Signal()

    def __init__(self, cfg: dict):
        super().__init__()
        self._cfg = cfg
        self._kb_listener: keyboard.GlobalHotKeys | None = None
        self._mouse_listener: mouse.Listener | None = None
        # (frozenset mods, Button) -> callable
        self._mouse_map: dict[tuple[frozenset, object], object] = {}
        # 当前按下的修饰键（供侧键组合用）；修饰键监听线程写、
        # 鼠标监听线程读，是两个 pynput 线程，须加锁。
        self._mods_down: set[str] = set()
        self._mods_lock = threading.Lock()
        self._mod_listener: keyboard.Listener | None = None
        self._escape_listener: keyboard.Listener | None = None
        self._escape_enabled = False
        # 设置页录入热键时暂停，避免抢键
        self._paused = False

    def start(self) -> list[str]:
        """注册热键。返回冲突列表。"""
        self._stop_listeners()
        self._paused = False
        conflicts = find_hotkey_conflicts(self._cfg)
        mapping = {
            self._cfg["hotkey_screenshot"]: self.screenshot_triggered.emit,
            self._cfg["hotkey_word"]: self.word_triggered.emit,
            self._cfg["hotkey_window"]: self.window_triggered.emit,
            self._cfg["hotkey_region_watch"]: self.region_watch_triggered.emit,
        }
        kb_map: dict = {}
        mouse_map: dict[tuple[frozenset, object], object] = {}
        for k, v in mapping.items():
            if not k:
                continue
            if is_mouse_hotkey(k):
                parsed = parse_mouse_hotkey(k)
                if parsed is None:
                    continue
                if parsed not in mouse_map:
                    mouse_map[parsed] = v
            else:
                # 手工改坏的配置串会让 GlobalHotKeys 构造时抛 ValueError，
                # 这里先逐条校验，坏的跳过并留日志，不拖垮其余热键。
                try:
                    keyboard.HotKey.parse(k)
                except ValueError:
                    _log.warning("键盘热键格式无效，已跳过: %r", k)
                    continue
                if k not in kb_map:
                    kb_map[k] = v

        self._mouse_map = mouse_map

        if kb_map:
            try:
                self._kb_listener = keyboard.GlobalHotKeys(kb_map)
                self._kb_listener.daemon = True
                self._kb_listener.start()
            except Exception:
                _log.exception("注册键盘热键失败，键盘热键本次未生效")
                self._kb_listener = None

        if mouse_map:
            # 有修饰键组合时需要跟踪 Ctrl/Alt/Shift
            need_mods = any(mods for mods, _ in mouse_map.keys())
            if need_mods:
                with self._mods_lock:
                    self._mods_down = set()
                self._mod_listener = keyboard.Listener(
                    on_press=self._on_mod_press,
                    on_release=self._on_mod_release,
                )
                self._mod_listener.daemon = True
                self._mod_listener.start()
            self._mouse_listener = mouse.Listener(on_click=self._on_mouse_click)
            self._mouse_listener.daemon = True
            self._mouse_listener.start()

        return conflicts

    def pause(self) -> None:
        """设置页录入热键时调用：停全局监听，不改变配置。"""
        if self._paused:
            return
        self._paused = True
        self._stop_listeners()

    def resume(self) -> None:
        """录入结束：若处于 pause，按当前配置重新注册。"""
        if not self._paused:
            return
        self._paused = False
        self.start()

    def stop(self):
        self._paused = False
        self.set_escape_enabled(False)
        self._stop_listeners()

    def set_escape_enabled(self, enabled: bool) -> None:
        """截屏原位替换显示期间临时监听全局 Esc，用于关闭覆盖层。"""
        enabled = bool(enabled)
        if enabled == self._escape_enabled:
            return
        self._escape_enabled = enabled
        if not enabled:
            listener = self._escape_listener
            self._escape_listener = None
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
            return
        try:
            listener = keyboard.Listener(on_press=self._on_escape)
            listener.daemon = True
            listener.start()
            self._escape_listener = listener
        except Exception:
            self._escape_listener = None
            _log.exception("Esc 监听启动失败")

    def _on_escape(self, key):
        if not self._escape_enabled:
            return
        try:
            if key == keyboard.Key.esc:
                self.escape_pressed.emit()
        except Exception:
            pass

    def _stop_listeners(self):
        if self._kb_listener:
            try:
                self._kb_listener.stop()
            except Exception:
                pass
            self._kb_listener = None
        if self._mouse_listener:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None
        if self._mod_listener:
            try:
                self._mod_listener.stop()
            except Exception:
                pass
            self._mod_listener = None
        self._mouse_map = {}
        with self._mods_lock:
            self._mods_down = set()

    def _on_mod_press(self, key):
        token = self._key_to_mod_token(key)
        if token:
            with self._mods_lock:
                self._mods_down.add(token)

    def _on_mod_release(self, key):
        token = self._key_to_mod_token(key)
        if token:
            with self._mods_lock:
                self._mods_down.discard(token)

    @staticmethod
    def _key_to_mod_token(key) -> str | None:
        try:
            if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                return "<ctrl>"
            if key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr):
                return "<alt>"
            if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
                return "<shift>"
            if key in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r):
                return "<cmd>"
        except Exception:
            pass
        return None

    def _on_mouse_click(self, x, y, button, pressed):
        if not pressed:
            return
        if button not in (mouse.Button.x1, mouse.Button.x2):
            return
        with self._mods_lock:
            mods = frozenset(self._mods_down)
        # 优先精确匹配当前修饰键；若无匹配再试「仅侧键」
        cb = self._mouse_map.get((mods, button))
        if cb is None and mods:
            cb = self._mouse_map.get((frozenset(), button))
        if cb is not None:
            try:
                cb()
            except Exception:
                # 信号 emit 一般不失败；真失败时留日志，别无声丢热键
                _log.exception("鼠标热键回调异常 button=%s mods=%s", button, set(mods))
