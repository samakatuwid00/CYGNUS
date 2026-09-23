# Start a long run that outlives the terminal that started it.
#
# The 24-hour soak was started twice as a background job of a shell session and
# died with it both times (2026-09-23: 5 checks, then silence). Task Scheduler
# owns the process instead of the shell, so closing the terminal, or the agent
# session that started it, leaves it running.
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\run_detached.ps1 `
#       -Name CygnusSoak -Script scripts\bench\soak.py -Args "--hours 24"
#
#   schtasks /query /tn CygnusSoak        # is it still there
#   schtasks /end   /tn CygnusSoak        # stop the run
#   schtasks /delete /tn CygnusSoak /f    # forget the task
#
# Output goes to data\logs\<Name>-console.txt. The task runs once, now, as the
# current user, and does nothing after the run ends.

param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Script,
    [string]$Args = "",
    [switch]$Replace
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "No venv at $python" }
if (-not (Test-Path (Join-Path $repo $Script))) { throw "No script at $Script" }

# schtasks writes to stderr when a task is missing, which -ErrorActionPreference
# Stop turns into a terminating NativeCommandError. Ask through cmd instead.
$existing = cmd /c "schtasks /query /tn $Name >nul 2>nul && echo yes"
if ($existing -and -not $Replace) {
    throw "A task called $Name already exists. Pass -Replace to overwrite it, or: schtasks /delete /tn $Name /f"
}

$logs = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$out = Join-Path $logs "$Name-console.txt"

# cmd /c so stdout can be redirected; the task itself cannot redirect.
$command = "cmd /c cd /d `"$repo`" && `"$python`" $Script $Args > `"$out`" 2>&1"
$start = (Get-Date).AddSeconds(60).ToString("HH:mm")

schtasks /create /tn $Name /tr $command /sc once /st $start /f | Out-Null
schtasks /run /tn $Name | Out-Null
Start-Sleep -Seconds 3
schtasks /query /tn $Name /fo list | Select-String "TaskName|Status"
Write-Host "Console output: $out"
Write-Host "Stop it with:   schtasks /end /tn $Name   (then /delete /tn $Name /f)"
