# LocalScreen Translator

LocalScreen Translator is a local Windows tray app for screenshot, selected-text, window live, and region live translation. It retains subtitles, annotations beside source text, history, and tray controls. [中文说明](./README.md)

## Architecture

| Stage | Implementation |
|---|---|
| OCR | PP-OCRv6 ONNX, DirectML first with CPU fallback |
| Translation | In-process llama.cpp v0.5.0; users import compatible GGUF models |
| UI | PySide6 |

OCR uses PP-OCRv6 detection and recognition models, leveraging DirectML hardware acceleration with automatic CPU fallback.

Translation automatically infers the source language from recognized text. The target language can be switched instantly from the control bar. Model and device settings apply smoothly in the background without restarting.

## Requirements and local setup

- 64-bit Windows.
- Python 3.11–3.13 for source runs (3.12 recommended). A built EXE or installer does not require Python on the target PC.
- A compatible NVIDIA driver for CUDA translation acceleration. Auto device selection can fall back to CPU. OCR uses DirectML and can fall back to CPU.

### Option 1: Quick start via GitHub Release (Recommended)

Get release assets from [GitHub Releases](https://github.com/2061863797/LocalScreenTranslator/releases):
1. **Download the installer**: Download and run `本地屏译-Setup.exe` (bundles PP-OCRv6 models and `llama-native` DLLs out of the box).
2. **Download the translation model**: Download `HY-MT1.5-1.8B-Q4_K_M.gguf` (or any compatible GGUF model).
3. **Import and run**: Launch the app, import the model under "Tray → Settings → Advanced", and start translating.

### Option 2: Run from source and local build

For source runs or offline development environments:
1. **Prepare runtime assets**:
   - Download `ocr.zip` from GitHub Releases and extract it into `runtime\ocr\` (contains `manifest.json`, `det.onnx`, etc.);
   - Download `llama-native.zip` from GitHub Releases and extract it into `runtime\llama-native\` (or run `scripts\fetch_llama_native.ps1` to fetch official b11146 DLLs).
2. **Initialize and run**:

```powershell
.\setup.ps1
venv\Scripts\python.exe scripts\smoke_import.py
venv\Scripts\pythonw.exe run.py
```

3. **Build local EXE**:

```powershell
.\build-exe.ps1
```

Each build creates a new `dist\local-timestamp\LocalScreenTranslator\` directory, automatically integrating `runtime\ocr` and `runtime\llama-native`. Run `LocalScreenTranslator.exe` directly.

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
- **Input & text translation**: Press `Ctrl+Enter` inside the input box to trigger translation instantly; view real-time source and translation character counters; one-click clear, copy feedback Toast, and window pinning.
- **Window live translation**: Supports real-time fuzzy title search filtering when selecting a target window, with full HWND tooltips for long titles.
- **HUD region selection**: Features luminous cyan framing, 4-corner HUD viewfinder crosshairs, and a dynamic dimension badge (real-time W × H px indicator).
- **Region subtitle mode**: The subtitle bar and region frame have separate control bars, each starting with a clean borderless `⠿` drag handle and a Pin button. When resizing the subtitle box via its bottom-right grip, the top control bar (350×30 px, 12 px font) remains stable and keeps full button titles without truncation.
- **Region annotation mode**: The control bar merges into a single strip docked on the top-left of the selection area, including drag handle, Pin, target language, subtitle switch, pause, and close.
- **Instant language switching**: Click the language button directly on the control bar to open the target language menu and retranslate immediately.

Window geometry and settings are persisted. Translation history and cache can be viewed and cleared under History or Settings.

## Troubleshooting

| Issue | Action |
|---|---|
| OCR model missing or invalid | Extract `ocr.zip` from GitHub Releases into the `runtime` directory, then restart the app |
| AI model fails to load | Check GGUF and its chat template; try CPU in Settings |
| Data and logs location | Portable mode uses app directory; installer uses `%LOCALAPPDATA%\LocalScreenTranslator` |

[Settings](./SETTINGS.en.md) · [Runtime files](./runtime/README.en.md) · [Third-party notices](./NOTICE) · [Source license](./LICENSE)
