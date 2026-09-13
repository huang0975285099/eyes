@echo off
chcp 65001 >nul
cd /d "%~dp0"
pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
set "STOP_EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %STOP_EXIT_CODE%
