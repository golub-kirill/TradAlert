<#
.SYNOPSIS
    Register (or re-register) the TradAlert daily scan as a Windows scheduled task.

.DESCRIPTION
    Runs scripts\run_daily.bat every weekday (Mon-Fri) at the given local time,
    only while the current user is logged on (no stored password needed).
    A run missed because the PC was off/asleep starts as soon as possible after.
    Re-running this script replaces any existing task of the same name.

    Task Scheduler fires on LOCAL time, so pick a time that is after the 4:00 PM
    ET US/TSX close in your timezone with a buffer for EOD data to settle.

.PARAMETER At
    Local fire time, HH:mm (default 18:00).

.PARAMETER TaskName
    Scheduled-task name (default "TradAlert Daily Scan").

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_daily_scan.ps1
    powershell -ExecutionPolicy Bypass -File scripts\register_daily_scan.ps1 -At 17:30
#>
param(
    [string]$At = "18:00",
    [string]$TaskName = "TradAlert Daily Scan"
)

$ErrorActionPreference = "Stop"

# Resolve repo paths from this script's own location (scripts\ sits under the root).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$Bat       = Join-Path $ScriptDir "run_daily.bat"

if (-not (Test-Path $Bat)) {
    throw "Wrapper not found: $Bat"
}

$action = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At

# StartWhenAvailable: catch up a run missed because the PC was off at fire time.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# Interactive principal = runs only when this user is logged on; no password stored.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Runs TradAlert main.py after the US close (Mon-Fri, local time)." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' - weekdays at $At, only when logged on."
Write-Host "Wrapper      : $Bat"
Write-Host "Scheduler log: $(Join-Path $Root 'logs\scheduler.log')"
Write-Host ""
Write-Host "Inspect : Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Run now : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove  : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
