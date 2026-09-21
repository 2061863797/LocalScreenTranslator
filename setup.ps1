#Requires -Version 5.1
<#
.SYNOPSIS
  本地屏译（LocalScreen Translator）一键环境安装 / 体检 / 按本机生成配置

.DESCRIPTION
  在项目根执行：
    .\setup.ps1                 # 完整安装
    .\setup.ps1 -Check          # 只检查
    .\setup.ps1 -CpuOnly        # 强制 CPU
    .\setup.ps1 -Gpu            # 强制 llama 使用 GPU
    .\setup.ps1 -BuildLauncher    # 强制重建 翻译.exe（普通安装缺少时会自动生成）
#>

param(
    [switch]$Check,
    [switch]$CpuOnly,
    [switch]$Gpu,
    [switch]$BuildLauncher,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Write-Step([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg){ Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Write-Err2([string]$msg) { Write-Host "  [X] $msg" -ForegroundColor Red }

function Install-PipWithRetry {
    param(
        [string]$VenvPy,
        [string[]]$Arguments,
        [string]$FailureMessage
    )
    & $VenvPy -m pip install @Arguments
    if ($LASTEXITCODE -eq 0) { return }

    # pip 的本机缓存偶尔会损坏或与索引哈希不一致；不删除用户全局缓存，
    # 仅为本次安装绕过缓存重新下载。
    Write-Warn2 "pip 首次安装失败，禁用缓存后重试"
    & $VenvPy -m pip install --no-cache-dir @Arguments
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

function Test-NvidiaGpu {
    try {
        $cmd = Get-Command nvidia-smi -ErrorAction SilentlyContinue
        if (-not $cmd) { return $false }
        $out = & nvidia-smi -L 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        if ($null -eq $out) { return $false }
        return ($out -match "GPU")
    } catch {
        return $false
    }
}

function Get-CpuThreadHint {
    try {
        $n = [Environment]::ProcessorCount
        if ($n -lt 2) { return 2 }
        if ($n -gt 16) { return 16 }
        return [int]$n
    } catch {
        return 8
    }
}

function Test-SupportedPython {
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        & $Exe -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13)) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    param([string]$Prefer)
    if ($Prefer -and (Test-Path -LiteralPath $Prefer)) {
        $resolved = (Resolve-Path -LiteralPath $Prefer).Path
        if (Test-SupportedPython $resolved) { return $resolved }
        Write-Warn2 "指定的 Python 版本不受支持（仅 3.11～3.13）: $resolved"
        return $null
    }
    foreach ($ver in @("3.12", "3.13", "3.11")) {
        try {
            $p = & py "-$ver" -c "import sys; print(sys.executable)" 2>$null
            if ($p) {
                $p = $p.ToString().Trim()
                if ((Test-Path -LiteralPath $p) -and (Test-SupportedPython $p)) { return $p }
            }
        } catch {}
    }
    try {
        $p = & python -c "import sys; print(sys.executable)" 2>$null
        if ($p) {
            $p = $p.ToString().Trim()
            if ((Test-Path -LiteralPath $p) -and (Test-SupportedPython $p)) { return $p }
        }
    } catch {}
    return $null
}

function Ensure-Venv {
    param([string]$BasePython)
    $venvPy = Join-Path $Root "venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPy) {
        Write-Ok "已有 venv: $venvPy"
        return $venvPy
    }
    Write-Step "创建 venv"
    Write-Host "  base: $BasePython"
    & $BasePython -m venv (Join-Path $Root "venv")
    if (-not (Test-Path -LiteralPath $venvPy)) {
        throw "venv 创建失败"
    }
    Write-Ok "venv 已创建"
    return $venvPy
}

function Install-PipPackages {
    param([string]$VenvPy)
    Write-Step "升级 pip 并安装 requirements.txt"
    Install-PipWithRetry -VenvPy $VenvPy `
        -Arguments @("-U", "pip", "setuptools", "wheel") `
        -FailureMessage "pip 升级失败"
    Install-PipWithRetry -VenvPy $VenvPy `
        -Arguments @("-r", (Join-Path $Root "requirements.txt")) `
        -FailureMessage "requirements 安装失败"
    Write-Ok "基础依赖已安装"
}

function Test-PythonDependencies {
    param([string]$VenvPy)
    Write-Step "检查 Python 依赖一致性"
    & $VenvPy -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Python 依赖存在冲突，请查看上方 pip check 输出"
    }
    Write-Ok "Python 依赖无冲突"
}

