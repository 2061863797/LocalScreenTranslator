# -*- coding: utf-8 -*-
"""由 setup.ps1 调用：合并 example + 本机 GPU/线程，写出 config.json。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 5:
        print(
            "用法: setup_config.py <root> <use_gpu:0|1> <threads> <旧版层数占位> [device]",
            file=sys.stderr,
        )
        return 2

    root = Path(sys.argv[1])
    use_gpu = sys.argv[2] == "1"
    threads = int(sys.argv[3])
    _legacy_layers = int(sys.argv[4])
    device_override = sys.argv[5].strip().lower() if len(sys.argv) > 5 else ""

    cfg_path = root / "config.json"
    example = root / "config.example.json"

    defaults: dict = {
        "llama_library_dir": "runtime/llama-native",
        "model_path": "runtime/models/HY-MT1.5-1.8B-Q4_K_M.gguf",
        "llama_device": "auto",
        "threads": threads,
        "ctx_size": 2048,
        "batch_size": 512,
        "target_language": "简体中文",
        "max_tokens": 512,
        "history_enabled": True,
        "ocr_max_side": 1600,
        "ocr_provider": "auto",
        "ocr_device_id": 0,
        "hotkey_screenshot": "<alt>+q",
        "hotkey_word": "<alt>+w",
        "hotkey_window": "<alt>+e",
        "hotkey_region_watch": "<alt>+r",
        "window_watch_interval_ms": 800,
        "window_watch_diff_threshold": 0.9,
        "window_annotate_skip_target_lang": False,
        "region_watch_interval_ms": 800,
        "region_watch_diff_threshold": 0.9,
        "region_watch_annotate": True,
        "region_annotate_skip_target_lang": False,
    }
    if example.is_file():
        try:
            raw = json.loads(example.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                defaults.update(raw)
        except (OSError, json.JSONDecodeError):
            pass

    cfg = dict(defaults)
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except (OSError, json.JSONDecodeError):
            pass

    cfg.pop("ocr_lang", None)
    for obsolete in (
        "ocr_mode", "ocr_language_tag", "ocr_language_tags", "llama_dir",
        "server_host", "server_port", "n_gpu_layers", "ubatch_size",
        "parallel_slots", "flash_attn", "mlock", "cache_type_k", "cache_type_v",
        "annotate_capture_visible", "translation_mode", "nllb_model_path", "nllb_device",
        "source_language",
    ):
        cfg.pop(obsolete, None)
    cfg.pop("hotkey_silent_ocr", None)

    # setup 职责：便携路径 + 本机线程参数
    cfg["llama_library_dir"] = "runtime/llama-native"
    # 保留设置页选择的模型；首次安装或无效配置仍使用默认模型。
    if not isinstance(cfg.get("model_path"), str) or not cfg["model_path"].strip():
        cfg["model_path"] = "runtime/models/HY-MT1.5-1.8B-Q4_K_M.gguf"
    if device_override in {"gpu", "cpu"}:
        cfg["llama_device"] = device_override
    elif not isinstance(cfg.get("llama_device"), str) or cfg["llama_device"].lower() not in {"auto", "gpu", "cpu"}:
        cfg["llama_device"] = "auto"
    cfg["threads"] = threads

    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    tmp.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, cfg_path)
    print(f"threads={threads} use_gpu={use_gpu}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
