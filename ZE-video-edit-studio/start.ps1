#!/usr/bin/env pwsh
# Windows 本机启动入口。具体端口、依赖、健康检查和浏览器打开由同一跨平台启动器管理。
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (Get-Command py -ErrorAction SilentlyContinue) {
  foreach ($selector in @("-3.13", "-3.12", "-3.11", "-3.10", "-3.9")) {
    $null = & py $selector -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) {
      & py $selector scripts/local_launcher.py
      exit $LASTEXITCODE
    }
  }
}
if (Get-Command python -ErrorAction SilentlyContinue) {
  & python scripts/local_launcher.py
  exit $LASTEXITCODE
}

Write-Error "[ZE][FATAL] 未找到 Python 3.9–3.13：https://www.python.org/downloads/"
exit 1
