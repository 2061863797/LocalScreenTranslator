#Requires -Version 5.1
<#
.SYNOPSIS
  本地屏译（LocalScreen Translator）受许可约束的特定捆绑模型打包脚本
.DESCRIPTION
  调用 package.ps1 并启用 -IncludeModel 参数，将 runtime\models\HY-MT1.5-1.8B-Q4_K_M.gguf
  一同打包进安装包中（仅限符合其社区许可地域限制的分发渠道使用）。
#>
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
& (Join-Path $Root "package.ps1") -IncludeModel
