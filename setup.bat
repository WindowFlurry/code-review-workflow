@echo off
rem Shared first-run setup, called by the numbered .bat files.
rem Pure ASCII on purpose: cmd reads batch files in the console code page and
rem GitHub renders files as UTF-8. Chinese here would be garbled in the browser
rem and, after a code-page switch, would break cmd's line parsing.

if exist ".venv\Scripts\python.exe" exit /b 0

echo.
echo ============================================================
echo   First run: setting up the Python environment.
echo   Downloads about 40 MB of dependencies; takes a few minutes.
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON

echo Creating .venv ...
python -m venv .venv
if not exist ".venv\Scripts\python.exe" goto VENV_FAILED

set "PIP=.venv\Scripts\python.exe"

rem Use a PyPI mirror by default: downloading from pypi.org can take many
rem minutes in mainland China. This is only a default - if PIP_INDEX_URL is
rem already set it is respected, and if the mirror fails the official index
rem is tried instead.
if not defined PIP_INDEX_URL set "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"

echo Installing dependencies ...
"%PIP%" -m pip install --upgrade pip --quiet
"%PIP%" -m pip install --prefer-binary -r requirements.txt
if errorlevel 1 (
    echo.
    echo The mirror did not work, retrying with the official PyPI ...
    set "PIP_INDEX_URL=https://pypi.org/simple"
    "%PIP%" -m pip install --prefer-binary -r requirements.txt
)
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
