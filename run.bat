@echo off
setlocal
title Windows Fixer AI Launcher
cd /d "%~dp0"

:: Check if Python is installed and accessible in PATH
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo.
    echo ========================================================
    echo  ERROR: Python 3.8+ was not found on your system.
    echo  Please install Python from https://www.python.org/
    echo ========================================================
    echo.
    pause
    exit /b 1
)

:: Launch Windows Fixer AI
echo Starting Windows Fixer AI...
python windows_fixer.py %*
