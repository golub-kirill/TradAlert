@echo off
REM ===========================================================================
REM TradAlert intraday 1h held-position monitor wrapper for Task Scheduler.
REM Runs scripts\intraday_monitor.py with the project virtualenv and appends the
REM combined stdout/stderr + exit code to logs\intraday_monitor.log.
REM Any extra arguments are forwarded, e.g.:  run_intraday_monitor.bat --force
REM ===========================================================================

setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%"

set "PYTHONIOENCODING=utf-8"

if not exist "logs" mkdir "logs"

echo ============================================================>> "logs\intraday_monitor.log"
echo [%DATE% %TIME%] starting intraday_monitor.py %*>> "logs\intraday_monitor.log"

".venv\Scripts\python.exe" scripts\intraday_monitor.py %*>> "logs\intraday_monitor.log" 2>&1
set "RC=%ERRORLEVEL%"

echo [%DATE% %TIME%] intraday_monitor.py exited rc=%RC%>> "logs\intraday_monitor.log"

popd
endlocal & exit /b %RC%
