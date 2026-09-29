# 本地屏译运行资源

OCR 使用 PP-OCRv6 ONNX 检测与识别模型；模型文件放在 `runtime\ocr\`。普通本地 EXE 会携带这些 OCR 运行资源及翻译动态库，不包含 GGUF 翻译模型权重。

| 路径 | 用途 |
|---|---|
| `runtime\ocr\` | PP-OCRv6 模型、字符表和完整性清单 |
| `runtime\llama-native\` | 固定 llama.cpp v0.5.0 / b11146 动态库，CPU 与 NVIDIA CUDA |
| `runtime\models\` | 可选的用户 GGUF 文件，默认配置仍识别已有 HY-MT |

若 `runtime\ocr\` 缺少模型文件，可从 [GitHub Release](https://github.com/2061863797/LocalScreenTranslator/releases/tag/2.0.0) 下载 `ocr.zip`，解压到项目的 `runtime` 目录。安装包构建会检查并打包该目录。新环境中可运行 `scripts\fetch_llama_native.ps1` 下载固定官方资产并验证 SHA256；脚本只提取翻译所需 DLL，不提取 llama-server。目录已有文件时脚本拒绝覆盖。完成后运行 `build-exe.ps1`，动态库被复制到新建的本地 EXE 目录。构建结果不会带 GGUF 权重，也不覆盖已有模型。

GGUF 可从设置页直接引用外部路径，或复制到用户模型目录。保存模型和设备设置后后台加载，无需重启。自定义 GGUF 应含与 llama.cpp v0.5.0 兼容的聊天模板；性能、翻译能力与许可由具体模型决定。

[返回 README](../README.md)
