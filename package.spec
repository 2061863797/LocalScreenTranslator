# -*- mode: python ; coding: utf-8 -*-
"""LocalScreenTranslator PyInstaller 打包规范文件

配置重点：
1. 采用 onedir 模式，保证启动效率与 DLL 依赖清晰；
2. 显式过滤外部注入的 icuuc.dll / icudt*.dll，确保使用 Windows 系统级标准库，
   彻底解决 `ImportError: DLL load failed while importing QtCore: 找不到指定的程序`；
3. 声明项目全部核心模块与 C 扩展的 hiddenimports。
"""

import os
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).resolve()

hidden_imports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "app.main",
    "app.applog",
    "app.capture",
    "app.config",
    "app.hotkeys",
    "app.i18n",
    "app.llama_server",
    "app.ocr_engine",
    "app.paths",
    "app.pipelines",
    "app.runtime_resources",
    "app.selection",
    "app.storage",
    "app.textlang",
    "app.translator",
    "app.window_watcher",
    "app.workers",
    "app.ui.overlays",
    "app.ui.topmost",
    "app.ui.windows",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "pywintypes",
    "win32gui",
    "win32con",
    "win32api",
    "win32process",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "cv2",
    "pyclipper",
    "mss",
    "PIL",
]

datas = [
    (str(ROOT / "icon.ico"), "."),
    (str(ROOT / "config.example.json"), "."),
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "jupyter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

EXCLUDED_BINARY_PREFIXES = ("icuuc", "icudt", "icuin", "icutu", "icutest", "icui18n")
cleaned_binaries = []
for binary in a.binaries:
    dest_name, src_path, type_code = binary
    dest_lower = dest_name.lower()
    if any(dest_lower.startswith(prefix) for prefix in EXCLUDED_BINARY_PREFIXES):
        print(f"[package.spec] 已过滤外部冲突 DLL: {dest_name} (来源: {src_path})")
        continue
    cleaned_binaries.append(binary)

a.binaries = cleaned_binaries

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalScreenTranslator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LocalScreenTranslator",
)
