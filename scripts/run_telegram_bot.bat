@echo off
REM ===========================================================================
REM TradAlert interactive Telegram daemon wrapper for Windows Task Scheduler.
REM
REM Launches telegram_bot.py with the project virtualenv and appends the
REM wrapper's combined stdout/stderr plus the exit code to logs\telegram_bot.log.
REM The daemon keeps its own structured application log in data\telegram_bot.log;
REM this file captures the scheduled-context output (incl. any traceback before
REM logging is configured) and a one-line start/exit record.
REM
REM Long-running: the bot polls until killed. Task Scheduler restarts it on
REM crash (see register_telegram_bot.ps1). Only ONE instance may poll at a time —
REM the daemon takes data\telegram_bot.lock and exits 0 if another holds it.
REM ===========================================================================

setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%"

if not exist "logs" mkdir "logs"

echo ============================================================>> "logs\telegram_bot.log"
echo [%DATE% %TIME%] starting telegram_bot.py>> "logs\telegram_bot.log"

".venv\Scripts\python.exe" telegram_bot.py>> "logs\telegram_bot.log" 2>&1
set "RC=%ERRORLEVEL%"

echo [%DATE% %TIME%] telegram_bot.py exited rc=%RC%>> "logs\telegram_bot.log"

popd
endlocal & exit /b %RC%
