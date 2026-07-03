@echo off
REM ===========================================================================
REM  Launch the TradAlert web control panel (one click).
REM  Uses the project venv directly (no activation / PATH needed). Starts the
REM  FastAPI server and opens the browser at http://localhost:8000.
REM  Stop with Ctrl+C in this window.
REM ===========================================================================
cd /d C:\PycharmProjects\TradAlert

if not exist .venv\Scripts\python.exe (
    echo .venv not found at C:\PycharmProjects\TradAlert\.venv — create it / fix the path.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m api --open --port 8000
pause
