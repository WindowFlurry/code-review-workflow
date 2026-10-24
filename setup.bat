@echo off
rem Shared first-run setup, called by the numbered .bat files.
rem Pure ASCII on purpose: cmd reads batch files in the console code page and
rem GitHub renders files as UTF-8. Chinese here would be garbled in the browser
rem and, after a code-page switch, would break cmd's line parsing.

if exist ".venv\Scripts\python.exe" exit /b 0

echo.
echo ============================================================
echo   First run: setting up the Python environment.
echo   Downloads dependencies; takes about 1-3 minutes.
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON

echo Creating .venv ...
python -m venv .venv
if not exist ".venv\Scripts\python.exe" goto VENV_FAILED

echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto PIP_FAILED

if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul

echo.
echo Environment ready.
echo.
exit /b 0

:NO_PYTHON
echo Python was not found on PATH.
echo.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo During installation, tick "Add Python to PATH", then run this file again.
echo.
pause
exit /b 1

:VENV_FAILED
echo.
echo Could not create .venv.
echo If "python" opens the Microsoft Store, install Python from python.org
echo instead (tick "Add Python to PATH"), then run this file again.
echo.
pause
exit /b 1

:PIP_FAILED
echo.
echo Dependency installation failed.
echo Check the network connection, then run this file again.
echo.
pause
exit /b 1
