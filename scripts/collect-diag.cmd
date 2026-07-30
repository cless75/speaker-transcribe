@echo off
REM Double-click collector: gathers node state + diagnostics into one file
REM (repo\logs and the Hub _meta), so it can be read from another machine.
REM Read-only: nothing on the node is changed.
setlocal
set "HERE=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%collect-diag.ps1" %*
echo.
pause
