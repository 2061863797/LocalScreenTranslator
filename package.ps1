#Requires -Version 5.1
<#
.SYNOPSIS
  本地屏译（LocalScreen Translator）一键打包构建与完整性验证脚本

.DESCRIPTION
  执行步骤：
    1. 检查 Python venv、Inno Setup (ISCC.exe) 与 runtime 完整性；
    2. 隔离并净化环境变量 PATH，彻底避免外部冲突 DLL（如 poppler 的 icuuc.dll）污染打包产物；
    3. 运行 PyInstaller 并应用 package.spec 的二进制过滤规则；
    4. 对打包后的 LocalScreenTranslator.exe 进行直接冒烟检查（验证 QtCore 正常加载）；
    5. 调用 Inno Setup 编译生成离线安装包 dist\LocalScreenTranslator-Setup.exe；
    6. 输出安装包哈希值与体积信息。
#>

param(
    [switch]$IncludeModel
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Write-Step([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg){ Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Write-Err2([string]$msg) { Write-Host "  [X] $msg" -ForegroundColor Red }

Write-Host "==========================================================" -ForegroundColor Green
Write-Host " 本地屏译 (LocalScreen Translator) 独立安装包构建工具" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green

# 1. 检查 Python venv
Write-Step "1. 检查 Python 运行环境"
$py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    Write-Err2 "未找到 venv\Scripts\python.exe，请先运行 .\setup.ps1 初始化环境。"
    exit 1
}
Write-Ok "Python: $py"

# 检查 PyInstaller
& $py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Step "安装 PyInstaller"
    & $py -m pip install "pyinstaller>=6.4.0,<7.0.0"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败" }
}
Write-Ok "PyInstaller 已就绪"

# 2. 检查 Inno Setup ISCC.exe
Write-Step "2. 查找 Inno Setup 编译器"
$isccCandidates = @(
    "C:\Users\$env:USERNAME\AppData\Local\Programs\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$iscc = $null
foreach ($cand in $isccCandidates) {
    if (Test-Path -LiteralPath $cand) {
        $iscc = $cand
        break
    }
}
if (-not $iscc) {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}
if (-not $iscc) {
    Write-Err2 "未找到 Inno Setup 6 (ISCC.exe)！请确认已安装 Inno Setup。"
    exit 1
}
Write-Ok "ISCC: $iscc"

# 3. 检查 runtime 离线资源
Write-Step "3. 检查离线模型与 runtime 资源"
$llamaExe = Join-Path $Root "runtime\llama\llama-server.exe"
$modelGguf = Join-Path $Root "runtime\models\HY-MT1.5-1.8B-Q4_K_M.gguf"
$ocrDir = Join-Path $Root "runtime\ocr"

if (-not (Test-Path -LiteralPath $llamaExe)) {
    Write-Err2 "缺少 $llamaExe"
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $ocrDir "det.onnx")) -or -not (Test-Path -LiteralPath (Join-Path $ocrDir "rec.onnx"))) {
    Write-Err2 "缺少 OCR ONNX 模型"
    exit 1
}
if ($IncludeModel) {
    if (-not (Test-Path -LiteralPath $modelGguf)) {
        Write-Err2 "已指定 -IncludeModel，但在 $modelGguf 未找到模型文件！"
        exit 1
    }
    Write-Ok "已检测到捆绑模型文件: $modelGguf"
} else {
    Write-Ok "按默认 Model-Free 模式打包（不包含模型文件，符合开源分发与合规要求）"
}
Write-Ok "runtime 基础引擎资源完整"

# 4. PATH 环境净化（核心安全防护）
Write-Step "4. 净化 PATH 环境变量（过滤第三方污染源）"
$originalPath = $env:PATH
$pathSegments = $originalPath -split ';'
$cleanSegments = @()
foreach ($seg in $pathSegments) {
    if ([string]::IsNullOrWhiteSpace($seg)) { continue }
    $segLower = $seg.ToLowerInvariant()
    # 彻底过滤含有 codex-runtimes、poppler 等私有或冲突的目录
    if ($segLower -match "codex-runtimes|poppler") {
        Write-Warn2 "排除污染 PATH: $seg"
        continue
    }
    $cleanSegments += $seg
}
$cleanPath = $cleanSegments -join ';'

