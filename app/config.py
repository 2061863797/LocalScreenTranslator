# -*- coding: utf-8 -*-
"""全局配置：加载、保存、默认值。"""

import json
import logging
import os

from .paths import (
    CONFIG_PATH,
    DEFAULT_MODEL_REL,
    resolve_path,
    to_portable_path,
)

# 再导出，兼容 from app.config import CONFIG_PATH
__all__ = ["CONFIG_PATH", "DEFAULTS", "load", "save"]

_log = logging.getLogger("st.config")

DEFAULTS = {
    # 模型由用户导入，llama.cpp 动态库随本地 EXE 提供
    "model_path": DEFAULT_MODEL_REL,
    "llama_library_dir": "runtime/llama-native",
    "llama_device": "auto",       # auto / gpu / cpu
    "threads": 8,
    "ctx_size": 2048,
    "batch_size": 512,

    # 翻译
    "target_language": "简体中文",   # 翻译结果统一使用该语言（翻译窗口内可随时切换）
    # 设置窗口界面语言：zh | en
    "ui_language": "zh",
    # 单次生成上限；屏幕翻译通常几百 token 内，过大只浪费上限预留
    "max_tokens": 512,
    # 本地明文历史；可在设置中关闭，并可在历史窗口手动清空
    "history_enabled": True,
    # 持久化翻译缓存：加速重复文本识别；注重隐私可设为 false（关闭历史不等于不落盘缓存）
    "translation_cache_enabled": True,
    # 0=沿用原有默认；非 0 为正文像素字号
    "translate_window_font_size": 0,
    "settings_window_geometry": [],
    "history_window_geometry": [],
    "translate_window_geometry": [],
    "subtitle_geometry": [],
    "subtitle_mode": "follow",

    # OCR
    "ocr_provider": "auto",         # auto = DirectML 优先，失败时回退 CPU
    "ocr_device_id": 0,
    # 识别前长边缩放到此像素（0=不缩放）；4K 截屏可明显降 OCR 耗时
    "ocr_max_side": 1600,
    # 低于此置信度的 OCR 行丢弃（0~1，0=不丢）
    "ocr_score_min": 0.45,

    # 热键
    "hotkey_screenshot": "<alt>+q",  # 截屏翻译
    "hotkey_word": "<alt>+w",        # 划词翻译
    "hotkey_window": "<alt>+e",      # 窗口持续翻译
    "hotkey_region_watch": "<alt>+r",  # 框选区域实时翻译

    # 窗口持续翻译（与区域分开）
    "window_watch_interval_ms": 800,
    "window_watch_font_size": 0,
    "window_watch_diff_threshold": 0.9,

    # 区域持续翻译
    "region_watch_interval_ms": 800,
    "region_watch_font_size": 0,
    "region_watch_diff_threshold": 0.9,
    "region_watch_annotate": True,    # 区域默认备注更直观

    # 备注模式译文颜色（窗口/区域共用，#RRGGBB）
    "annotate_text_color": "#00F0FF",
}


def _migrate_legacy(cfg: dict, raw: dict) -> None:
    """旧版共用 watch_* 迁移到 window_/region_ 前缀（仅当新键未在文件中出现时）。"""
    if "watch_interval_ms" in raw:
        try:
            v = int(raw["watch_interval_ms"])
        except (TypeError, ValueError):
            v = DEFAULTS["window_watch_interval_ms"]
        if "window_watch_interval_ms" not in raw:
            cfg["window_watch_interval_ms"] = v
        if "region_watch_interval_ms" not in raw:
            cfg["region_watch_interval_ms"] = v
    if "watch_diff_threshold" in raw:
        try:
            v = float(raw["watch_diff_threshold"])
        except (TypeError, ValueError):
            v = DEFAULTS["window_watch_diff_threshold"]
        if "window_watch_diff_threshold" not in raw:
            cfg["window_watch_diff_threshold"] = v
        if "region_watch_diff_threshold" not in raw:
            cfg["region_watch_diff_threshold"] = v
    if "watch_annotate" in raw:
        v = bool(raw["watch_annotate"])
        if "region_watch_annotate" not in raw:
            cfg["region_watch_annotate"] = v
