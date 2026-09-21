# 本地屏译（LocalScreen Translator）

本地屏译是一款 Windows 本地屏幕翻译工具，支持截屏、划词、窗口持续翻译和区域实时翻译。

- OCR：ONNX Runtime DirectML + PP-OCRv6（不可用时自动回退 CPU）
- 翻译：本机 `llama-server` + HY-MT1.5
- 界面：Windows 托盘应用，中英文可切换
- 协议：[Apache-2.0](./LICENSE)；第三方和模型许可见 [NOTICE](./NOTICE)
- English: [README.en.md](./README.en.md)

## 基础说明

安装完成后，OCR 和翻译都在本机运行。

当前 **ocr** 附件的字符表覆盖中文、英文和日文，不包含韩文字符；OCR 可识别语种由附件中的识别模型决定。

安装所需文件和资源来源如下：

| 内容 | 来源 | 放置位置 |
|------|------|----------|
| 程序源码 | 源码包 | `app\`、`run.py`、安装脚本等 |
| ONNX OCR 模型 | Releases 的 **ocr** 附件 | `runtime\ocr\` |
| HY-MT 翻译模型 | Releases 的 **models** 附件 | `runtime\models\` |
| CPU/CUDA llama-server | Releases 的通用 **llama** 附件 | `runtime\llama\` |
| Python 环境、本机配置和便捷启动器 | 运行 `setup.ps1` 后生成 | `venv\`、`config.json`、`翻译.exe` |

`翻译.exe` 是便捷启动器，不是包含全部依赖的单文件程序；它仍需要同目录下的 `venv`、`app`、`run.py` 和 `runtime`。

## 通用运行条件

电脑需要满足：

- Windows 10/11 64 位；
- Python 3.11、3.12 或 3.13，推荐 **Python 3.12**，不要使用 3.14；
- 支持 DirectX 12 的显卡可使用 DirectML 加速 OCR；不满足时自动使用 CPU；
- 至少约 4 GB 可用磁盘空间；完整安装完成后项目目录目标不超过 3 GB；
- 首次安装 Python 依赖时可以联网。

#### 指定 Python 3.12

```powershell
.\setup.ps1 -Python "C:\Path\To\Python312\python.exe"
```

没有 Python 时可先安装：

```powershell
winget install Python.Python.3.12
```

## 快速开始

### 1. 准备文件

1. 下载并解压源码包。
2. 在 Releases 下载 **ocr**、**models**、**llama** 三个压缩包。
3. 把三个压缩包都解压到项目的 `runtime\` 目录。压缩包内第一层应分别为 `ocr\`、`models\`、`llama\`；如果提示合并或替换同名文件，请允许替换。

最终目录应为：

```text
项目目录\
  run.py
  翻译.exe
  app\
  runtime\
    models\
      HY-MT1.5-1.8B-Q4_K_M.gguf
    llama\
      llama-server.exe
      *.dll
    ocr\
      manifest.json
      det.onnx
      rec.onnx
      characters.txt
```

### 2. 安装并检查

在项目根目录打开 PowerShell：

```powershell
# 仅在提示脚本未签名时执行；只影响当前 PowerShell 窗口：
Set-ExecutionPolicy -Scope Process Bypass -Force

