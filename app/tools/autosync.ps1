# Turns the laptop's background sync on or off (Windows Task Scheduler).
# Every 2 hours, while the laptop is on, it runs `duetracker sync --log`, which:
#   - checks POLITEMall for your submissions (using your saved login), and
#   - pushes the results to your server (API_URL in config/.env).
# Missed runs (laptop asleep or off) run as soon as it's back.
param([switch]$Remove)

$TaskName = 'Lastcall laptop sync'
$AppDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $AppDir '.venv\Scripts\pythonw.exe'   # pythonw = no window pops up

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Background sync turned off."
    exit 0
}
if (-not (Test-Path $Python)) {
    Write-Host "Run setup.bat first."
    exit 1
}

$action   = New-ScheduledTaskAction -Execute $Python -Argument '-m duetracker sync --log' -WorkingDirectory $AppDir
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 2)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Background sync is on: every 2 hours while your laptop is on (first run in 1 minute)."
Write-Host "Results of the last run: data\laptop_sync.log"
