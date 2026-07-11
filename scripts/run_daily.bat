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

REM UTF-8 so the report's box-drawing dividers don't raise UnicodeEncodeError
REM when stdout is redirected to the cp1252 log below.
set "PYTHONIOENCODING=utf-8"

if not exist "logs" mkdir "logs"

echo ============================================================>> "logs\scheduler.log"

REM Ensure the local Ollama server is up so the AI advisor can score entries.
REM Fail-safe: the helper always exits 0, so a down/missing Ollama never blocks
REM the scan (the advisor is fail-open and simply omits its note).
echo [%DATE% %TIME%] ensuring ollama...>> "logs\scheduler.log"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup\ensure_ollama.ps1">> "logs\scheduler.log" 2>&1

echo [%DATE% %TIME%] starting main.py %*>> "logs\scheduler.log"

".venv\Scripts\python.exe" main.py %*>> "logs\scheduler.log" 2>&1
set "RC=%ERRORLEVEL%"

echo [%DATE% %TIME%] main.py exited rc=%RC%>> "logs\scheduler.log"

popd
endlocal & exit /b %RC%