function Ensure-Config {
    param(
        [bool]$UseGpu,
        [int]$Threads,
        [string]$DeviceOverride = ""
    )
    Write-Step "生成 / 更新 config.json"
    $ngl = 0
    if ($UseGpu) { $ngl = 99 }

    $py = Join-Path $Root "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $py)) {
        $py = Find-Python -Prefer $Python
    }
    if (-not $py) { throw "无 Python，无法写 config" }

    $script = Join-Path $Root "scripts\setup_config.py"
    $flag = "0"
    if ($UseGpu) { $flag = "1" }
    & $py $script $Root $flag $Threads $ngl $DeviceOverride
    if ($LASTEXITCODE -ne 0) { throw "写 config 失败" }
    Write-Ok "config.json 已就绪 (n_gpu_layers=$ngl, threads=$Threads)"
}

function Test-Runtime {
    Write-Step "检查 runtime 资源"
    $ok = $true
    $llama = Join-Path $Root "runtime\llama\llama-server.exe"
    $defaultModel = Join-Path $Root "runtime\models\HY-MT1.5-1.8B-Q4_K_M.gguf"
    $model = $defaultModel
    $configFile = Join-Path $Root "config.json"
    if (Test-Path -LiteralPath $configFile) {
        try {
            # Windows PowerShell 5.1 对无 BOM UTF-8 的默认解码不可靠，必须显式指定。
            $configData = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $configuredModel = $configData.model_path
            if ($configuredModel -is [string] -and -not [string]::IsNullOrWhiteSpace($configuredModel)) {
                if ([System.IO.Path]::IsPathRooted($configuredModel)) {
                    $model = [System.IO.Path]::GetFullPath($configuredModel)
                } else {
                    $model = [System.IO.Path]::GetFullPath((Join-Path $Root $configuredModel))
                }
            }
        } catch {
            Write-Warn2 "config.json 的 model_path 无法读取，改为检查默认模型"
            $model = $defaultModel
        }
    }
    $isDefaultModel = [string]::Equals(
        [System.IO.Path]::GetFullPath($model),
        [System.IO.Path]::GetFullPath($defaultModel),
        [System.StringComparison]::OrdinalIgnoreCase
    )
    $ocr = Join-Path $Root "runtime\ocr"

    if (Test-Path -LiteralPath $llama) {
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $llama --version 1>$null 2>$null
        $llamaCode = $LASTEXITCODE
        $ErrorActionPreference = $oldEap
        if ($llamaCode -eq 0) {
            Write-Ok "llama-server: $llama"
        } else {
            Write-Err2 "llama-server 无法运行或依赖 DLL 不完整: $llama"
            $ok = $false
        }
    } else {
        Write-Err2 "缺少 $llama"
        $ok = $false
    }

    if (Test-Path -LiteralPath $model) {
        $mb = [math]::Round((Get-Item -LiteralPath $model).Length / 1MB, 1)
        $fs = [System.IO.File]::OpenRead($model)
        try {
            $magicBytes = New-Object byte[] 4
            [void]$fs.Read($magicBytes, 0, 4)
            $magic = [System.Text.Encoding]::ASCII.GetString($magicBytes)
        } finally { $fs.Dispose() }
        if ($magic -ne "GGUF" -or $mb -le 0) {
            Write-Err2 "翻译模型文件不完整或不是 GGUF: $model"
            $ok = $false
        } elseif ($isDefaultModel -and $mb -gt 100) {
            $expectedModelSha = "4383ac0c3c8e476de98ff979c2a3f069f8c4fb385e7860cf2d28da896cc477c7"
            $actualModelSha = (Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actualModelSha -eq $expectedModelSha) {
                Write-Ok "翻译模型: $model ($mb MB，SHA256 正确)"
            } else {
                Write-Err2 "翻译模型 SHA256 不匹配: $model"
                $ok = $false
            }
        } elseif ($isDefaultModel) {

            Write-Err2 "默认翻译模型文件不完整: $model"
            $ok = $false
        } else {
            Write-Ok "自定义翻译模型: $model ($mb MB，GGUF 文件头正确)"
        }
    } else {
        $otherModels = @(Get-ChildItem -Path (Join-Path $Root "runtime\models") -Filter "*.gguf" -ErrorAction SilentlyContinue)
        if ($otherModels.Count -gt 0) {
            Write-Ok "检测到可用翻译模型: $($otherModels[0].FullName)"
        } else {
            Write-Warn2 "未检测到预置翻译模型: $model"
            Write-Warn2 "提示: 本软件支持免模型安装。后续可从 Release 下载 models.zip 或在软件设置窗口一键导入 GGUF 模型。"
        }
    }

    $ocrRequired = @("manifest.json", "det.onnx", "rec.onnx", "characters.txt")
    $ocrReady = $true
    foreach ($name in $ocrRequired) {
        $file = Join-Path $ocr $name
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            $ocrReady = $false
        }
    }
    if ($ocrReady) {
        try {
            $manifest = Get-Content -LiteralPath (Join-Path $ocr "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($name in @("det.onnx", "rec.onnx", "characters.txt")) {
                $expected = $manifest.files.$name.sha256
                $actual = (Get-FileHash -LiteralPath (Join-Path $ocr $name) -Algorithm SHA256).Hash.ToLowerInvariant()
                if (-not $expected -or $actual -ne $expected.ToLowerInvariant()) { $ocrReady = $false }
            }
        } catch { $ocrReady = $false }
    }
    if ($ocrReady) {
        Write-Ok "OCR 模型目录: $ocr"
    } else {
        Write-Err2 "缺少 $ocr"
        $ok = $false
    }

    if (-not $ok) {
        Write-Warn2 "runtime 不齐：请把 Releases 的 ocr、models、llama 三个压缩包解压到 runtime 目录。"
    }
    return $ok
}

