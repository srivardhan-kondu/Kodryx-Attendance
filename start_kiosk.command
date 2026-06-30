#!/bin/bash
# ============================================================
#  start_kiosk.command  -  launches the attendance kiosk (macOS)
#  Double-click in Finder to run. (First time: right-click > Open
#  to get past Gatekeeper, or: chmod +x start_kiosk.command)
#  To auto-start at login: System Settings > General > Login Items
#  > add this file. HR then only uses the browser.
# ============================================================
cd "$(dirname "$0")"

PY="venv/bin/python"
[ -x "$PY" ] || PY="python3"

# macOS reserves port 5000 for AirPlay, so use 5050
export PORT=5050

"$PY" serve.py &
SERVER_PID=$!

sleep 8
open "http://localhost:5050/"

echo "Kiosk server running (PID $SERVER_PID). Close this window to stop."
wait $SERVER_PID
