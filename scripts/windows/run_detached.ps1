# Start a long run that outlives the terminal that started it.
#
# The 24-hour soak died three times on 2026-09-23. Twice as a background job of
# a shell session, which took it down with the session. Then, as a scheduled
# task running python.exe through cmd, at 55 minutes with Last Result
# -1073741510 (STATUS_CONTROL_C_EXIT): a task started from the interactive
# session shares that session's console, so a Ctrl+C anywhere in it reached the
# run.
#
# So: Task Scheduler owns the process (not the shell), and the process is
# pythonw.exe (no console at all, so no console signal can reach it).
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\run_detached.ps1 `
#       -Name CygnusSoak -Script scripts\bench\soak.py -Args "--hours 24"
#
# pythonw writes nothing to a console, so the script's own log is the record -
# for the soak, data\logs\soak-<time>.jsonl. A crash before that file exists
# leaves no trace, which is the price of having no console.
#
# Stop the run with Stop-Process -Id <pid printed below>; the task itself is
# inert afterwards and is removed with:
#   schtasks /delete /tn <Name> /f

param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Script,
    [string]$Args = "",
    [switch]$Replace
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "No pythonw at $pythonw" }
if (-not (Test-Path (Join-Path $repo $Script))) { throw "No script at $Script" }

$existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
if ($existing -and -not $Replace) {
    throw "A task called $Name already exists. Pass -Replace, or: schtasks /delete /tn $Name /f"
}

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "$Script $Args" -WorkingDirectory $repo
# Defaults that stop long runs: the three-day kill, the "only on AC power"
# rule, and pausing when the laptop goes on battery.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable
Register-ScheduledTask -TaskName $Name -Action $action -Settings $settings `
    -Description "Cygnus: $Script $Args" -Force | Out-Null
Start-ScheduledTask -TaskName $Name
Start-Sleep -Seconds 5

$leaf = Split-Path -Leaf $Script
$run = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
       Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($leaf) }
if ($run) {
    Write-Host ("Running: pid {0}" -f ($run.ProcessId -join ", "))
    Write-Host ("Stop it: Stop-Process -Id {0}" -f ($run.ProcessId -join ", "))
} else {
    Write-Host "Started, but no pythonw process matched $leaf - it may have exited at once."
}
Write-Host "Forget the task: schtasks /delete /tn $Name /f"
