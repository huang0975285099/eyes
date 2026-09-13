@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pwsh.exe >nul 2>nul
if errorlevel 1 (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
) else (
    pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
)
set "START_EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %START_EXIT_CODE%
