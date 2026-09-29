# LocalScreen Translator runtime resources

OCR uses PP-OCRv6 ONNX detection and recognition models located in `runtime\ocr\`. The installer bundles OCR assets and llama-native runtime DLLs.

| Path | Purpose |
|---|---|
| `runtime\ocr\` | PP-OCRv6 models, character table, and integrity manifest |
| `runtime\llama-native\` | llama.cpp v0.5.0 / b11146 native DLLs (CPU and CUDA) |
| `runtime\models\` | Local GGUF translation model files |

For source runs or standalone builds:
- If OCR models are missing, download `ocr.zip` from [GitHub Releases](https://github.com/2061863797/LocalScreenTranslator/releases) and extract to `runtime\ocr\`.
- If native DLLs are missing, download `llama-native.zip` from Releases and extract to `runtime\llama-native\`, or run `scripts\fetch_llama_native.ps1` to fetch official b11146 assets.

Translation models (such as `HY-MT1.5-1.8B-Q4_K_M.gguf`) can be downloaded from Releases and placed in `runtime\models\` or referenced externally in Settings. Settings apply in the background without restarting.

[Back to README](../README.en.md)
