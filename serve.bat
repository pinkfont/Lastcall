@echo off
rem Run the API, scheduler and Telegram bot. Keep this window open.
rem API docs: http://127.0.0.1:8000/docs
cd /d "%~dp0app"
if not exist .venv\Scripts\python.exe (
    echo Run setup.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m duetracker serve
pause
