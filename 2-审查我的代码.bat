@echo off
setlocal
title Code Review Agent - Review My Code
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
set PYTHONUNBUFFERED=1

call "%~dp0setup.bat"
if errorlevel 1 exit /b

".venv\Scripts\python.exe" -m codereview review --interactive --quiet

pause
