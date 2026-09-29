#Requires -Version 5.1
<# 本地开发构建入口；仅生成普通 onedir EXE，不运行安装器。 #>
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "build-exe.ps1")
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    throw "本地 EXE 构建失败：$LASTEXITCODE"
}
