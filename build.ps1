# 重建启动器 翻译.exe（需已存在 venv + PyInstaller）
# 用法：在项目根执行  .\build.ps1
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "未找到 venv\Scripts\python.exe，请先按 README 创建 venv 并安装依赖。"
}

Write-Host "==> 检查 PyInstaller"
& $py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "安装 PyInstaller..."
    & $py -m pip install "pyinstaller==6.19.0"
}

$icon = Join-Path $Root "icon.ico"
$launcher = Join-Path $Root "launcher.py"
if (-not (Test-Path $launcher)) {
    Write-Error "缺少 launcher.py"
}

Write-Host "==> PyInstaller 打包启动器"
& $py -m PyInstaller --noconfirm --onefile --windowed `
    --name "翻译" `
    --icon $icon `
    --distpath $Root `
    --workpath (Join-Path $Root "build") `
    --specpath $Root `
    $launcher

if ($LASTEXITCODE -ne 0) {
    Write-Error "打包失败"
}

Write-Host "完成: $(Join-Path $Root '翻译.exe')"
Write-Host "说明: exe 仅为启动器，仍需同目录的 venv/、app/、run.py。"
