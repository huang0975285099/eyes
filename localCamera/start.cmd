@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating a local Python environment...
  python -m venv .venv
  if errorlevel 1 goto :failed
)

echo [2/3] Checking dependencies...
".venv\Scripts\python.exe" -c "import cv2, numpy" >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies for the first run...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :failed
)

echo [3/3] Starting USB camera monitor...
".venv\Scripts\python.exe" app.py
goto :eof

:failed
echo.
echo Startup failed. Check that Python is installed and the network is available.
pause
exit /b 1
