# 本地屏译运行资源

OCR 使用 PP-OCRv6 ONNX 检测与识别模型；模型文件放在 `runtime\ocr\`。普通本地 EXE 会携带这些 OCR 运行资源及翻译动态库，不包含 GGUF 翻译模型权重。

| 路径 | 用途 |
|---|---|
| `runtime\ocr\` | PP-OCRv6 模型、字符表和完整性清单 |
| `runtime\llama-native\` | 固定 llama.cpp v0.5.0 / b11146 动态库，CPU 与 NVIDIA CUDA |
| `runtime\models\` | 可选的用户 GGUF 文件，默认配置仍识别已有 HY-MT |

若 `runtime\ocr\` 缺少模型文件，可从 [GitHub Releases](https://github.com/2061863797/LocalScreenTranslator/releases) 下载 `ocr.zip`，解压到项目的 `runtime` 目录。若 `runtime\llama-native\` 缺少动态库，可从 Release 下载 `llama-native.zip` 解压到 `runtime\llama-native`，或运行 `scripts\fetch_llama_native.ps1` 自动拉取官方 b11146 资产并验证 SHA256。本地 EXE 构建（`.\build-exe.ps1`）与安装包制作会自动检查并将动态库与 OCR 资源打包分发。构建结果不会携带 GGUF 权重，也不覆盖已有模型。

GGUF 翻译模型文件（如 `HY-MT1.5-1.8B-Q4_K_M.gguf`）可从 Release 下载后直接复制到 `runtime\models\` 或在设置页中外部引用。保存模型和设备设置后后台加载，无需重启。自定义 GGUF 应含与 llama.cpp v0.5.0 兼容的聊天模板；性能、翻译能力与许可由具体模型决定。

[返回 README](../README.md)
