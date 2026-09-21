# -*- coding: utf-8 -*-
"""项目根路径、便携 runtime 与运行期数据文件位置。

目录约定（一文件夹可带走）::

    <ROOT>/
      run.py / 翻译.exe / venv / app / config.json
      runtime/
        llama/          # llama-server.exe + 依赖 DLL
        models/         # HY-MT 等 .gguf
        ocr/            # ONNX OCR 模型与清单
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 源码运行时 app/ 的上一级是软件根目录；PyInstaller 打包后以 exe
# 所在目录为根，确保 runtime 位于便携软件目录。
ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)


def get_data_dir() -> Path:
    """运行期可写用户数据目录（配置、日志与数据库）。

    - 便携模式：ROOT 下存在 portable.flag（或源码开发环境已有 config.json），数据保存在 ROOT；
    - 安装模式（PyInstaller 安装包装入 Program Files）：写入系统标准 %LOCALAPPDATA%/LocalScreenTranslator。
    """
    if (ROOT / "portable.flag").exists():
        return ROOT
    if not getattr(sys, "frozen", False) and (ROOT / "config.json").exists():
        return ROOT

    try:
        from PySide6.QtCore import QCoreApplication, QStandardPaths

        if not QCoreApplication.instance():
            QCoreApplication.setApplicationName("LocalScreenTranslator")
        loc = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        if loc:
            p = Path(loc)
            if p.name != "LocalScreenTranslator":
                p = p / "LocalScreenTranslator"
            p.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass

    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        p = Path(local_app) / "LocalScreenTranslator"
    else:
        p = Path.home() / "AppData" / "Local" / "LocalScreenTranslator"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _migrate_legacy_data_if_needed(data_dir: Path) -> None:
    """若从旧版本便携目录升级到安装版用户目录，自动平滑迁移现有配置与历史数据库。"""
    if data_dir == ROOT or (ROOT / "portable.flag").exists():
        return
    import shutil

    for filename in ("config.json", "data.db"):
        src = ROOT / filename
        dst = data_dir / filename
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


DATA_DIR = get_data_dir()
_migrate_legacy_data_if_needed(DATA_DIR)
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "app.log"
DB_PATH = DATA_DIR / "data.db"
ICON_ICO = ROOT / "icon.ico"

# 内置资源（相对 ROOT，写入 config 时用正斜杠）
RUNTIME_DIR = ROOT / "runtime"
RUNTIME_LLAMA = RUNTIME_DIR / "llama"
RUNTIME_MODELS = RUNTIME_DIR / "models"
RUNTIME_OCR = RUNTIME_DIR / "ocr"
USER_MODELS = DATA_DIR / "models"
USER_MODELS.mkdir(parents=True, exist_ok=True)

DEFAULT_GGUF_NAME = "HY-MT1.5-1.8B-Q4_K_M.gguf"
DEFAULT_MODEL_REL = f"runtime/models/{DEFAULT_GGUF_NAME}"
DEFAULT_LLAMA_REL = "runtime/llama"


def resolve_path(value: str | Path) -> Path:
    """相对路径优先相对于 ROOT，若不存在则尝试 DATA_DIR / USER_MODELS；绝对路径原样 resolve。"""
    p = Path(value)
    if not p.is_absolute():
        candidate_root = (ROOT / p).resolve()
        if candidate_root.exists():
            return candidate_root
        candidate_user = (DATA_DIR / p).resolve()
        if candidate_user.exists():
            return candidate_user
        candidate_model = (USER_MODELS / p.name).resolve()
        if candidate_model.exists():
            return candidate_model
        return candidate_root
    return p.resolve()


def to_portable_path(value: str | Path) -> str:
    """若在 ROOT 下则存相对路径（正斜杠），便于整夹拷贝。"""
    p = Path(value)
    if not p.is_absolute():
        # 已是相对：规范化分隔符
        return str(Path(value)).replace("\\", "/")
    p = p.resolve()
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        try:
            return str(p.relative_to(DATA_DIR)).replace("\\", "/")
        except ValueError:
            return str(p)


def is_gguf_model(path: str | Path, check_suffix: bool = True) -> bool:
    """仅做本地模型选择所需的轻量校验：扩展名（可选）与 GGUF 文件头。"""
    p = Path(path)
    if check_suffix and p.suffix.lower() != ".gguf":
        return False
    if not p.is_file():
        return False
    try:
        with p.open("rb") as stream:
            return stream.read(4) == b"GGUF"
    except OSError:
        return False


def available_translation_models(models_dir: str | Path | None = None) -> list[Path]:
    """列出 models 顶层可选择的有效 GGUF，支持同时扫描内置 runtime 与用户目录。"""
    if models_dir is not None:
        dirs = [Path(models_dir)]
    else:
        dirs = [RUNTIME_MODELS, USER_MODELS]
    found: dict[str, Path] = {}
    for d in dirs:
        try:
            for path in d.iterdir():
                if is_gguf_model(path):
                    if path.name not in found:
                        found[path.name] = path
        except OSError:
            pass
    return sorted(found.values(), key=lambda path: path.name.casefold())


def runtime_status() -> dict:
    """诊断用：内置资源是否齐全。"""
    models = available_translation_models()
    gguf = resolve_path(DEFAULT_MODEL_REL)
    model_file = gguf.is_file() or len(models) > 0
    actual_model_path = str(gguf) if gguf.is_file() else (str(models[0]) if models else str(gguf))
    return {
        "root": str(ROOT),
        "llama_server": (RUNTIME_LLAMA / "llama-server.exe").is_file(),
        "model": model_file,
        "ocr_models": all((RUNTIME_OCR / name).is_file() for name in (
            "manifest.json", "det.onnx", "rec.onnx", "characters.txt"
        )),
        "llama_dir": str(RUNTIME_LLAMA),
        "model_path": actual_model_path,
        "ocr": str(RUNTIME_OCR),
    }
