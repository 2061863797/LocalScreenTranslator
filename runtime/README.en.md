# LocalScreen Translator runtime resources

OCR uses PP-OCRv6 ONNX detection and recognition models stored in `runtime\ocr\`. The plain local EXE includes those OCR runtime assets and translation DLLs, but does not distribute GGUF translation weights.

| Path | Purpose |
|---|---|
| `runtime\ocr\` | PP-OCRv6 models, character table, and integrity manifest |
| `runtime\llama-native\` | Pinned llama.cpp v0.5.0 / b11146 CPU and NVIDIA CUDA DLLs |
| `runtime\models\` | Optional user GGUF files; the default config still detects an existing HY-MT |

If `runtime\ocr\` is missing model files, download `ocr.zip` from [GitHub Releases](https://github.com/2061863797/LocalScreenTranslator/releases) and extract it into the project's `runtime` directory. If `runtime\llama-native\` is missing DLLs, download `llama-native.zip` from Releases and extract it to `runtime\llama-native`, or run `scripts\fetch_llama_native.ps1` to download and verify official b11146 assets. The local EXE build (`.\build-exe.ps1`) and installer build check and package these runtime directories. Build outputs do not distribute GGUF weights or overwrite existing models.

Translation model files (such as `HY-MT1.5-1.8B-Q4_K_M.gguf`) can be downloaded from Releases and placed into `runtime\models\` or referenced externally in Settings. Settings changes load in the background without restarting. Custom GGUFs require chat templates compatible with llama.cpp v0.5.0; performance and licensing depend on the model.

[Back to README](../README.en.md)