function Test-Imports {
    param([string]$VenvPy)
    Write-Step "冒烟导入"
    if (-not (Test-Path -LiteralPath $VenvPy)) {
        throw "缺少 venv，无法检查 Python 依赖"
    }
    $script = Join-Path $Root "scripts\smoke_import.py"
    & $VenvPy $script
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "app 导入成功"
    } else {
        throw "真实依赖或 app.main 导入失败"
    }
}

function Invoke-BuildLauncher {
    param([string]$VenvPy)
    Write-Step "生成 翻译.exe"
    & $VenvPy -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Install-PipWithRetry `
            -VenvPy $VenvPy `
            -Arguments @("pyinstaller") `
            -FailureMessage "PyInstaller 安装失败"
    }
    $build = Join-Path $Root "build.ps1"
    if (Test-Path -LiteralPath $build) {
        & $build
    } else {
        & $VenvPy -m PyInstaller --noconfirm --onefile --windowed `
            --name "翻译" `
            --icon (Join-Path $Root "icon.ico") `
            --distpath $Root `
            --workpath (Join-Path $Root "build") `
            --specpath $Root `
            (Join-Path $Root "launcher.py")
        if ($LASTEXITCODE -ne 0) {
            throw "翻译.exe 生成失败"
        }
    }
    $launcherExe = Join-Path $Root "翻译.exe"
    if (-not (Test-Path -LiteralPath $launcherExe)) {
        throw "翻译.exe 生成结束后仍未找到：$launcherExe"
    }
    Write-Ok "启动器已生成: $launcherExe"
}

# ---------------- main ----------------
Write-Host "本地屏译（LocalScreen Translator）一键安装 / 体检" -ForegroundColor White
Write-Host "目录: $Root"

$hasNvidia = Test-NvidiaGpu
if ($CpuOnly) {
    $useGpu = $false
    $deviceOverride = "cpu"
    Write-Host "模式: 强制 CPU (-CpuOnly)"
} elseif ($Gpu) {
    $useGpu = $true
    $deviceOverride = "gpu"
    Write-Host "模式: 强制 GPU (-Gpu)"
    if (-not $hasNvidia) {
        Write-Warn2 "未检测到 nvidia-smi，仍强制使用 GPU 配置（启动可能失败）"
    }
} else {
    $useGpu = $hasNvidia
    $deviceOverride = ""
    if ($useGpu) {
        Write-Host "模式: 自动（检测到 NVIDIA → GPU）"
    } else {
        Write-Host "模式: 自动（无 NVIDIA → CPU）"
    }
}

$threads = Get-CpuThreadHint
$nglShow = 0
if ($useGpu) { $nglShow = 99 }
Write-Host "threads=$threads  n_gpu_layers=$nglShow"

$runtimeOk = Test-Runtime

if ($Check) {
    Write-Step "仅检查模式 (-Check)"
    $checkOk = $runtimeOk
    $venvPy = Join-Path $Root "venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPy) {
        if (-not (Test-SupportedPython $venvPy)) {
            throw "venv Python 版本不受支持（仅 3.11～3.13）"
        }
        Test-PythonDependencies -VenvPy $venvPy
        Test-Imports -VenvPy $venvPy
    } else {
        Write-Err2 "尚未创建 venv，完整安装请运行: .\setup.ps1"
        $checkOk = $false
    }
    $launcherExe = Join-Path $Root "翻译.exe"
    if (Test-Path -LiteralPath $launcherExe) {
        Write-Ok "启动器: $launcherExe"
    } else {
        Write-Warn2 "缺少 翻译.exe；可运行 .\setup.ps1 -BuildLauncher 生成，或直接用 pythonw 启动"
    }
    Write-Host ""
    if ($checkOk) {
        Write-Ok "体检结束"
        exit 0
    }
    Write-Warn2 "体检结束（运行环境或 runtime 有缺失）"
    exit 2
}

$basePy = Find-Python -Prefer $Python
if (-not $basePy) {
    Write-Err2 "未找到 Python 3.11/3.12/3.13"
    Write-Host "  安装: winget install Python.Python.3.12"
    Write-Host "  或:   https://www.python.org/downloads/"
    exit 1
}
Write-Ok "基础 Python: $basePy"

$venvPy = Ensure-Venv -BasePython $basePy
if (-not (Test-SupportedPython $venvPy)) {
    throw "现有 venv 不是受支持的 Python 3.11～3.13；请删除 venv 后重新运行 setup.ps1"
}
Install-PipPackages -VenvPy $venvPy
Test-PythonDependencies -VenvPy $venvPy
Ensure-Config -UseGpu $useGpu -Threads $threads -DeviceOverride $deviceOverride
Test-Imports -VenvPy $venvPy

$launcherExe = Join-Path $Root "翻译.exe"
if ($BuildLauncher -or -not (Test-Path -LiteralPath $launcherExe)) {
    Invoke-BuildLauncher -VenvPy $venvPy
} else {
    Write-Ok "启动器已存在: $launcherExe"
}

Write-Step "完成"
Write-Host ""
Write-Host '  启动: 双击 翻译.exe  或  venv\Scripts\pythonw.exe run.py'
Write-Host "  检查: .\setup.ps1 -Check"
Write-Host "  仅CPU: .\setup.ps1 -CpuOnly"
Write-Host '  说明: ocr/models/llama 使用 Release 三个压缩包；venv 需在每台电脑运行本脚本生成'
Write-Host ""

if (-not $runtimeOk) { exit 2 }
exit 0
