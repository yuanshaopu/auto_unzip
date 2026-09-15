# 在 PowerShell 中运行 auto_unzip。
# 用法: 在 PowerShell 里执行  .\在PowerShell中运行.ps1
# 或: powershell -ExecutionPolicy Bypass -File .\在PowerShell中运行.ps1
#
# 该脚本先把控制台解码切到 UTF-8, 再调用同目录下的 auto_unzip.exe,
# 目标目录 = 本脚本所在目录。
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $here "auto_unzip.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Host "未找到 auto_unzip.exe, 请与脚本放在同一目录。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

& $exe $here
Write-Host ""
Write-Host "--- 处理完成, 8 秒后自动关闭 ---"
Start-Sleep -Seconds 8
