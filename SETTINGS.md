# 本地屏译 — 设置说明

从托盘打开“设置”；模型与设备设置保存后在当前进程中生效。模型加载在后台运行，失败会显示原因并恢复原选择。界面语言支持中文和英文。OCR 使用随应用运行资源提供的 PP-OCRv6 ONNX 模型，不需要 Windows OCR 语言包。

| 选项 | 配置键 | 说明 |
|---|---|---|
| GGUF 模型 | `model_path` | 直接引用或导入有效 GGUF；权重不随 EXE 提供 |
| llama.cpp 动态库 | `llama_library_dir` | 本地开发默认 `runtime/llama-native`，构建时复制进 EXE 目录 |
| AI 设备 | `llama_device` | `auto`、`gpu`、`cpu` |
| 目标语言 | `target_language` | 每次显示一种；可在翻译窗口和实时控制条切换并重译 |
| OCR 模型 | `runtime/ocr/` | PP-OCRv6 检测、识别模型及字符表；需随项目恢复或单独下载 |
| OCR 设备 | `ocr_provider`、`ocr_device_id` | 默认自动优先 DirectML；失败时回退 CPU，也可在配置中指定 CPU |
| OCR 长边限制 | `ocr_max_side` | 默认 1600；0 表示不做额外长边缩放 |
| OCR 最低置信度 | `ocr_score_min` | 默认 0.45；低于此值的识别行会被丢弃 |
| 生成上限 | `max_tokens` | 默认 512；可选 64–8192 |
| CPU 线程 | `threads` | llama.cpp 推理线程数 |
| 上下文 | `ctx_size` | 默认 2048 |
| 翻译缓存 | `translation_cache_enabled` | 关闭后不再写入；旧缓存可在历史窗口清除 |
| 本地历史 | `history_enabled` | 关闭后不再写入；旧历史可在历史窗口清除 |
| 界面语言 | `ui_language` | `zh` 或 `en` |
| 翻译窗口字号 | `translate_window_font_size` | 0=默认，或 12–20 px |
| 备注译文颜色 | `annotate_text_color` | `#RRGGBB` |
| 窗口/区域监视间隔 | `window_watch_interval_ms` / `region_watch_interval_ms` | 默认各 800 ms |
| 窗口/区域字号 | `window_watch_font_size` / `region_watch_font_size` | 0=默认，或 12–20 px |
| 区域备注 | `region_watch_annotate` | 布尔值 |
| 热键 | `hotkey_screenshot`、`hotkey_word`、`hotkey_window`、`hotkey_region_watch` | 默认 Alt+Q/W/E/R |

OCR 模型文件应放在 `runtime/ocr/`，包含 `manifest.json`、`det.onnx`、`rec.onnx` 和 `characters.txt`。可从 [GitHub Release](https://github.com/2061863797/LocalScreenTranslator/releases/tag/2.0.0) 下载 `ocr.zip` 并解压到 `runtime/` 目录。备注浮层始终从捕获中排除。旧版 WinRT OCR 语言字段及旧翻译设置读取时忽略，下次保存会清除；历史数据库保留。

[English](./SETTINGS.en.md) · [返回首页](./README.md)
