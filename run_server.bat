@echo off
REM Headless backend launcher for the always-on kiosk (no browser window).
REM Started at boot by the "KodryxAttendanceServer" scheduled task.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"%~dp0venv\Scripts\python.exe" serve.py
