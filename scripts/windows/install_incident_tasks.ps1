# Keep Cygnus's incident watcher running and its turns reviewed, without a terminal.
#
#   CygnusIncidentWatch   scripts\watch_incidents.py, started at logon and again
#                         if it dies (the rules; nothing leaves the machine)
#   CygnusIncidentReview  scripts\review_incidents.py, every hour (the new turns
#                         go to Claude through `claude -p`; see docs/PRIVACY.md)
#
# Both run pythonw.exe, so no console window opens and no Ctrl+C reaches them
# (run_detached.ps1 says why that matters).
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_incident_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_incident_tasks.ps1 -NoReview
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_incident_tasks.ps1 -Remove

param(
    [switch]$NoReview,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"

if ($Remove) {
    foreach ($name in "CygnusIncidentWatch", "CygnusIncidentReview") {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed $name"
        }
    }
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -match "watch_incidents\.py" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Confirm:$false; Write-Host "Stopped watcher pid $($_.ProcessId)" }
    return
}

if (-not (Test-Path $pythonw)) { throw "No pythonw at $pythonw" }
$user = "$env:USERDOMAIN\$env:USERNAME"

# The watcher: at logon, restarted up to three times a minute apart if it dies.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "scripts\watch_incidents.py" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "CygnusIncidentWatch" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Cygnus: write down what goes wrong (scripts\watch_incidents.py)" -Force | Out-Null
$running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
           Where-Object { $_.CommandLine -match "watch_incidents\.py" }
if (-not $running) { Start-ScheduledTask -TaskName "CygnusIncidentWatch" }
Write-Host "CygnusIncidentWatch: at logon$(if ($running) { ' (already running now)' } else { ', started now' })"

if ($NoReview) {
    if (Get-ScheduledTask -TaskName "CygnusIncidentReview" -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName "CygnusIncidentReview" -Confirm:$false
    }
    Write-Host "CygnusIncidentReview: not installed (-NoReview)"
    return
}

# The review: every hour from now on; one that was missed while asleep runs on wake.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "scripts\review_incidents.py" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "CygnusIncidentReview" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Cygnus: a model reviews the last hour's turns (scripts\review_incidents.py)" -Force | Out-Null
Write-Host "CygnusIncidentReview: every hour, first at $((Get-Date).AddMinutes(5).ToString('HH:mm'))"
Write-Host "Remove both: powershell -ExecutionPolicy Bypass -File scripts\windows\install_incident_tasks.ps1 -Remove"