def _validated_values(raw: object) -> dict:
    """只接收对象配置；已知键类型不合法时回退默认值。"""
    if not isinstance(raw, dict):
        raise ValueError("config.json 顶层必须是 JSON 对象")
    out = dict(raw)
    out.pop("ocr_lang", None)
    for obsolete in (
        "ocr_mode", "ocr_language_tag", "ocr_language_tags", "llama_dir",
        "window_watch_annotate",
        "annotate_skip_target_lang",
        "window_annotate_skip_target_lang",
        "region_annotate_skip_target_lang",
        "server_host", "server_port", "n_gpu_layers", "ubatch_size",
        "parallel_slots", "flash_attn", "mlock", "cache_type_k", "cache_type_v",
        "translation_mode", "nllb_model_path", "nllb_device",
    ):
        out.pop(obsolete, None)
    out.pop("source_language", None)
    out.pop("hotkey_silent_ocr", None)
    out.pop("annotate_capture_visible", None)
    for key, default in DEFAULTS.items():
        if key not in raw:
            continue
        value = raw[key]
        valid = True
        if default is None:
            valid = value is None or isinstance(value, str)
        elif isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, float):
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif isinstance(default, str):
            valid = isinstance(value, str)
        elif isinstance(default, list):
            valid = (
                isinstance(value, list) and len(value) in (0, 4)
                and all(isinstance(item, int) for item in value)
            )
        if not valid:
            _log.warning("配置项 %s 类型无效，使用默认值", key)
            out.pop(key, None)

    if "max_tokens" in out and not 64 <= out["max_tokens"] <= 8192:
        out.pop("max_tokens")
    if "ocr_provider" in out and out["ocr_provider"].lower() not in ("auto", "dml", "cpu"):
        out.pop("ocr_provider")
    if "ocr_device_id" in out and out["ocr_device_id"] < 0:
        out.pop("ocr_device_id")
    if "llama_device" in out and out["llama_device"].lower() not in ("auto", "gpu", "cpu"):
        out.pop("llama_device")
    if "subtitle_mode" in out and out["subtitle_mode"] not in ("follow", "free", "pinned"):
        out.pop("subtitle_mode")
    if "ocr_max_side" in out and not 0 <= out["ocr_max_side"] <= 10000:
        out.pop("ocr_max_side")
    for key in ("window_watch_interval_ms", "region_watch_interval_ms"):
        if key in out and not 50 <= out[key] <= 60000:
            out.pop(key)
    for key in (
        "translate_window_font_size",
        "window_watch_font_size",
        "region_watch_font_size",
    ):
        if key in out and out[key] != 0 and not 12 <= out[key] <= 20:
            out.pop(key)
    for key in (
        "window_watch_diff_threshold",
        "region_watch_diff_threshold",
        "ocr_score_min",
    ):
        if key in out and not 0 <= float(out[key]) <= 1:
            out.pop(key)
    return out


def _prefer_bundled_runtime(cfg: dict) -> None:
    """配置里的绝对路径失效时，自动切到项目内 runtime/。"""
    for key, rel in (("model_path", DEFAULT_MODEL_REL),):
        cur = cfg.get(key) or ""
        try:
            ok = resolve_path(cur).exists()
        except OSError:
            ok = False
        if ok:
            continue
        bundled = resolve_path(rel)
        if bundled.exists():
            cfg[key] = rel


def load() -> dict:
    """读取配置，缺失项用默认值补齐。路径可写相对 ROOT 的 runtime/…。"""
    cfg = dict(DEFAULTS)
    raw: dict = {}
    if CONFIG_PATH.exists():
        try:
            parsed = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            raw = _validated_values(parsed)
            cfg.update(raw)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            _log.warning("配置读取失败，使用默认值: %s", e)
            raw = {}
    _migrate_legacy(cfg, raw)
    _prefer_bundled_runtime(cfg)
    return cfg


def save(cfg: dict) -> None:
    # 不再写回已拆分的旧键；模型路径尽量写成相对 ROOT，方便整夹拷贝
    obsolete = {
        "annotate_skip_target_lang", "window_annotate_skip_target_lang",
        "region_annotate_skip_target_lang", "annotate_capture_visible", "ocr_lang",
        "window_watch_annotate", "watch_annotate",
        "ocr_mode", "ocr_language_tag", "ocr_language_tags", "llama_dir",
        "source_language",
        "server_host", "server_port", "n_gpu_layers", "ubatch_size",
        "parallel_slots", "flash_attn", "mlock", "cache_type_k", "cache_type_v",
        "translation_mode", "nllb_model_path", "nllb_device",
    }
    data = {k: v for k, v in cfg.items() if k not in obsolete}
    for key in ("llama_library_dir", "model_path"):
        if key in data and data[key]:
            data[key] = to_portable_path(data[key])
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
