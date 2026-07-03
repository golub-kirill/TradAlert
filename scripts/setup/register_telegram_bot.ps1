<#
.SYNOPSIS
    Register (or re-register) the TradAlert interactive Telegram daemon as a
    Windows scheduled task that starts at logon and restarts on crash.

.DESCRIPTION
    Runs pythonw.exe telegram_bot.py directly at user logon, only while the current user
    is logged on (no stored password needed). The daemon long-polls Telegram and
    answers the alert/position buttons + commands. If it crashes, Task Scheduler
    restarts it (RestartCount / RestartInterval). Re-running this script replaces
    any existing task of the same name.

    Only ONE poller may drain the bot's getUpdates (Telegram 409 Conflict). The
    daemon takes data\telegram_bot.lock and exits cleanly if another instance
    already holds it, so a duplicate launch is harmless. Stop any other poller
    (e.g. a manual `python telegram_bot.py`) before relying on the task.

.PARAMETER TaskName
    Scheduled-task name (default "TradAlert Telegram Bot").

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_telegram_bot.ps1
#>
param(
    [string]$TaskName = "TradAlert Telegram Bot"
)

$ErrorActionPreference = "Stop"

# Resolve repo paths from this script's own location (scripts\setup\ sits two
# levels under the root).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$Pythonw   = Join-Path $Root ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $Pythonw)) {
    throw "venv pythonw not found: $Pythonw"
}

# Launch pythonw.exe DIRECTLY (not via run_telegram_bot.bat): a cmd wrapper spawns
# python as a CHILD that gets reparented on Stop-ScheduledTask, so the daemon keeps
# polling after a "stop". Running the interpreter as the task's OWN process means
# Stop-ScheduledTask terminates the daemon cleanly. pythonw = no console window;
# the daemon keeps its own structured log at data\telegram_bot.log. (run_telegram_bot.bat
# remains for manual launches.)
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "telegram_bot.py" -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn

# Long-running daemon: no execution time limit; restart up to 3x on crash;
# StartWhenAvailable so a logon missed while asleep still launches it.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# Interactive principal = runs only when this user is logged on; no password stored.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Runs the TradAlert interactive Telegram daemon at logon (auto-restart)." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' - starts at logon, only when logged on, restarts on crash."
Write-Host "Launch   : $Pythonw telegram_bot.py (direct; Stop-ScheduledTask kills it cleanly)"
Write-Host "Bot log  : $(Join-Path $Root 'logs\telegram_bot.log')"
Write-Host ""
Write-Host "Requires : telegram.daemon_enabled: true in config\settings.yaml,"
Write-Host "           TG_BOT_TOKEN + numeric TG_CHAT_ID in config\secrets.env."
Write-Host ""
Write-Host "Inspect  : Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Start now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Stop     : Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove   : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