# 5. 编译 PyInstaller 主程序
Write-Step "5. 执行 PyInstaller 编译 (onedir 模式)"
$specFile = Join-Path $Root "package.spec"
if (-not (Test-Path -LiteralPath $specFile)) {
    Write-Err2 "未找到 $specFile"
    exit 1
}

# 清理旧的编译中间产物
$distAppDir = Join-Path $Root "dist\LocalScreenTranslator"
if (Test-Path -LiteralPath $distAppDir) {
    Write-Host "  清理旧的 dist\LocalScreenTranslator..."
    Remove-Item -LiteralPath $distAppDir -Recurse -Force
}

$buildDir = Join-Path $Root "build\LocalScreenTranslator"
if (Test-Path -LiteralPath $buildDir) {
    Write-Host "  清理旧的 build\LocalScreenTranslator..."
    Remove-Item -LiteralPath $buildDir -Recurse -Force
}

try {
    $env:PATH = $cleanPath
    & $py -m PyInstaller --noconfirm $specFile
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }
} finally {
    $env:PATH = $originalPath
}
Write-Ok "PyInstaller 编译完成"

# 6. 打包产物安全检查与冒烟验证
Write-Step "6. 产物完整性与 DLL 兼容性冒烟检测"
$appExe = Join-Path $distAppDir "LocalScreenTranslator.exe"
if (-not (Test-Path -LiteralPath $appExe)) {
    Write-Err2 "未找到编译产物: $appExe"
    exit 1
}

# 检查是否存在被污染的 ICU 库
$pollutedDlls = Get-ChildItem -Path $distAppDir -Recurse -Filter "icuuc*.dll" -ErrorAction SilentlyContinue
if ($pollutedDlls) {
    Write-Err2 "发现被污染的 icuuc 动态库残留: $($pollutedDlls.FullName)"
    exit 1
}
Write-Ok "无冲突 ICU 库残留"

# 运行冒烟测试
Write-Host "  启动冒烟检查 (LocalScreenTranslator.exe --smoke-check)..."
$smokeProc = Start-Process -FilePath $appExe -ArgumentList "--smoke-check" -Wait -PassThru
$smokeExitCode = $smokeProc.ExitCode

if ($smokeExitCode -ne 0) {
    Write-Err2 "冒烟检查失败！退出码: $smokeExitCode"
    exit 1
}
Write-Ok "冒烟检查成功通过 (QtCore 与核心依赖正常加载)"

# 7. Inno Setup 编译安装包
Write-Step "7. 调用 Inno Setup 制作最终安装包"
$issFile = Join-Path $Root "installer.iss"
if (-not (Test-Path -LiteralPath $issFile)) {
    Write-Err2 "未找到 $issFile"
    exit 1
}

if ($IncludeModel) {
    & $iscc /Qp /DIncludeModels=1 $issFile
} else {
    & $iscc /Qp /DIncludeModels=0 $issFile
}
if ($LASTEXITCODE -ne 0) {
    Write-Err2 "Inno Setup 编译安装包失败！"
    exit 1
}

# 8. 校验最终安装包
Write-Step "8. 校验安装包产物"
$finalSetup = Join-Path $Root "dist\本地屏译-Setup.exe"
if (-not (Test-Path -LiteralPath $finalSetup)) {
    Write-Err2 "未生成最终安装包: $finalSetup"
    exit 1
}

$fileItem = Get-Item -LiteralPath $finalSetup
$mb = [math]::Round($fileItem.Length / 1MB, 2)
$hash = (Get-FileHash -LiteralPath $finalSetup -Algorithm SHA256).Hash

Write-Ok "安装包已成功生成！"
Write-Host "  路径: $finalSetup" -ForegroundColor White
Write-Host "  大小: $mb MB" -ForegroundColor White
Write-Host "  SHA256: $hash" -ForegroundColor Gray
Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " 打包全流程成功完成！" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
