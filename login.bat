@echo off
rem Sign in to POLITEMall so submitted assignments are ticked off automatically.
cd /d "%~dp0app"
if not exist .venv\Scripts\python.exe (
    echo Run setup.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m duetracker login
echo.
pause
