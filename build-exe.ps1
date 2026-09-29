#Requires -Version 5.1
<# 本机开发：构建普通 onedir EXE；每次写入新的目录，不覆盖已有程序或模型。 #>

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"
$native = Join-Path $root "runtime\llama-native"
$ocr = Join-Path $root "runtime\ocr"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "缺少项目虚拟环境：$python；请先运行 .\setup.ps1"
}
& $python (Join-Path $root "scripts\verify_lock.py")
if ($LASTEXITCODE -ne 0) { throw "本地依赖与 requirements-lock.txt 不一致" }
foreach ($dll in @("llama.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu-x64.dll")) {
    if (-not (Test-Path -LiteralPath (Join-Path $native $dll) -PathType Leaf)) {
        throw "缺少进程内 llama.cpp v0.5.0 动态库：$dll"
    }
}
foreach ($name in @("manifest.json", "det.onnx", "rec.onnx", "characters.txt")) {
    if (-not (Test-Path -LiteralPath (Join-Path $ocr $name) -PathType Leaf)) {
        throw "缺少 PP-OCR runtime 文件：$name；请解压 GitHub Release 的 ocr.zip 到 runtime\ocr"
    }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$distRoot = Join-Path $root "dist\local-$stamp"
$buildRoot = Join-Path $root "build\local-$stamp"
if ((Test-Path -LiteralPath $distRoot) -or (Test-Path -LiteralPath $buildRoot)) {
    throw "本次构建目录已存在：$distRoot"
}

Push-Location $root
try {
    & $python -m PyInstaller --noconfirm --distpath $distRoot --workpath $buildRoot (Join-Path $root "package.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

    $output = Join-Path $distRoot "LocalScreenTranslator"
    $exe = Join-Path $output "LocalScreenTranslator.exe"
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "未生成 $exe" }

    $runtimeOutput = Join-Path $output "runtime"
    New-Item -ItemType Directory -Path $runtimeOutput -Force | Out-Null
    Copy-Item -LiteralPath $native -Destination $runtimeOutput -Recurse
    Copy-Item -LiteralPath $ocr -Destination $runtimeOutput -Recurse
    foreach ($name in @("LICENSE", "NOTICE")) {
        Copy-Item -LiteralPath (Join-Path $root $name) -Destination $output
    }

    foreach ($flag in @("--smoke-check", "--native-smoke-check", "--ocr-smoke-check")) {
        $process = Start-Process -FilePath $exe -ArgumentList $flag -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "exe 验证失败：$flag (退出码 $($process.ExitCode))" }
    }
    $localModel = Join-Path $root "runtime\models\HY-MT1.5-1.8B-Q4_K_M.gguf"
    if (Test-Path -LiteralPath $localModel -PathType Leaf) {
        $process = Start-Process -FilePath $exe -ArgumentList @("--ai-smoke-check", $localModel) -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "冻结 EXE 真实 GGUF 翻译失败（退出码 $($process.ExitCode)）" }
    }
    Write-Host "本机 EXE 已就绪：$exe"
    Write-Host "请在设置里导入兼容 llama.cpp 的 GGUF 模型。"
} finally {
    Pop-Location
}
