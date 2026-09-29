# LocalScreen Translator

LocalScreen Translator is a local Windows tray app for screenshot, selected-text, window live, and region live translation. It retains subtitles, annotations beside source text, history, and tray controls. [中文说明](./README.md)

## Architecture

| Stage | Implementation |
|---|---|
| OCR | PP-OCRv6 ONNX, DirectML first with CPU fallback |
| Translation | In-process llama.cpp v0.5.0; users import compatible GGUF models |
| UI | PySide6 |

OCR does not depend on Windows language packs. It uses the PP-OCRv6 detection and recognition models in `runtime\ocr`. It uses a compatible DirectML device when available and falls back to CPU. Language coverage depends on the model.

Only the target language is selected for translation; the AI model infers the source language from OCR text. The target language synchronizes across the current session, and one target language is displayed at a time. Model and device changes load in the background without restarting; a failed load restores the previous selection.

Annotations remain beside source text and are always excluded from screen capture to prevent OCR from reading translated text. Original-text replacement and complex blank-space placement are outside this stage.

## Requirements and local setup

- 64-bit Windows.
- Python 3.11–3.13 for source runs (3.12 recommended). A built EXE does not require Python on the target PC.
- PP-OCR model files in `runtime\ocr`; `setup.ps1` checks their integrity.
- A compatible NVIDIA driver for CUDA translation acceleration. Auto device selection can fall back to CPU. OCR uses DirectML and can fall back to CPU. CUDA DLLs add substantial size.

For a source run:

If `runtime\ocr` is missing, download `ocr.zip` from GitHub Releases and extract it into the project `runtime` directory, producing `runtime\ocr\manifest.json` and the model files.

```powershell
.\setup.ps1
venv\Scripts\python.exe scripts\smoke_import.py
venv\Scripts\pythonw.exe run.py
```

To build a plain local EXE, first prepare fixed-version DLLs in a new or empty `runtime\llama-native` directory. `scripts\fetch_llama_native.ps1` downloads official b11146 assets and checks SHA256. The build copies the OCR models and llama.cpp DLLs to a new folder; it does not package GGUF weights. Then run:

```powershell
.\build-exe.ps1
```

Each build creates a new `dist\local-timestamp\LocalScreenTranslator\` directory and leaves earlier EXEs untouched. Run `LocalScreenTranslator.exe` there and keep its whole folder together. It uses no MSIX, certificate, or installer.

## Import translation models

**AI mode:** Under Tray → Settings → Advanced, import a compatible GGUF by direct path or copy it into the user models directory. HY-MT, Qwen, and Gemma require a chat template understood by this llama.cpp version. Select the model and CPU/GPU/Auto, then save.

## Use

| Action | Default hotkey |
|---|---|
| Screenshot translation | Alt+Q |
| Selected-text translation | Alt+W |
| Window live translation | Alt+E |
| Region live translation | Alt+R |

The tray opens Settings, History, Logs, and Quit. One window or region live session can run at a time. Window translation always uses annotations beside the source text; region translation can switch between subtitles and annotations:
- **Region subtitle mode**: The subtitle bar and region frame have separate control bars, each starting with a `⠿` drag handle and a Pin button. When resizing the subtitle box via its bottom-right grip, the top control bar (350×30 px, 12 px font) remains stable and keeps full button titles without truncation.
- **Region annotation mode**: The control bar merges into a single strip docked on the top-left of the selection area, including drag handle, Pin, target language, subtitle switch, pause, and close.
- **Instant language switching**: Click the language button directly on the control bar to open the target language menu and retranslate immediately.

Valid window geometry is restored. History stores up to 50 items and the translation cache up to 50,000, in plain text in local `data.db`. You can disable future writes separately and clear existing entries in History.

## Troubleshooting

| Issue | Action |
|---|---|
| OCR model missing or invalid | Extract `ocr.zip` from GitHub Releases into the `runtime` directory, then restart the app |
| AI model fails to load | Check GGUF and its chat template; try CPU in Settings |
| Annotation is absent from screen recordings | The overlay is excluded so translated text does not reenter OCR |
| Need data or logs | Portable mode uses the app root; a plain EXE normally uses %LOCALAPPDATA%\LocalScreenTranslator |

[Settings](./SETTINGS.en.md) · [Runtime files](./runtime/README.en.md) · [Third-party notices](./NOTICE) · [Source license](./LICENSE)
