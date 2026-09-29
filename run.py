# -*- coding: utf-8 -*-
"""启动入口：python run.py

进入主程序并使用项目内便携 runtime。
"""

import sys

if "--ocr-smoke-check" in sys.argv:
    from app import config
    from app.ocr_engine import OcrEngine

    engine = OcrEngine(config.load())
    engine.preload()
    print(f"PP-OCR ONNX smoke OK: {engine.provider}")
    raise SystemExit(0)

if "--smoke-check" in sys.argv:
    from app.main import main
    from PySide6.QtCore import qVersion
    print(f"Smoke check OK: Qt {qVersion()}")
    sys.exit(0)

if "--native-smoke-check" in sys.argv:
    from app import config
    from app.paths import resolve_path
    from app.translation_runtime.llama_native import LlamaNativeBackend

    cfg = config.load()
    backend = LlamaNativeBackend(resolve_path(cfg["llama_library_dir"]), cfg)
    backend._library()
    backend.close()
    print("Native llama.cpp DLL OK")
    sys.exit(0)

if "--ai-smoke-check" in sys.argv:
    import tempfile
    from pathlib import Path

    from app import config
    from app.storage import Storage
    from app.translation_runtime.router import TranslationRouter

    try:
        index = sys.argv.index("--ai-smoke-check")
        model_path = sys.argv[index + 1]
        cfg = config.load()
        cfg.update(model_path=model_path, llama_device="auto", source_language="英语")
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "smoke.db")
            router = TranslationRouter(cfg, storage)
            try:
                router.preload()
                translated = router.translate("Hello world", "简体中文", "smoke")
                if not translated:
                    raise RuntimeError("AI 翻译返回空结果")
                print(f"AI smoke OK: {translated}")
            finally:
                router.close()
                storage.close()
    except Exception as exc:
        print(f"AI smoke failed: {exc}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
