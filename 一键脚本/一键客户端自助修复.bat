@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem Client self-repair (REPAIR_INTERACTIVE_V1): checks first, then asks before
rem changing anything. Keep every cmd-parsed line ASCII-only; Chinese text must
rem go through powershell Write-Host (see portable-windows-sync.bat notes).
set "REPAIR_PS="
if exist "%~dp0_updater\player-self-repair.ps1" set "REPAIR_PS=%~dp0_updater\player-self-repair.ps1"
if not defined REPAIR_PS if exist "%~dp0player-self-repair.ps1" set "REPAIR_PS=%~dp0player-self-repair.ps1"
if not defined REPAIR_PS if exist "%~dp0..\tools\player-self-repair.ps1" set "REPAIR_PS=%~dp0..\tools\player-self-repair.ps1"
if not defined REPAIR_PS goto :Missing
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PS%" -InstanceDir "%~dp0." -Interactive -NoPause
set "code=%ERRORLEVEL%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Write-Host ''; Write-Host '需要帮助时，把 _repair\latest\report.txt 发给管理员。按回车键关闭窗口...'"
pause >nul
exit /b %code%
:Missing
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Write-Host '[错误] 找不到 _updater\player-self-repair.ps1。请先双击「更新mod-Windows端.bat」完整同步一次。按回车键关闭窗口...'"
pause >nul
exit /b 1
