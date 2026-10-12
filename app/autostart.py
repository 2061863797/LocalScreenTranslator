# -*- coding: utf-8 -*-
"""开机自启动：读写当前用户的 Windows 启动项（HKCU\\...\\Run）。

以注册表为准：任务管理器中禁用或手动删除后，设置界面会如实显示为关闭。
开机启动时附带 AUTOSTART_FLAG，主程序据此推迟加载模型以减轻开机负担。
"""

from __future__ import annotations

import subprocess
import sys

from .applog import get_logger
from .paths import ROOT

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "LocalScreenTranslator"
AUTOSTART_FLAG = "--autostart"

_log = get_logger("autostart")


def launch_command() -> str:
    """开机时执行的命令：安装版启动 exe；源码运行直接用 venv 的 pythonw 启动 run.py。"""
    if getattr(sys, "frozen", False):
        args = [sys.executable, AUTOSTART_FLAG]
    else:
        pythonw = ROOT / "venv" / "Scripts" / "pythonw.exe"
        args = [str(pythonw), str(ROOT / "run.py"), AUTOSTART_FLAG]
    return subprocess.list2cmdline(args)


def launched_at_startup(argv: list[str] | None = None) -> bool:
    return AUTOSTART_FLAG in (sys.argv if argv is None else argv)


def is_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """写入或删除启动项；成功返回 True，失败记日志并返回 False。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        _log.info("开机自启动已%s", "开启" if enabled else "关闭")
        return True
    except OSError as exc:
        _log.warning("设置开机自启动失败: %s", exc)
        return False
