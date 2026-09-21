# -*- coding: utf-8 -*-
"""全局配置：加载、保存、默认值。"""

import json
import logging
import os
from pathlib import Path

from .paths import (
    CONFIG_PATH,
    DEFAULT_LLAMA_REL,
    DEFAULT_MODEL_REL,
    resolve_path,
    to_portable_path,
)

# 再导出，兼容 from app.config import CONFIG_PATH
__all__ = ["CONFIG_PATH", "DEFAULTS", "load", "save"]

_log = logging.getLogger("st.config")

DEFAULTS = {
    # llama.cpp / 模型：默认用项目内 runtime/（整夹可带走）
    "llama_dir": DEFAULT_LLAMA_REL,
    "model_path": DEFAULT_MODEL_REL,
    "server_host": "127.0.0.1",
    "server_port": 8080,
    "llama_device": "auto",       # auto / gpu / cpu
    "n_gpu_layers": 99,
    "threads": 8,
    # 翻译场景 prompt 短：2048 足够，比 4096 省 KV/显存、冷启动更快
    "ctx_size": 2048,
    # 单用户短请求：小 batch 即可，过大只占资源
    "batch_size": 512,
    "ubatch_size": 256,
    # 本工具同时只会打一路翻译
    "parallel_slots": 1,
    "flash_attn": True,
    # 模型已基本上 GPU 时 mlock 对推理帮助有限，却会拖慢启动；需要可改 true
    "mlock": False,
    # KV 量化：q8_0 在短上下文下几乎无损、更省显存/带宽；空字符串=服务端默认
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",

    # 翻译
    "target_language": "简体中文",   # 源语言自动识别，统一译为该语言（翻译窗口内可随时切换）
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
    "window_watch_annotate": False,   # True=备注，False=字幕条
    # 备注模式：跳过已是目标语言的行（窗口 / 区域各自独立）
    "window_annotate_skip_target_lang": False,

    # 区域持续翻译
    "region_watch_interval_ms": 800,
    "region_watch_font_size": 0,
    "region_watch_diff_threshold": 0.9,
    "region_watch_annotate": True,    # 区域默认备注更直观
    "region_annotate_skip_target_lang": False,

    # 备注模式译文颜色（窗口/区域共用，#RRGGBB）
    "annotate_text_color": "#00F0FF",
    # 备注译文是否出现在系统截屏/录屏中（窗口/区域共用）。
    # 关闭（默认）：浮层从屏幕捕获排除，区域备注无需遮罩还原底图，响应最快；
    # 开启：录屏能拍到译文，区域备注每帧额外还原底图，稍慢。
    "annotate_capture_visible": False,
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
        if "window_watch_annotate" not in raw:
            cfg["window_watch_annotate"] = v
        if "region_watch_annotate" not in raw:
            cfg["region_watch_annotate"] = v
    # 旧版共用「跳过目标语」→ 拆成窗口/区域（仅新键未写入文件时）
    if "annotate_skip_target_lang" in raw:
        v = bool(raw["annotate_skip_target_lang"])
        if "window_annotate_skip_target_lang" not in raw:
            cfg["window_annotate_skip_target_lang"] = v
        if "region_annotate_skip_target_lang" not in raw:
            cfg["region_annotate_skip_target_lang"] = v


def _validated_values(raw: object) -> dict:
    """只接收对象配置；已知键类型不合法时回退默认值。"""
    if not isinstance(raw, dict):
        raise ValueError("config.json 顶层必须是 JSON 对象")
    out = dict(raw)
    out.pop("ocr_lang", None)
    out.pop("hotkey_silent_ocr", None)
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

    if "server_port" in out and not 1 <= out["server_port"] <= 65535:
        out.pop("server_port")
    if "max_tokens" in out and not 64 <= out["max_tokens"] <= 8192:
        out.pop("max_tokens")
    if "ocr_provider" in out and out["ocr_provider"].lower() not in ("auto", "dml", "cpu"):
        out.pop("ocr_provider")
    if "llama_device" in out and out["llama_device"].lower() not in ("auto", "gpu", "cpu"):
        out.pop("llama_device")
    if "subtitle_mode" in out and out["subtitle_mode"] not in ("follow", "free", "pinned"):
        out.pop("subtitle_mode")
    if "ocr_device_id" in out and out["ocr_device_id"] < 0:
        out.pop("ocr_device_id")
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
    for key, rel in (
        ("llama_dir", DEFAULT_LLAMA_REL),
        ("model_path", DEFAULT_MODEL_REL),
    ):
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
    # 迁移后内存里可去掉旧共用键（磁盘仍保留到下次 save）
    if "window_annotate_skip_target_lang" in cfg and "region_annotate_skip_target_lang" in cfg:
        cfg.pop("annotate_skip_target_lang", None)
    _prefer_bundled_runtime(cfg)
    # 强制本机回环，避免误配 0.0.0.0 暴露 llama-server
    host = str(cfg.get("server_host") or "127.0.0.1").strip().lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        cfg["server_host"] = "127.0.0.1"
    else:
        cfg["server_host"] = "127.0.0.1"
    return cfg


def save(cfg: dict) -> None:
    # 不再写回已拆分的旧键；模型路径尽量写成相对 ROOT，方便整夹拷贝
    data = {k: v for k, v in cfg.items() if k != "annotate_skip_target_lang"}
    for key in ("llama_dir", "model_path"):
        if key in data and data[key]:
            data[key] = to_portable_path(data[key])
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
