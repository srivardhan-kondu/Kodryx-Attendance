# Zero-Terminal Kiosk Setup

Goal: the kiosk PC boots → the backend starts automatically → HR only ever
uses the **browser**. HR never opens a terminal.

The whole system is one backend process (`serve.py`) that serves the
dashboard, the camera scanning (`/api/scan`), and employee enrollment.

---

## Windows (the kiosk PC)

### One-time setup (you / IT)
1. Install Python 3.11 (tick **"Add Python to PATH"**).
2. Double-click **`setup_kiosk.bat`** — builds the environment, installs everything.
3. Create a file named **`.env`** in this folder with your database URL:
   ```
   MONGO_URI=mongodb+srv://USER:PASS@cluster.xxxx.mongodb.net/attendance_db
   ```
4. Double-click **`start_kiosk.bat`** to test — a browser opens at
   `http://localhost:5000/#kiosk` and the camera starts scanning by itself.

### Make it auto-start on boot
1. Press `Win + R`, type **`shell:startup`**, press Enter (opens the Startup folder).
2. Right-click **`start_kiosk.bat`** → **Create shortcut**, then move the shortcut
   into that Startup folder.
3. Reboot. The server now starts on boot and the browser opens to the kiosk page.

> More robust option: use **Task Scheduler** → "Create Task" → trigger **At startup**,
> action: start `start_kiosk.bat`. This runs even before login and can restart on failure.

---

## macOS (for local testing)
1. `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
2. Create `.env` with your `MONGO_URI` (or omit to use the in-memory demo).
3. Double-click **`start_kiosk.command`** (first time: right-click → Open).
   It serves on `http://localhost:5050` and opens the browser.
4. Auto-start at login: **System Settings → General → Login Items → +** → add
   `start_kiosk.command`.

---

## What HR does (every day — browser only)
- **Mark Attendance** tab → **▶ Start Camera**. People look at the camera; first
  scan of the day = login, later scans update exit time. **⏹ Stop Camera** when done.
- **Enroll Employee** tab → type a name, add 1–3 photos → learned instantly.
- **Daily / Monthly** tabs → view and export reports.

## Notes
- **The scanning PC must use `http://localhost:5000/#kiosk`.** Two parts matter:
  - `#kiosk` is what auto-starts the camera and opens Mark Attendance. Without
    it the page is just a dashboard and the camera never turns on.
  - `localhost` is what makes the camera legal. Browsers only grant camera
    access on `https://` or `localhost`, so opening the scanner at
    `http://<kiosk-pc-ip>:5000` **cannot work** — the browser hides the camera
    API entirely and the page will say so in the status banner.
- Other office PCs can still open `http://<kiosk-pc-ip>:5000` to **view**
  reports; only the camera is unavailable there.
- Liveness (anti-photo) is ON by default. To relax during testing only:
  set `ENABLE_ANTI_SPOOFING=0` (off) or `LIVENESS_THRESHOLD=0.3` (looser)
  as environment variables before launch.
