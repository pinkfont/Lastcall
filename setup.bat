@echo off
rem One-time setup on Windows: double-click this file.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add Python to PATH", then run this again.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo Installing libraries...
python -m pip install -q --disable-pip-version-check -r requirements.txt

if not exist .env copy .env.example .env >nul

echo Running tests...
python -m pytest -q

echo.
echo Setup done. Put your feed link in .env, then double-click due.bat.
pause
