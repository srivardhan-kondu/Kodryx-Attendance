@echo off
REM ============================================================
REM  start_kiosk.bat  -  launches the attendance kiosk (Windows)
REM  Double-click to run, OR put a shortcut to this file in the
REM  Startup folder (Win+R -> shell:startup) so it runs on boot.
REM  HR then only ever uses the browser.
REM ============================================================
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
) else (
    set "PY=python"
)

REM Start the backend minimized in the background
start "AttendanceKiosk" /min %PY% serve.py

REM Wait for it to come up, then open the kiosk page in the browser
timeout /t 8 /nobreak >nul
start "" http://localhost:5000/

REM --- For a locked-down full-screen kiosk, comment the line above
REM     and use Chrome/Edge kiosk mode instead, e.g.:
REM start "" chrome --kiosk --app=http://localhost:5000/
