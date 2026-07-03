<#
.SYNOPSIS
    Register (or re-register) the TradAlert intraday 1h held-position monitor as a
    Windows scheduled task.

.DESCRIPTION
    Runs scripts\run_intraday_monitor.bat every weekday (Mon-Fri), repeating hourly
    within a daily window (default 10:00-16:00 LOCAL time), only while the current
    user is logged on (no stored password). Each run checks open LONG positions and
    alerts (Telegram) on a 1h close below the stop. Journal-only.

    Task Scheduler fires on LOCAL time — set -Start/-End to your local equivalent of
    ~30 min after the US open through the close. The script also self-gates to NYSE
    RTH, so an off-hours fire is a harmless no-op.

    Re-running replaces any existing task of the same name.

.PARAMETER Start
    Local start time of the hourly window, HH:mm (default 10:00).

.PARAMETER End
    Local end time of the hourly window, HH:mm (default 16:00).

.PARAMETER TaskName
    Scheduled-task name (default "TradAlert Intraday Monitor").

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_intraday_monitor.ps1
    powershell -ExecutionPolicy Bypass -File scripts\register_intraday_monitor.ps1 -Start 09:30 -End 16:00
#>
param(
    [string]$Start = "10:00",
    [string]$End = "16:00",
    [string]$TaskName = "TradAlert Intraday Monitor"
)

$ErrorActionPreference = "Stop"

# scripts\setup\ sits two levels under the root; the scheduler-pinned wrapper
# stays at scripts\ root.
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsDir = Split-Path -Parent $ScriptDir
$Root       = Split-Path -Parent $ScriptsDir
$Bat        = Join-Path $ScriptsDir "run_intraday_monitor.bat"

if (-not (Test-Path $Bat)) {
    throw "Wrapper not found: $Bat"
}

$action = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory $Root

# Repeat hourly from $Start until $End, on weekdays. RepetitionDuration bounds the
# hourly repeats to the trading window; the task self-gates to RTH regardless.
$span = [datetime]$End - [datetime]$Start
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Start
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $Start `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration $span).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Hourly intraday 1h held-long breakdown monitor (Mon-Fri, local time)." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' - weekdays hourly $Start-$End, only when logged on."
Write-Host "Wrapper      : $Bat"
Write-Host "Monitor log  : $(Join-Path $Root 'logs\intraday_monitor.log')"
Write-Host ""
Write-Host "Run now : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove  : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
