# Deployment — Frontend (Vercel) + Backend (Render)

Client–server architecture:
- **Frontend** = static site in `frontend/` → **Vercel**. This is the URL you give HR.
- **Backend** = Flask API in `dashboard/app.py` (face recognition + DB) → **Render**.
- **Database** = MongoDB Atlas (durable, off the server).

```
HR browser ──> Vercel (frontend)  ──fetch /api──>  Render (backend)  ──>  MongoDB Atlas
   camera frames are captured in the browser and POSTed to /api/scan
```

---

## 1. MongoDB Atlas
Create a free cluster, a DB user, allow network access (0.0.0.0/0 for simplicity),
and copy the connection string:
`mongodb+srv://USER:PASS@cluster.xxxx.mongodb.net/attendance_db`

## 2. Backend on Render
1. Push this repo to GitHub.
2. Render → **New → Web Service** → pick the repo.
3. It reads `render.yaml` (or set manually):
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn dashboard.app:app --workers 1 --timeout 180 --bind 0.0.0.0:$PORT`
4. **Environment → add** `MONGO_URI` = your Atlas string.
5. **Plan: use a paid instance with ≥ 2 GB RAM.** InsightFace + the face model
   need memory; the free 512 MB tier will crash (OOM). First request after a
   cold start is slow (it downloads/loads the model).
6. After deploy, note the URL, e.g. `https://attendance-backend.onrender.com`.
   Test it: open `https://.../api/health` → `{"ok": true}`.

## 3. Frontend on Vercel
1. Vercel → **New Project** → same repo → set **Root Directory = `frontend`**.
2. No build step (static).
3. Before (or after) deploy, set the backend URL: edit **`frontend/config.js`**:
   ```js
   window.API_BASE = "https://attendance-backend.onrender.com";
   ```
   Commit & redeploy (or edit in the Vercel dashboard).
4. Vercel gives you the URL, e.g. `https://attendance.vercel.app` —
   **this is the link you give HR.** Nothing else for them to do.

## 4. First-time setup (once)
Open the Vercel URL → **Employees** tab → add each employee with photos.
They can immediately mark attendance from the **Mark Attendance** tab.

---

## Notes
- **HTTPS is required** for the browser camera. Vercel and Render are HTTPS, so
  the camera works. (Plain `http://` non-localhost will block the camera.)
- **CORS** is already enabled on the backend, so the Vercel domain can call it.
- **Liveness** (anti-photo) is ON by default. Test it on the real camera; if it
  rejects real faces, tune `LIVENESS_THRESHOLD` (env) or ask for the blink
  challenge. To disable for testing: env `ENABLE_ANTI_SPOOFING=0`.
- **Entry/exit timing** is env-tunable: `ATTENDANCE_SPLIT_HOUR` (default 13 = 1PM),
  `EXIT_MIN_GAP_HOURS` (default 1).
- **Clear today's attendance:** `python tools/clear_today.py` (uses `MONGO_URI`).
- Local single-host run (no split): `python serve.py` → open `http://localhost:5000`
  (the backend also serves the frontend at `/`).
