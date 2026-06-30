"""
serve.py  —  the single backend for the web-centric attendance kiosk.

Serves EVERYTHING HR uses, all in the browser:
  • Dashboard + reports
  • /api/scan      (browser-camera attendance marking)
  • Add / remove employee (instant, no terminal worker)

Setup once:  put your Atlas connection string in a .env file:
    MONGO_URI=mongodb+srv://user:pass@cluster.xxxx.mongodb.net/attendance_db

Run:         python serve.py
Then open:   http://localhost:5000   (override with PORT env var)
"""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from attendance_db import initialise_database
from dashboard.app import app


def main():
    try:
        initialise_database()
        print("[OK] Database ready.")
    except Exception as exc:
        print(f"[WARN] DB init failed — check MONGO_URI in your .env file: {exc}")

    port = int(os.environ.get("PORT", "5000"))
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print(f"\n  Attendance kiosk running →  http://localhost:{port}\n"
          f"  Open that in a browser. Press Ctrl+C to stop.\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
