@echo off
REM Double-click installer: registers the watcher scheduled task.
REM Registering a task needs elevation, so this file self-elevates via UAC.
REM NOTE: the task is registered for the user of the ELEVATED shell — accept the UAC
REM prompt under the node's own account, not under a different admin account.
setlocal
set "HERE=%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator rights...
  powershell.exe -NoProfile -Command "Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','%HERE%install-watch-task.ps1' -Verb RunAs"
  exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%install-watch-task.ps1" %*
echo.
pause
