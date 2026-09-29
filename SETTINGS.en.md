# LocalScreen Translator — Settings

Open Settings from the tray. Saving applies model and device settings within the current process. Model loading runs in the background; a failure shows its reason and restores the previous selection. The UI supports Chinese and English. OCR uses the PP-OCRv6 ONNX model shipped as a runtime resource and does not need Windows OCR language packs.

| Option | Key | Meaning |
|---|---|---|
| GGUF model | `model_path` | Directly reference or import a valid GGUF; weights are not bundled |
| llama.cpp DLLs | `llama_library_dir` | Local development default `runtime/llama-native`; copied into the EXE directory on build |
| AI device | `llama_device` | `auto`, `gpu`, or `cpu` |
| Target language | `target_language` | One target at a time; changing it retranslates |
| OCR model | `runtime/ocr/` | PP-OCRv6 detector, recognizer, and character table |
| OCR device | `ocr_provider`, `ocr_device_id` | Auto prefers DirectML and falls back to CPU; CPU can be selected in config |
| OCR long-side cap | `ocr_max_side` | Default 1600; 0 disables additional long-side scaling |
| OCR minimum confidence | `ocr_score_min` | Default 0.45; lower-scoring lines are discarded |
| Generation limit | `max_tokens` | Default 512, allowed 64–8192 |
| CPU threads | `threads` | llama.cpp inference threads |
| Context | `ctx_size` | Default 2048 |
| Translation cache | `translation_cache_enabled` | Disable future writes; clear old cache in History |
| Local history | `history_enabled` | Disable future writes; clear old history in History |
| UI language | `ui_language` | `zh` or `en` |
| Translation window font | `translate_window_font_size` | 0=default or 12–20 px |
| Annotation color | `annotate_text_color` | `#RRGGBB` |
| Window/region interval | `window_watch_interval_ms` / `region_watch_interval_ms` | Default 800 ms each |
| Window/region font | `window_watch_font_size` / `region_watch_font_size` | 0=default or 12–20 px |
| Region annotations | `region_watch_annotate` | Boolean |
| Hotkeys | `hotkey_screenshot`, `hotkey_word`, `hotkey_window`, `hotkey_region_watch` | Alt+Q/W/E/R by default |

Place the OCR files `manifest.json`, `det.onnx`, `rec.onnx`, and `characters.txt` in `runtime/ocr/`. Download `ocr.zip` from the [GitHub release](https://github.com/2061863797/LocalScreenTranslator/releases/tag/2.0.0) and extract it into `runtime/`. Annotation overlays are always excluded from capture. Legacy WinRT OCR language fields and old translation settings are ignored on load and removed on the next save; history data is retained.

[中文](./SETTINGS.md) · [Home](./README.en.md)
