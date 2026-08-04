@echo off
REM start.bat — Windows 双击启动 ZE 工具（本机产品模式）。
REM   转调 start.ps1，并绕过 PowerShell 默认执行策略限制（不改系统策略，仅本进程）。
REM   指定端口:  set PORT=8123 && start.bat
REM   正式部署仍走受管部署入口；本脚本不会调用生产 start.sh。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 (
  echo.
  echo [ZE] 启动失败，请查看上方的具体错误。
)
pause
