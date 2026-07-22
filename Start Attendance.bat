@echo off
title Kodryx Office Attendance
cd /d "%~dp0"

echo ================================================
echo         KODRYX OFFICE ATTENDANCE
echo ================================================
echo.

REM Use the project's own Python (it has the face-recognition models)
set "PY=%~dp0venv\Scripts\python.exe"

if not exist "%PY%" (
  echo  [PROBLEM] Setup looks incomplete - the "venv" folder is missing.
  echo  Please contact your administrator to finish setup.
  echo.
  pause
  exit /b 1
)

echo  Starting the attendance system...
echo  The dashboard will open in your web browser automatically.
echo.
echo  ============================================================
echo   KEEP THIS WINDOW OPEN while using the system.
echo   To shut down the attendance system, just close this window.
echo  ============================================================
echo.

REM Open the dashboard in the default browser ~10 seconds after startup
start "" /b powershell -WindowStyle Hidden -Command "Start-Sleep 10; Start-Process 'http://localhost:5000'"

REM Run the server in THIS window (its logs appear below)
"%PY%" serve.py

echo.
echo  The attendance server has stopped.
pause
