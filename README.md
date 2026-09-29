# 本地屏译（LocalScreen Translator）

本地屏译是 Windows 本地屏幕翻译托盘工具，支持截屏、划词、窗口持续翻译、区域实时翻译、字幕与原文旁备注、历史记录。界面支持中文和英文。[English](./README.en.md)

## 当前架构

| 环节 | 实现 |
|---|---|
| OCR | PP-OCRv6 ONNX，ONNX Runtime DirectML 优先并回退 CPU |
| 翻译 | 进程内 llama.cpp v0.5.0，兼容 GGUF 由用户导入 |
| 显示 | PySide6 窗口和托盘 |

OCR 不依赖 Windows OCR 语言包。它使用 `runtime\ocr` 中的 PP-OCRv6 检测与识别模型；DirectML 可用时使用兼容显卡，不可用时回退 CPU。识别范围由随模型训练的语言能力决定。

翻译时只选择目标语言；AI 模型根据 OCR 提取的文字判断原文语言。目标语言在当前会话全局同步，每次显示一个目标语言。模型与设备设置保存后在后台加载，无需重启，加载失败会恢复原选择。

备注译文继续贴在原文旁。备注浮层始终从屏幕捕获中排除，避免下一轮 OCR 读到译文。本阶段没有原文覆盖翻译或复杂空白位置搜索。

## 运行条件与准备

- 64 位 Windows。
- 源码运行使用 Python 3.11～3.13，推荐 3.12；构建好的 EXE 不要求目标电脑安装 Python。
- `runtime\ocr` 中需有 PP-OCR 模型文件；`setup.ps1` 会检查完整性。
- 翻译 GPU 路线需要兼容的 NVIDIA 驱动；设备选“自动”时可回退 CPU。CUDA DLL 会使 EXE 目录明显变大。OCR 使用 DirectML，CPU 可作为回退。

源码首次准备：

如果工作区没有 `runtime\ocr`，先从 GitHub Release 下载 `ocr.zip` 并解压到项目 `runtime` 目录，使文件位于 `runtime\ocr\manifest.json` 等路径。

```powershell
.\setup.ps1
venv\Scripts\python.exe scripts\smoke_import.py
venv\Scripts\pythonw.exe run.py
```

如需构建普通本地 EXE，先在全新或空的 `runtime\llama-native` 目录准备固定版本 DLL；`scripts\fetch_llama_native.ps1` 会从官方 b11146 资产下载并校验 SHA256。构建会将 `runtime\ocr` 模型和 llama.cpp DLL 一起复制到新目录；GGUF 权重不随 EXE 分发。随后运行：

```powershell
.\build-exe.ps1
```

每次构建写入新的 `dist\local-日期时间\LocalScreenTranslator\` 目录，不覆盖已有 EXE。运行其中的 `LocalScreenTranslator.exe`，并保持整个目录完整。首次运行生成用户配置与历史。没有 MSIX、签名或安装器步骤。

## 导入翻译模型

**AI 翻译**：在“托盘 → 设置 → 高级”导入兼容 GGUF，可直接引用外部文件或复制进用户模型目录；HY-MT、Qwen、Gemma 需有当前 llama.cpp 可解析的聊天模板。选择模型与 CPU/GPU/自动设备后保存，后台加载。

## 操作

| 功能 | 默认热键 |
|---|---|
| 截屏翻译 | Alt+Q |
| 划词翻译 | Alt+W |
| 窗口持续翻译 | Alt+E |
| 区域实时翻译 | Alt+R |

托盘菜单可打开设置、历史、日志和退出。窗口与区域监视同一时间只能运行一个；窗口翻译固定以备注模式显示，区域翻译可在字幕和备注之间切换：
- **区域字幕模式**：翻译框与识别框各自拥有独立控制条，最前方均提供 `⠿` 拖动手柄与“固定”按钮。拖动翻译框右下角把手缩放面板时，上方状态栏尺寸（350×30 px，12 px 字号）保持稳定不变，所有按钮全称完整保留。
- **区域备注模式**：控制栏合并为单条居左停靠在识别区上方，集成手柄、固定、目标语言、切换字幕、暂停与关闭按钮。
- **目标语言热切换**：控制条内直接点击当前目标语言按钮即可弹出语言菜单，切换后自动重新翻译当前内容。

设置、翻译窗口和字幕条会记住有效位置。历史默认最多 50 条，翻译缓存默认最多 50,000 条，均为本机 `data.db` 中的明文；可分别关闭后续写入并在历史窗口清理。

## 常见问题

| 现象 | 处理 |
|---|---|
| OCR 模型缺失或校验失败 | 将 Release 的 `ocr.zip` 解压到 `runtime` 目录，然后重启应用 |
| AI 模型加载失败 | 检查 GGUF 与聊天模板；在设置中选 CPU 重试 |
| 备注未出现在系统截图/录屏 | 这是当前固定行为，目的是避免译文再次进入 OCR |
| 想查看数据和日志 | 便携模式在程序根目录，普通 EXE 通常位于 %LOCALAPPDATA%\LocalScreenTranslator |

[设置详解](./SETTINGS.md) · [运行资源说明](./runtime/README.md) · [第三方许可](./NOTICE) · [源码许可](./LICENSE)
