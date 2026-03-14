@echo off
setlocal enabledelayedexpansion
title VORTEX Video Downloader

echo.
echo  =========================================
echo   VORTEX Video Downloader
echo  =========================================
echo.

:: ---- Find Python ---------------------------------------------------------
set PYTHON=
for %%P in (python python3) do (
    if "!PYTHON!"=="" (
        %%P --version >nul 2>&1
        if not errorlevel 1 set PYTHON=%%P
    )
)

if "!PYTHON!"=="" (
    echo [ERROR] Python not found.
    echo.
    echo  Please install Python 3.8+ from:
    echo  https://www.python.org/downloads/
    echo.
    echo  IMPORTANT: Check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo [OK] Found: !PYTHON!
!PYTHON! --version

:: ---- Check ffmpeg --------------------------------------------------------
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARN] ffmpeg not found.
    echo  Some formats ^(MP4 merge, audio extraction^) may not work.
    echo  Install from: https://ffmpeg.org/download.html
    echo  Then add ffmpeg\bin to your system PATH.
    echo.
)

:: ---- Create virtualenv ---------------------------------------------------
if not exist ".venv" (
    echo [..] Creating virtual environment...
    !PYTHON! -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv. Trying global pip install...
        goto :global_install
    )
)

:: ---- Activate venv -------------------------------------------------------
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
    set PYTHON=python
) else (
    echo [WARN] venv activation failed, using global Python.
)

:: ---- Install deps --------------------------------------------------------
:install_deps
echo [..] Checking dependencies...
!PYTHON! -c "import flask, flask_cors, yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo [..] Installing / updating dependencies...
    !PYTHON! -m pip install --upgrade pip -q
    !PYTHON! -m pip install -r requirements.txt -q
    if errorlevel 1 (
        echo [ERROR] pip install failed. Check your internet connection or firewall.
        pause
        exit /b 1
    )
) else (
    echo [OK] Dependencies already installed.
)
goto :run

:global_install
echo [..] Checking global dependencies...
!PYTHON! -c "import flask, flask_cors, yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo [..] Installing globally...
    !PYTHON! -m pip install -r requirements.txt -q
    if errorlevel 1 (
        echo [ERROR] pip install failed.
        pause
        exit /b 1
    )
) else (
    echo [OK] Dependencies already installed.
)

:: ---- Launch server -------------------------------------------------------
:run
echo.
echo [OK] Starting VORTEX...
echo      Open your browser to: http://localhost:5000
echo      Press Ctrl+C to stop.
echo.
!PYTHON! app.py

echo.
echo  Server stopped.
pause
