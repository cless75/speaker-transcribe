@echo off
REM Double-click installer: registers the watcher scheduled task.
REM Runs install-watch-task.ps1 with ExecutionPolicy Bypass so no manual policy change is needed.
setlocal
set "HERE=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%install-watch-task.ps1" %*
echo.
pause
