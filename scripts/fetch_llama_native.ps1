#Requires -Version 5.1
<# 仅供本机构建：下载固定的 llama.cpp v0.5.0 / b11146 DLL，不获取 server 或模型。 #>
param([string]$Destination = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Destination) { $Destination = Join-Path $root "runtime\llama-native" }
$target = [IO.Path]::GetFullPath($Destination)
if ((Test-Path -LiteralPath $target) -and @(Get-ChildItem -LiteralPath $target -Force).Count -gt 0) {
    throw "目标目录已有文件，拒绝覆盖：$target"
}
New-Item -ItemType Directory -Path $target -Force | Out-Null

$assets = @(
    @{Name="llama-b11146-bin-win-cpu-x64.zip"; Hash="14cf1303ca9ac3abd94816850532f9f9a69ac66fbaca3776fc6f9061c2fac1d1"},
    @{Name="llama-b11146-bin-win-cuda-12.4-x64.zip"; Hash="3c806a6ceccc3dae1c743ceb1a1fb2cce5b76f40bfbd4c6b7b8afb6ef45a5807"},
    @{Name="cudart-llama-bin-win-cuda-12.4-x64.zip"; Hash="8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6"}
)
$download = Join-Path ([IO.Path]::GetTempPath()) ("lst-llama-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $download | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($asset in $assets) {
    $name = $asset.Name
    $zipPath = Join-Path $download $name
    $url = "https://github.com/ggml-org/llama.cpp/releases/download/b11146/$name"
    Invoke-WebRequest -Uri $url -OutFile $zipPath
    $actual = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $asset.Hash) { throw "官方 DLL 压缩包 SHA256 不匹配：$name" }
    $zip = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        foreach ($entry in $zip.Entries) {
            $file = $entry.Name
            $needed = $file -in @("llama.dll", "ggml.dll", "ggml-base.dll", "libomp.dll", "ggml-cuda.dll", "cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll") -or $file -like "ggml-cpu-*.dll"
            if (-not $needed) { continue }
            if ($name -like '*cpu-x64.zip' -and $file -in @("ggml-cuda.dll", "cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll")) { continue }
            if ($name -like '*cuda-12.4-x64.zip' -and $name -notlike 'cudart-*' -and $file -ne "ggml-cuda.dll") { continue }
            $dest = Join-Path $target $file
            if (Test-Path -LiteralPath $dest) { continue }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest)
        }
    } finally { $zip.Dispose() }
    Remove-Item -LiteralPath $zipPath
}
Remove-Item -LiteralPath $download
Write-Host "llama.cpp v0.5.0 DLL 已准备：$target"
Write-Host "上游提交：7fe450e19305b828c199d602c23a8337aaa1f03b"
