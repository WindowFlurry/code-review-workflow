@echo off
setlocal
title Code Review Agent - Review My Code
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
set PYTHONUNBUFFERED=1

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo No Python environment found.
    echo Run these two commands in this folder first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    echo See README.md section 0 for details.
    echo.
    pause
    exit /b
)

".venv\Scripts\python.exe" -m codereview review --interactive --quiet

pause
