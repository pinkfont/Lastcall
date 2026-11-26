@echo off
rem One-time setup (safe to run again any time). Double-click this file.
cd /d "%~dp0"
set "PY=app\.venv\Scripts\python.exe"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add Python to PATH", then run this again.
    pause
    exit /b 1
)

rem Recreate the Python environment if it's missing or broken.
if exist "%PY%" (
    "%PY%" -c "import sys" >nul 2>nul
    if errorlevel 1 rmdir /s /q app\.venv
)
if not exist "%PY%" (
    echo Creating Python environment...
    python -m venv app\.venv
)

echo Installing libraries...
"%PY%" -m pip install -q --disable-pip-version-check -r app\requirements.txt

if not exist config\.env copy config\.env.example config\.env >nul
rem Generate an API key in config\.env if there isn't one yet.
"%PY%" -c "import secrets,re,pathlib; p=pathlib.Path('config/.env'); t=p.read_text(); t=t if re.search(r'^API_KEY=\S', t, re.M) else (re.sub(r'^API_KEY=.*$', 'API_KEY='+secrets.token_urlsafe(32), t, flags=re.M) if re.search(r'^API_KEY=', t, re.M) else t.rstrip()+'\nAPI_KEY='+secrets.token_urlsafe(32)+'\n'); p.write_text(t)"

echo Running tests...
pushd app
.venv\Scripts\python.exe -m pytest -q
popd

echo.
echo Setup done. Put your feed link in config\.env, then double-click login and serve.
pause
