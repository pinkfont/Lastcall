@echo off
rem Fetch the latest from POLITEMall, tick off submitted work, show the next 14 days.
cd /d "%~dp0app"
if not exist .venv\Scripts\python.exe (
    echo Run setup.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m duetracker sync
echo.
.venv\Scripts\python.exe -m duetracker due --days 14
echo.
pause
