@echo off
rem Run the REST API + scheduler locally. Docs: http://127.0.0.1:8000/docs
cd /d "%~dp0"
if not exist .venv (
    echo Run setup.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python -m duetracker serve
pause
