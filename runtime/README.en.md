# LocalScreen Translator runtime resources

OCR uses PP-OCRv6 ONNX detection and recognition models stored in `runtime\ocr\`. The plain local EXE includes those OCR runtime assets and translation DLLs, but does not distribute GGUF translation weights.

| Path | Purpose |
|---|---|
| `runtime\ocr\` | PP-OCRv6 models, character table, and integrity manifest |
| `runtime\llama-native\` | Pinned llama.cpp v0.5.0 / b11146 CPU and NVIDIA CUDA DLLs |
| `runtime\models\` | Optional user GGUF files; the default config still detects an existing HY-MT |

If `runtime\ocr\` is missing model files, download `ocr.zip` from the [GitHub release](https://github.com/2061863797/LocalScreenTranslator/releases/tag/2.0.0) and extract it into the project's `runtime` directory. The installer build checks and packages this directory. On a fresh machine, run `scripts\fetch_llama_native.ps1` to download fixed official assets, check SHA256, and extract only the needed DLLs. It does not extract llama-server and refuses to overwrite a nonempty target directory. Then run `build-exe.ps1`; the DLLs are copied into a new local EXE directory. GGUF weights are not included or overwritten.

Settings can directly reference an external GGUF or copy one to the user models directory. Save model and device settings to load them in the background without restarting. A custom GGUF needs a chat template compatible with llama.cpp v0.5.0; model quality, resource needs, and licensing vary.

[Back to README](../README.en.md)
