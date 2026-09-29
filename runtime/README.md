# 本地屏译运行资源

OCR 使用 PP-OCRv6 ONNX 检测与识别模型，存放于 `runtime\ocr\`。独立安装包已内置 OCR 资源与 llama-native 动态库。

| 路径 | 用途 |
|---|---|
| `runtime\ocr\` | PP-OCRv6 模型、字符表和完整性清单 |
| `runtime\llama-native\` | llama.cpp v0.5.0 / b11146 原生动态库（CPU 与 CUDA） |
| `runtime\models\` | 本地 GGUF 翻译模型文件 |

源码运行或独立构建时：
- 若缺少 OCR 模型，从 [GitHub Releases](https://github.com/2061863797/LocalScreenTranslator/releases) 下载 `ocr.zip` 解压至 `runtime\ocr\`。
- 若缺少动态库，从 Release 下载 `llama-native.zip` 解压至 `runtime\llama-native\`，或运行 `scripts\fetch_llama_native.ps1` 自动拉取官方 b11146 资产。

GGUF 翻译模型（如 `HY-MT1.5-1.8B-Q4_K_M.gguf`）可从 Release 下载后放入 `runtime\models\` 或在设置页中外部引用，保存后在后台平滑生效。

[返回 README](../README.md)
