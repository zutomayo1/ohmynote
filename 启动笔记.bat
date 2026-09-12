@echo off
setlocal
cd /d "%~dp0"
title InkNote Notes Server

rem --- Double-clicked (no arguments): if the server is already up, just open the browser ---
if not "%~1"=="" goto :run

netstat -ano | findstr /r /c:"TCP.*:8000 .*LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo.
    echo   InkNote is already running.
    echo   Opening http://127.0.0.1:8000/ ...
    echo.
    start "" http://127.0.0.1:8000/
    exit /b 0
)

:run
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   [ERROR] Python virtual environment not found.
    echo   Please run this once:
    echo.
    echo       pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================================
echo     InkNote  (Mo Hen)  -  personal notes and blog
echo.
echo     Starting... the browser will open by itself.
echo     KEEP THIS WINDOW OPEN while you use the app.
echo     Close this window (or press Ctrl+C) to stop the server.
echo   ============================================================
echo.

if "%~1"=="" (
    ".venv\Scripts\python.exe" run.py --open
) else (
    ".venv\Scripts\python.exe" run.py %*
)

echo.
echo   Server stopped. Press any key to close this window.
pause >nul
