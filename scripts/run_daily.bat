@echo off
REM ===========================================================================
REM TradAlert daily-scan wrapper for Windows Task Scheduler.
REM
REM Runs main.py with the project virtualenv and appends the wrapper's combined
REM stdout/stderr plus the exit code to logs\scheduler.log. main.py keeps its own
REM detailed application log in data\tradealert.log; this file captures the
REM scheduled-context output (incl. any traceback before logging is configured)
REM and a one-line run/exit record so a failed unattended run is easy to spot.
REM
REM Any extra arguments are forwarded to main.py, e.g.:
REM     run_daily.bat --force
REM ===========================================================================

setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%"

if not exist "logs" mkdir "logs"

echo ============================================================>> "logs\scheduler.log"
echo [%DATE% %TIME%] starting main.py %*>> "logs\scheduler.log"

".venv\Scripts\python.exe" main.py %*>> "logs\scheduler.log" 2>&1
set "RC=%ERRORLEVEL%"

echo [%DATE% %TIME%] main.py exited rc=%RC%>> "logs\scheduler.log"

popd
endlocal & exit /b %RC%
