@echo off
rem Double-click to fetch the latest from POLITEMall and show the next 14 days.
cd /d "%~dp0"
if not exist .venv (
    echo Run setup.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python -m duetracker sync
echo.
python -m duetracker due --days 14
echo.
pause
