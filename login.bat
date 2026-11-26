@echo off
rem Sign in to POLITEMall once so submitted assignments are ticked off automatically.
cd /d "%~dp0"
if not exist .venv (
    echo Run setup.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python -m duetracker login
echo.
pause
