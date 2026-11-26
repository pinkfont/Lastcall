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
python -c "import secrets,re,pathlib; p=pathlib.Path('.env'); t=p.read_text(); t=t if re.search(r'^API_KEY=\S', t, re.M) else (re.sub(r'^API_KEY=.*$', 'API_KEY='+secrets.token_urlsafe(32), t, flags=re.M) if re.search(r'^API_KEY=', t, re.M) else t.rstrip()+'\nAPI_KEY='+secrets.token_urlsafe(32)+'\n'); p.write_text(t)"

echo Running tests...
python -m pytest -q

echo.
echo Setup done. Put your feed link in .env, then double-click due.bat.
pause
