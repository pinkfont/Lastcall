@echo off
rem Turn on the laptop's background sync (every 2 hours). It checks POLITEMall for
rem your submissions and pushes them to your server. To turn it off:  autosync.bat off
cd /d "%~dp0"
if /i "%~1"=="off" (
    powershell -NoProfile -ExecutionPolicy Bypass -File app\tools\autosync.ps1 -Remove
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File app\tools\autosync.ps1
)
pause
