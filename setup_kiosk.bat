@echo off
REM ============================================================
REM  setup_kiosk.bat  -  ONE-TIME setup (run once by IT/you)
REM  Creates the Python environment and installs everything.
REM ============================================================
cd /d "%~dp0"

echo Creating virtual environment...
python -m venv venv

echo Installing dependencies (this downloads the AI models on first run)...
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\pip install -r requirements.txt

echo.
echo ============================================================
echo  Setup complete.
echo  1) Create a file named  .env  in this folder containing:
echo        MONGO_URI=your_atlas_connection_string
echo  2) Double-click  start_kiosk.bat  to launch.
echo  3) To auto-start on boot, see AUTOSTART.md
echo ============================================================
pause