.\setup.ps1
.\setup.ps1 -Check
```

`setup.ps1` 会创建 `venv`、安装依赖、检查依赖冲突、生成本机 `config.json`、自动选择 GPU 配置，并在缺少时生成 `翻译.exe`。

### 3. 启动

双击 `翻译.exe`，或在 PowerShell 执行：

```powershell
venv\Scripts\pythonw.exe run.py
```

### CPU 用户

默认“自动”模式检测不到 CUDA 时会直接使用 CPU，无需下载其它文件。也可以启动软件后进入“设置 → 高级 → 翻译设备”，选择“CPU”并重启软件。没有 DirectML 时 OCR 同样会自动回退 CPU；CPU 翻译通常更慢。

## 功能展示

| 截图翻译 | 选择窗口持续翻译目标 |
|:---------:|:--------------------:|
| ![截图翻译结果](./docs/images/screenshot-translation-result.png) | ![窗口持续翻译目标选择](./docs/images/window-picker.png) |

| 持续翻译字幕显示 | 区域实时翻译备注模式 |
|:----------------:|:--------------------:|
| ![持续翻译字幕显示](./docs/images/live-translation-overlay.png) | ![区域实时翻译备注模式](./docs/images/inline-annotation-mode.png) |

字体大小可在“设置 → 常规 / 窗口翻译 / 区域翻译”中分别调整，可选范围为 12–20 px；选择“默认”会保留软件原有字号，保存后立即生效。

## 切换翻译模型

需要切换模型时：

1. 自行下载兼容 `llama.cpp` 的 `.gguf` 翻译模型。
2. 把模型文件直接放入项目的 `runtime\models\`，不要再套一层目录。
3. 打开“托盘 → 设置 → 高级 → 模型与生成”，选择模型并保存。
4. 退出并重新启动软件，新模型才会生效。

每次打开设置都会重新扫描模型目录。列表只显示文件头有效的 `.gguf`；模型是否适合翻译、支持当前提示格式以及所需显存，由模型本身决定。

## 默认热键

| 功能 | 热键 |
|------|------|
| 截屏翻译 | Alt+Q |
| 划词翻译 | Alt+W |
| 窗口持续翻译 | Alt+E |
| 区域实时翻译 | Alt+R |

启动后会自动打开设置，并显示 OCR 与翻译模型的加载状态；失败项可直接重试。托盘菜单仍可打开设置、历史和日志。窗口持续翻译与区域实时翻译同时只能运行一个，控制条可原地暂停或继续。

翻译历史默认最多保存 50 条，以明文写入数据库 `data.db`（便携版位于软件根目录；安装版位于系统标准用户目录 `%LOCALAPPDATA%\LocalScreenTranslator\`）。可在“设置 → 常规”关闭后续记录；历史窗口支持搜索、复制、删除单条或确认后清空。设置、历史、翻译窗口以及字幕条会记住上次有效的位置和大小。

## 常见问题

| 现象 | 处理 |
|------|------|
| runtime 资源缺失 | 把 Releases 的 **ocr**、**models**、**llama** 三个压缩包解压到项目的 `runtime\` 目录 |
| DirectML 不可用 | OCR 会自动回退 CPU；可在 `config.json` 将 `ocr_provider` 固定为 `cpu` |
| GPU/llama 启动失败 | 在“设置 → 高级 → 翻译设备”选择 CPU，重启软件 |
| 截屏/录屏拍不到备注译文 | 默认排除以保证翻译速度；可在“设置 → 常规”开启“备注译文出现在系统截屏 / 录屏中”（区域备注会稍慢） |
| 安装后没有 `翻译.exe` | 运行 `.\setup.ps1 -BuildLauncher` 强制生成；也可直接运行 `venv\Scripts\pythonw.exe run.py` |
| 提示程序已在运行 | 检查系统托盘，程序只允许一个实例 |
| 想确认资源是否齐全 | 运行 `.\setup.ps1 -Check` |

数据与日志位置：便携版保存在软件根目录下的 `config.json`、`data.db` 与 `app.log`；安装包安装版自动安全存储在 `%LOCALAPPDATA%\LocalScreenTranslator\`（升级时会自动平滑迁移旧配置与历史）。详细设置说明见设置页面和 [SETTINGS.md](./SETTINGS.md)（英文版 [SETTINGS.en.md](./SETTINGS.en.md)）。

更详细的 runtime 目录说明见 [runtime/README.md](./runtime/README.md)。

## 独立安装包构建

若需要将软件（含 Python 运行环境与离线模型资源）打包为单文件安装包（`LocalScreenTranslator-Setup.exe`），在满足已安装 Inno Setup 6 与完整 `runtime\` 资源的前提下执行：

```powershell
.\package.ps1
```

脚本将自动执行 PATH 环境净化、PyInstaller onedir 编译、QtCore 依赖兼容性冒烟检测以及 Inno Setup 封装，生成安装包至 `dist\LocalScreenTranslator-Setup.exe`。

## 其它

模型等第三方许可见 [NOTICE](./NOTICE)。

**本安装说明可能由 AI 生成，请自行核对路径与 Release 文件名后再操作。**
