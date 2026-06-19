# =============================================================
#  dashboard/app.py  —  Web Dashboard (Phase 3)
#
#  New in Phase 3:
#    • Camera status includes all 4 cameras
# =============================================================

import sys
import os
import cv2
import time
import threading
import tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import (Flask, render_template_string, jsonify,
                   send_file, request, send_from_directory, Response)
from tz_utils import strftime_today
from attendance_db import (
    get_today_summary, get_date_summary, export_to_excel,
    get_camera_status, get_monthly_report, export_monthly_to_excel,
    get_recent_unknowns
)
def get_activity_summary(*args, **kwargs): return []
def get_recent_activity(*args, **kwargs): return []
def export_activity_to_excel(*args, **kwargs): return None
def get_live_presence(*args, **kwargs): return []
def get_tracking_history(*args, **kwargs): return []
def get_activity_summary_tracking(*args, **kwargs): return []
from config import CAMERA_CONFIG

app = Flask(__name__)

# ---------------------------------------------------------------
# MJPEG Camera Streamer — thread-safe frame buffer per camera
# ---------------------------------------------------------------
class CameraStream:
    """Opens a camera in a background thread and holds the latest JPEG frame."""

    def __init__(self, camera_name, camera_url):
        self.name      = camera_name
        self.url       = camera_url
        self._last_good_frame = None

    def start(self):
        pass

    def get_frame(self):
        import os
        import time
        import tempfile
        temp_path = os.path.join(tempfile.gettempdir(), f"{self.name}_latest.jpg")
        try:
            if os.path.exists(temp_path):
                if time.time() - os.path.getmtime(temp_path) < 10:
                    with open(temp_path, "rb") as f:
                        data = f.read()
                    if len(data) > 100 and data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'):
                        self._last_good_frame = data
                        return data
        except Exception:
            pass
        return self._last_good_frame

    def is_alive(self):
        return True


# One stream object per configured camera
_streams: dict[str, CameraStream] = {}

def _get_or_start_stream(camera_name: str) -> CameraStream | None:
    """Lazy-create and start a CameraStream on first request.
    URL is resolved from CAMERA_CONFIG — no hardcoded webcam indices.
    """
    if camera_name not in _streams:
        # Look up the URL from the central config
        url = None
        for cam in CAMERA_CONFIG:
            if cam["name"] == camera_name:
                url = cam.get("url")
                break
        if url is None:
            return None
        stream = CameraStream(camera_name, url)
        _streams[camera_name] = stream
        stream.start()
    return _streams[camera_name]


def _mjpeg_generator(stream: CameraStream):
    """Yield multipart JPEG frames for the MJPEG response."""
    OFFLINE_JPEG = None   # generated once on first need
    while True:
        frame = stream.get_frame()
        if frame is None:
            # Send a small offline JPEG placeholder
            if OFFLINE_JPEG is None:
                import numpy as np
                img = np.zeros((540, 960, 3), dtype='uint8')
                img[:] = (30, 30, 40)   # dark background
                cv2.putText(img, 'Camera Offline', (330, 260),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.5, (120, 120, 140), 2)
                _, buf = cv2.imencode('.jpg', img)
                OFFLINE_JPEG = buf.tobytes()
            frame = OFFLINE_JPEG
            time.sleep(1)
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
               + frame + b'\r\n')
        time.sleep(0.08)


# ---------------------------------------------------------------
# HTML — Full Dashboard
# ---------------------------------------------------------------
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Office Attendance Dashboard</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ccircle cx='50' cy='50' r='50' fill='%23328243'/%3E%3Cpath d='M42 52c8.28 0 15-6.72 15-15s-6.72-15-15-15-15 6.72-15 15 6.72 15 15 15zm0 8c-10.04 0-30 5.04-30 15v10h60v-10c0-9.96-19.96-15-30-15z' fill='white'/%3E%3Ccircle cx='72' cy='65' r='16' fill='white'/%3E%3Cpath d='M64 65l5 5 10-10' fill='none' stroke='%23328243' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-page:     #f6f5f1;
      --bg-surface:  #ffffff;
      --bg-subtle:   #f1efe9;
      --bg-inset:    #faf9f5;
      --border:      #e4e1d8;
      --border-strong:#cfcbbe;
      --text:        #1c1b18;
      --text-soft:   #4a4945;
      --text-muted:  #7a7870;
      --accent:      #1f3a5f;
      --accent-soft: #e7ecf3;
      --success:     #1f7a4d;
      --success-bg:  #e6f1ea;
      --danger:      #a8331f;
      --danger-bg:   #f6e6e1;
      --warning:     #8a5a0b;
      --warning-bg:  #f5ecd7;
      --info:        #28556e;
      --info-bg:     #e4edf2;
      --mono:        'IBM Plex Mono', ui-monospace, monospace;
      --sans:        'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { height: 100%; }

    body {
      font-family: var(--sans);
      background: var(--bg-page);
      color: var(--text);
      font-size: 14px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }

    /* ---------- Top bar ---------- */
    .topbar {
      background: var(--bg-surface);
      border-bottom: 1px solid var(--border);
      padding: 0 32px;
      height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand-mark {
      width: 28px;
      height: 28px;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      display: grid;
      place-items: center;
      font-weight: 700;
      font-size: 12px;
      letter-spacing: 0.04em;
    }
    .topbar h1 {
      font-size: 15px;
      font-weight: 600;
      color: var(--text);
      letter-spacing: -0.005em;
    }
    .topbar h1 .sub {
      color: var(--text-muted);
      font-weight: 400;
      margin-left: 8px;
      font-size: 13px;
    }
    .topbar-right {
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .cam-badges {
      display: flex;
      gap: 8px;
    }
    .cam-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: var(--text-soft);
      background: var(--bg-inset);
      padding: 6px 10px;
      border-radius: 6px;
      border: 1px solid var(--border);
    }
    .dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
    .dot-green  { background: var(--success); }
    .dot-red    { background: var(--danger); }
    .dot-yellow { background: var(--text-muted); }

    .live-clock {
      font-family: var(--mono);
      font-size: 13px;
      font-weight: 500;
      color: var(--text-soft);
      padding: 6px 10px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--bg-inset);
      font-variant-numeric: tabular-nums;
    }

    /* ---------- Layout ---------- */
    .container {
      max-width: 1320px;
      margin: 0 auto;
      padding: 28px 32px 64px;
    }

    /* ---------- Tabs ---------- */
    .tabs {
      display: flex;
      gap: 4px;
      margin-bottom: 24px;
      border-bottom: 1px solid var(--border);
    }
    .tab {
      background: none;
      border: none;
      color: var(--text-muted);
      padding: 10px 14px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      border-bottom: 2px solid transparent;
      margin-bottom: -1px;
      transition: color 0.15s ease, border-color 0.15s ease;
      font-family: var(--sans);
      user-select: none;
    }
    .tab:hover { color: var(--text); }
    .tab.active {
      color: var(--text);
      border-bottom-color: var(--accent);
    }

    /* ---------- Layout ---------- */
    .main { }
    .page { display: none; }
    .page.active { display: block; }

    /* ---------- Stat cards ---------- */
    .stat-row {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 24px;
    }
    .stat-card {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px 18px;
    }
    .stat-label {
      font-size: 12px;
      color: var(--text-muted);
      margin-bottom: 6px;
      font-weight: 500;
      letter-spacing: 0.01em;
    }
    .stat-value {
      font-size: 26px;
      font-weight: 600;
      color: var(--text);
      letter-spacing: -0.02em;
      font-variant-numeric: tabular-nums;
    }
    .stat-sub { font-size: 11px; color: var(--text-muted); margin-top: 4px; }

    /* ---------- Section heading ---------- */
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
      gap: 16px;
    }
    .section-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--text);
      letter-spacing: -0.005em;
    }

    /* ---------- Tables ---------- */
    .table-wrap {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      margin-bottom: 20px;
    }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    thead tr { border-bottom: 1px solid var(--border); }
    thead th {
      padding: 10px 14px;
      font-size: 11px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      background: var(--bg-inset);
    }
    thead th:first-child { border-top-left-radius: 6px; }
    thead th:last-child  { border-top-right-radius: 6px; }
    tbody tr { border-bottom: 1px solid var(--border); }
    tbody tr:last-child { border-bottom: none; }
    tbody tr:hover { background: var(--bg-inset); }
    tbody td { padding: 12px 14px; vertical-align: middle; color: var(--text); }
    td.name { font-weight: 500; }
    td.mono {
      font-family: var(--mono);
      font-variant-numeric: tabular-nums;
      color: var(--text-soft);
      font-size: 13px;
    }
    .empty {
      text-align: center;
      padding: 40px 16px;
      color: var(--text-muted);
      font-size: 13px;
    }

    /* ---------- Badges ---------- */
    .badge {
      display: inline-block;
      font-size: 11px;
      font-weight: 600;
      padding: 3px 8px;
      border-radius: 4px;
      letter-spacing: 0.02em;
      border: 1px solid transparent;
      white-space: nowrap;
    }
    .badge-green  { background: var(--success-bg); color: var(--success); border-color: #cfe3d7; }
    .badge-yellow { background: var(--warning-bg); color: var(--warning); border-color: #ead9b0; }
    .badge-red    { background: var(--danger-bg);  color: var(--danger);  border-color: #ecd0c8; }
    .badge-blue   { background: var(--info-bg);    color: var(--info);    border-color: #cfdde4; }
    .badge-purple { background: var(--accent-soft);color: var(--accent);  border-color: #ccd6e3; }
    .badge-orange { background: var(--warning-bg); color: var(--warning); border-color: #ead9b0; }
    .badge-gray   { background: var(--bg-inset);   color: var(--text-muted); border-color: var(--border); }

    /* ---------- Controls ---------- */
    .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    input[type="date"], input[type="month"], select {
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      padding: 7px 10px;
      font-size: 13px;
      background: var(--bg-surface);
      color: var(--text);
      outline: none;
      font-family: var(--sans);
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    input[type="date"]:focus, input[type="month"]:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-soft);
    }
    button, .btn {
      padding: 7px 14px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 500;
      border: 1px solid var(--border-strong);
      background: var(--bg-surface);
      color: var(--text);
      cursor: pointer;
      transition: background 0.15s ease, border-color 0.15s ease;
      font-family: var(--sans);
    }
    button:hover, .btn:hover {
      background: var(--bg-inset);
      border-color: #b8b4a6;
    }
    .btn-primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    .btn-primary:hover {
      background: #18304d;
      border-color: #18304d;
    }
    .btn-ghost {
      background: var(--bg-surface);
      border: 1px solid var(--border-strong);
      color: var(--text);
    }
    .btn-ghost:hover { background: var(--bg-inset); }

    /* ---------- Activity meter ---------- */
    .meter { background: var(--bg-inset); border-radius: 99px; height: 6px; width: 80px; overflow: hidden; display: inline-block; vertical-align: middle; border: 1px solid var(--border); }
    .meter-fill { height: 100%; border-radius: 99px; background: var(--success); }

    /* ---------- Camera pill ---------- */
    .cam-pill {
      display: inline-block;
      font-size: 11px;
      font-weight: 500;
      padding: 3px 8px;
      border-radius: 4px;
      background: var(--bg-inset);
      border: 1px solid var(--border);
      color: var(--text-soft);
      text-transform: capitalize;
      font-family: var(--mono);
    }

    /* ---------- Scrollbar ---------- */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 3px; }

    @media (max-width: 960px) { .stat-row { grid-template-columns: repeat(2, 1fr); } }

    /* ---------- Camera Watch ---------- */
    .cameras-intro {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 20px;
      padding: 10px 14px;
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 6px;
    }
    .camera-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 20px;
    }
    @media (max-width: 900px) { .camera-grid { grid-template-columns: 1fr; } }

    .cam-tile {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .cam-tile-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
      background: var(--bg-inset);
    }
    .cam-tile-name {
      font-size: 13px;
      font-weight: 600;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .cam-role-chip {
      font-size: 10px;
      font-weight: 600;
      padding: 2px 7px;
      border-radius: 4px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .cam-tile-actions {
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .cam-action-btn {
      background: var(--bg-surface);
      border: 1px solid var(--border-strong);
      border-radius: 5px;
      padding: 5px 9px;
      font-size: 12px;
      cursor: pointer;
      color: var(--text-soft);
      font-family: var(--sans);
      transition: background 0.15s;
    }
    .cam-action-btn:hover { background: var(--bg-inset); }
    .cam-feed-wrap {
      position: relative;
      background: #1c1b18;
      aspect-ratio: 16/9;
      overflow: hidden;
    }
    .cam-feed {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .cam-feed-overlay {
      position: absolute;
      inset: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 10px;
      background: rgba(10,10,15,0.85);
      color: #9ca3af;
      font-size: 13px;
    }
    .cam-feed-overlay .offline-icon { font-size: 36px; opacity: 0.5; }
    .cam-status-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 14px;
      font-size: 11px;
      color: var(--text-muted);
      border-top: 1px solid var(--border);
      font-family: var(--mono);
    }
    .cam-status-live {
      display: flex;
      align-items: center;
      gap: 5px;
      color: var(--success);
      font-weight: 600;
    }
    .pulse-dot {
      width: 7px; height: 7px;
      border-radius: 50%;
      background: var(--success);
      animation: camPulse 1.5s infinite;
    }
    @keyframes camPulse {
      0%,100% { opacity: 1; transform: scale(1); }
      50%      { opacity: 0.4; transform: scale(0.8); }
    }

    /* Fullscreen overlay */
    .fs-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: #000;
      z-index: 9000;
      flex-direction: column;
    }
    .fs-overlay.open { display: flex; }
    .fs-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 20px;
      background: rgba(0,0,0,0.6);
      position: absolute;
      top: 0; left: 0; right: 0;
      z-index: 1;
    }
    .fs-title { color: #fff; font-size: 14px; font-weight: 600; }
    .fs-close {
      color: #fff;
      background: rgba(255,255,255,0.1);
      border: 1px solid rgba(255,255,255,0.2);
      border-radius: 6px;
      padding: 5px 12px;
      font-size: 13px;
      cursor: pointer;
      font-family: var(--sans);
    }
    .fs-close:hover { background: rgba(255,255,255,0.2); }
    .fs-img {
      width: 100%;
      height: 100%;
      object-fit: contain;
    }
  </style>
</head>
<body>

<!-- Topbar -->
<div class="topbar">
  <div class="brand">
    <div class="brand-mark">OA</div>
    <h1>Office Attendance<span class="sub">Operations console</span></h1>
  </div>
  <div class="topbar-right">
    <div class="cam-badges" id="cam-badges">
      <div class="cam-badge"><div class="dot dot-yellow"></div><span>Loading cameras...</span></div>
    </div>
    <div class="live-clock" id="clock">--:--:--</div>
  </div>
</div>

<div class="container">

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" id="nav-tab-attendance" onclick="showTab('tab-attendance')">Daily Dashboard</div>
  <div class="tab" id="nav-tab-monthly" onclick="showTab('tab-monthly')">Monthly Reports</div>
  <div class="tab" id="nav-tab-enroll" onclick="showTab('tab-enroll')">Enroll Employee</div>
</div>

<div class="main">

  <!-- ══════════════════ DAILY DASHBOARD ══════════════════ -->
  <div id="tab-attendance" class="page active">
    <div class="stat-row">
      <div class="stat-card">
        <div class="stat-label">Present Today</div>
        <div class="stat-value" id="att-present">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">In Office Now</div>
        <div class="stat-value" id="att-inoffice" style="color:var(--accent)">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Completed 8 Hrs</div>
        <div class="stat-value" id="att-complete" style="color:var(--success)">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Short Hours Today</div>
        <div class="stat-value" id="att-short" style="color:var(--danger)">—</div>
      </div>
    </div>

    <div class="table-wrap">
      <div class="section-head" style="padding:16px 18px 0;">
        <h2 class="section-title">Today's Attendance — <span id="today-date" style="color:var(--text-muted);font-weight:400;"></span></h2>
        <button onclick="loadAttendance()">Refresh Now</button>
      </div>
      <table style="margin-top:12px;">
        <thead>
          <tr>
            <th>Employee</th>
            <th>First Seen</th>
            <th>Last Seen</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="att-body">
          <tr><td colspan="4" class="empty">Loading daily data...</td></tr>
        </tbody>
      </table>
      <p style="font-size:12px;color:var(--text-muted);padding:10px 14px;">Automatically updates every 30 seconds.</p>
    </div>

    <div class="table-wrap">
      <div class="section-head" style="padding:16px 18px 0;">
        <div class="section-title">Historical Attendance Lookup</div>
        <div class="controls">
          <input type="date" id="att-date">
          <button onclick="loadAttendance()">View Date</button>
        </div>
      </div>
      <div style="padding:12px 14px 14px;">
        <div class="controls">
          <input type="date" id="exp-start">
          <span style="color:var(--text-muted);font-size:13px;">to</span>
          <input type="date" id="exp-end">
          <button class="btn-primary" onclick="exportAttendance()">Download Excel Sheet</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ══════════════════ WORK ACTIVITY ══════════════════ -->
  <div id="tab-activity" class="page">
    <div class="stat-row">
      <div class="stat-card">
        <div class="stat-label">Observed People</div>
        <div class="stat-value" id="act-count" style="color:var(--accent)">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Avg Working %</div>
        <div class="stat-value" id="act-avg" style="color:var(--success)">0%</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Recent Events</div>
        <div class="stat-value" id="act-verified">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Date</div>
        <div class="stat-value" id="act-date-display" style="font-size:18px;color:var(--text-muted)">Today</div>
      </div>
    </div>

    <div class="table-wrap">
      <div class="section-head" style="padding:16px 18px 0;">
        <div class="section-title">Daily Work Activity</div>
        <div class="controls">
          <input type="date" id="act-date">
          <button onclick="loadActivity()">View Date</button>
        </div>
      </div>
      <table style="margin-top:12px;">
        <thead>
          <tr>
            <th>Employee</th>
            <th>Working</th>
            <th>Not Working</th>
            <th>Unknown</th>
            <th>Observed</th>
            <th>Working %</th>
            <th>Last Seen</th>
          </tr>
        </thead>
        <tbody id="act-body">
          <tr><td colspan="7" class="empty">No activity observations loaded.</td></tr>
        </tbody>
      </table>
    </div>

    <div class="table-wrap">
      <div class="section-head" style="padding:16px 18px 0 14px;">
        <div class="section-title">Export Work Activity Range</div>
      </div>
      <div style="padding:12px 14px 14px;">
        <div class="controls">
          <input type="date" id="act-exp-start">
          <span style="color:var(--text-muted);font-size:13px;">to</span>
          <input type="date" id="act-exp-end">
          <button class="btn-primary" onclick="exportActivity()">Download Excel Sheet</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ══════════════════ MONTHLY ══════════════════ -->
  <div id="tab-monthly" class="page">
    <div class="table-wrap">
      <div class="section-head" style="padding:16px 18px 0;">
        <div class="section-title">Monthly Attendance Summaries</div>
        <div class="controls">
          <input type="month" id="month-select">
          <button class="btn-primary" onclick="loadMonthly()">Generate Report</button>
          <button onclick="exportMonthly()">Export Monthly Excel</button>
        </div>
      </div>
      <table style="margin-top:12px;">
        <thead>
          <tr>
            <th>Employee ID</th>
            <th>Employee Name</th>
            <th>Days Present</th>
            <th>Required Workdays</th>
            <th>Attendance %</th>
          </tr>
        </thead>
        <tbody id="monthly-body">
          <tr><td colspan="5" class="empty">Select a month above and click Generate Report.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ══════════════════ ENROLL EMPLOYEE ══════════════════ -->
  <div id="tab-enroll" class="page">
    <div class="section-head" style="padding:16px 18px 0;">
      <div class="section-title">Enroll New Employee</div>
    </div>
    <div style="padding: 16px; max-width: 500px;">
      <p style="color:var(--text-muted); font-size:14px; margin-bottom:16px;">
        Upload 1 to 3 clear photos of the new employee's face. The cloud dashboard will securely queue these images for your local Office PC to process and train the AI model.
      </p>
      <div style="display:flex; flex-direction:column; gap:12px;">
        <input type="text" id="enroll-name" placeholder="Employee Full Name" style="padding:10px; border:1px solid var(--border); border-radius:4px; font-family:var(--sans); font-size:14px;">
        
        <!-- Webcam Section -->
        <div style="border: 1px solid var(--border); border-radius: 4px; padding: 12px; background: var(--bg-inset);">
          <div style="display: flex; gap: 8px; margin-bottom: 12px;">
            <button class="btn-ghost" onclick="startWebcam()" id="btn-start-cam" style="flex:1; padding:10px; border-radius:4px; border: 1px solid var(--border); background: #fff; cursor: pointer;">Start Webcam</button>
            <button class="btn-primary" onclick="capturePhoto()" id="btn-capture" style="flex:1; display:none; padding:10px; border-radius:4px; border:none; cursor: pointer;">Capture Photo</button>
          </div>
          <video id="webcam-video" autoplay playsinline style="width: 100%; border-radius: 4px; display: none; background: #000;"></video>
          <canvas id="webcam-canvas" style="display:none;"></canvas>
          <div id="captured-preview" style="display: flex; gap: 8px; margin-top: 12px; overflow-x: auto;"></div>
        </div>

        <p style="color:var(--text-muted); font-size:12px; text-align:center; margin:0;">— OR UPLOAD FILES —</p>
        
        <input type="file" id="enroll-files" multiple accept="image/*" style="padding:10px; border:1px solid var(--border); border-radius:4px; font-family:var(--sans);">
        <button class="btn-primary" onclick="submitEnrollment()" style="padding:12px 16px; border-radius:4px; font-size:14px; font-weight:600;">Submit Enrollment</button>
      </div>
      <p id="enroll-status" style="margin-top:16px; font-weight:600; font-size:14px;"></p>
    </div>
    
    <!-- ═════════ REGISTERED EMPLOYEES LIST ═════════ -->
    <div class="section-head" style="padding:16px 18px 0; margin-top: 16px; border-top: 1px solid var(--border);">
      <div class="section-title">Registered Employees</div>
      <button onclick="loadEmployees()">Refresh List</button>
    </div>
    <div style="padding: 16px;">
      <div id="employee-list-container">
        <p style="color:var(--text-muted);font-style:italic;">Loading employees...</p>
      </div>
    </div>

  </div>

</div><!-- /main -->
</div><!-- /container -->

<!-- Fullscreen Camera Overlay -->
<div class="fs-overlay" id="fs-overlay">
  <div class="fs-bar">
    <span class="fs-title" id="fs-title">Camera Feed</span>
    <button class="fs-close" onclick="closeFullscreen()">✕ Close</button>
  </div>
  <img class="fs-img" id="fs-img" src="" alt="fullscreen feed">
</div>

<style>
  .unknown-grid {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 12px;
    margin-top: 8px;
  }
  @media (max-width: 1024px) { .unknown-grid { grid-template-columns: repeat(4, 1fr); } }
  @media (max-width: 768px)  { .unknown-grid { grid-template-columns: repeat(2, 1fr); } }
  .unknown-card {
    background: var(--bg-inset);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 14px 10px;
    transition: border-color 0.15s ease, background 0.15s ease;
  }
  .unknown-card:hover {
    border-color: var(--border-strong);
    background: var(--bg-surface);
  }
  .unknown-img {
    width: 84px;
    height: 84px;
    border-radius: 50%;
    object-fit: cover;
    border: 1px solid var(--border);
    background: var(--bg-surface);
    margin-bottom: 10px;
  }
  .unknown-src  { font-size: 11px; font-weight: 600; color: var(--accent); text-transform: capitalize; }
  .unknown-time { font-size: 11px; color: var(--text-muted); margin-top: 4px; text-align: center; font-family: var(--mono); }
</style>

<script>
  const todayStr = new Date().toISOString().split('T')[0];

  // ── Live clock ──
  function updateClock() {
    const now = new Date();
    const h = now.getHours().toString().padStart(2,'0');
    const m = now.getMinutes().toString().padStart(2,'0');
    const s = now.getSeconds().toString().padStart(2,'0');
    const el = document.getElementById('clock');
    if (el) el.textContent = `${h}:${m}:${s}`;
  }
  setInterval(updateClock, 1000);
  updateClock();

  // ── Today date display ──
  const todayDateEl = document.getElementById('today-date');
  if (todayDateEl) {
    const today = new Date();
    todayDateEl.textContent = today.toLocaleDateString('en-IN', { weekday:'long', day:'numeric', month:'long', year:'numeric' });
  }

  // ── Tab switching ──
  const CAMERA_NAMES = [
    { name: 'camera_1',    label: 'Live Tracking Camera',    role: 'office'  }
  ];

  let cameraFeedsLoaded = false;

  function buildCameraGrid() {
    const grid = document.getElementById('camera-grid');
    if (!grid) return;
    grid.innerHTML = CAMERA_NAMES.map(c => {
      const roleClass = `role-${c.role}`;
      const roleLabel = c.role.charAt(0).toUpperCase() + c.role.slice(1);
      return `
        <div class="cam-tile" id="tile-${c.name}">
          <div class="cam-tile-header">
            <div class="cam-tile-name">
              <span>📷 ${c.label}</span>
              <span class="cam-role-chip ${roleClass}">${roleLabel}</span>
            </div>
            <div class="cam-tile-actions">
              <button class="cam-action-btn" onclick="takeSnapshot('${c.name}', '${c.label}')">⬇ Snapshot</button>
              <button class="cam-action-btn" onclick="openFullscreen('${c.name}', '${c.label}')">⛶ Fullscreen</button>
            </div>
          </div>
          <div class="cam-feed-wrap">
            <img class="cam-feed" id="feed-${c.name}"
                 src="/video_feed/${c.name}"
                 alt="${c.label}"
                 onerror="this.style.display='none';document.getElementById('overlay-${c.name}').style.display='flex'">
            <div class="cam-feed-overlay" id="overlay-${c.name}" style="display:none;">
              <span class="offline-icon">📷</span>
              <span>Camera Offline</span>
              <button class="cam-action-btn" onclick="reloadFeed('${c.name}')">Retry</button>
            </div>
          </div>
          <div class="cam-status-bar">
            <span class="cam-status-live"><span class="pulse-dot"></span> LIVE</span>
            <span>${c.name.replace(/_/g,' ').toUpperCase()}</span>
          </div>
        </div>`;
    }).join('');
  }

  function reloadFeed(name) {
    const img = document.getElementById(`feed-${name}`);
    const overlay = document.getElementById(`overlay-${name}`);
    if (img) {
      img.style.display = 'block';
      overlay.style.display = 'none';
      img.src = `/video_feed/${name}?t=${Date.now()}`;
    }
  }

  function takeSnapshot(name, label) {
    const img = document.getElementById(`feed-${name}`);
    if (!img || img.style.display === 'none') {
      alert('Camera is offline.'); return;
    }
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth || 960;
    canvas.height = img.naturalHeight || 540;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/jpeg', 0.9);
    a.download = `snapshot_${name}_${new Date().toISOString().replace(/[:.]/g,'-')}.jpg`;
    a.click();
  }

  function openFullscreen(name, label) {
    const overlay = document.getElementById('fs-overlay');
    const fsImg   = document.getElementById('fs-img');
    const fsTitle = document.getElementById('fs-title');
    fsImg.src   = `/video_feed/${name}`;
    fsTitle.textContent = label;
    overlay.classList.add('open');
  }

  function closeFullscreen() {
    const overlay = document.getElementById('fs-overlay');
    const fsImg   = document.getElementById('fs-img');
    overlay.classList.remove('open');
    fsImg.src = '';  // stop the stream
  }

  // Close fullscreen on Escape key
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeFullscreen(); });

  function showTab(id) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    const navTab = document.getElementById('nav-' + id);
    if (navTab) navTab.classList.add('active');
    if (id === 'tab-cameras')    { /* streams auto-start on img load */ }
    if (id === 'tab-live')       loadLive();
    if (id === 'tab-attendance') loadAttendance();
    if (id === 'tab-activity')   loadActivity();
    if (id === 'tab-security')   loadUnknowns();
    if (id === 'tab-enroll')     loadEmployees();
  }

  // ── Camera status badges ──
  function loadCameraStatus() {
    fetch('/api/camera_status')
      .then(r => r.json())
      .then(data => {
        const el = document.getElementById('cam-badges');
        if (!data.length) {
          el.innerHTML = '<div class="cam-badge"><div class="dot dot-yellow"></div><span>No camera data</span></div>';
          return;
        }
        el.innerHTML = data.map(c => {
          const online = c.status === 'online';
          const cls = online ? 'dot-green' : 'dot-red';
          const label = c.camera_name.replace('camera_', '').replace(/_/g, ' ');
          const statusText = online ? 'Online' : 'Offline';
          return `<div class="cam-badge"><div class="dot ${cls}"></div><span>${label.charAt(0).toUpperCase()+label.slice(1)}: ${statusText}</span></div>`;
        }).join('');
      }).catch(() => {
        const el = document.getElementById('cam-badges');
        if (el) el.innerHTML = '<div class="cam-badge"><div class="dot dot-red"></div><span>Status Error</span></div>';
      });
  }

  // ── Activity label helper ──
  function actLabel(v) {
    if (!v) return '—';
    const map = {
      'working_with_laptop': '💻 Working',
      'on_call':             '📞 On Call',
      'in_meeting':          '🤝 Meeting',
      'idle':                '💤 Idle',
      'active':              '🚶 Active',
      'not_visible':         '👻 Not Visible',
    };
    return map[v] || v.split('_').map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
  }

  function actBadge(v) {
    const map = {
      'working_with_laptop': 'badge-green',
      'on_call':             'badge-blue',
      'in_meeting':          'badge-purple',
      'idle':                'badge-yellow',
      'active':              'badge-orange',
      'not_visible':         'badge-gray',
    };
    const cls = map[v] || 'badge-gray';
    return `<span class="badge ${cls}">${actLabel(v)}</span>`;
  }

  function statusBadge(s) {
    const map = {
      'Complete':  'badge-green',
      'In office': 'badge-blue',
      'Short':     'badge-yellow',
      'Absent':    'badge-gray',
    };
    return `<span class="badge ${map[s]||'badge-gray'}">${s}</span>`;
  }

  function camPill(cam) {
    if (!cam) return '<span class="cam-pill">—</span>';
    const short = cam.replace('camera_', 'CAM ').toUpperCase();
    return `<span class="cam-pill">📷 ${short}</span>`;
  }

  function timeSince(ts) {
    if (!ts) return '—';
    const diff = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
    if (diff < 60)   return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
    return `${Math.floor(diff/3600)}h ago`;
  }

  function durationSince(ts) {
    if (!ts) return '—';
    const diff = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    return h > 0 ? `${h}h ${m.toString().padStart(2,'0')}m` : `${m}m`;
  }

  // ── Live Tracking ──
  function loadLive() {
    const tbody = document.getElementById('live-body');
    fetch('/api/live_tracking')
      .then(r => r.json())
      .then(data => {
        document.getElementById('live-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();

        let present = data.length;
        let working = data.filter(r => r.current_activity === 'working_with_laptop').length;
        let idle    = data.filter(r => ['idle','on_call'].includes(r.current_activity)).length;
        let verified = data.filter(r => r.face_verified).length;

        document.getElementById('live-present').textContent  = present;
        document.getElementById('live-working').textContent  = working;
        document.getElementById('live-idle').textContent     = idle;
        document.getElementById('live-verified').textContent = verified;

        if (!data.length) {
          tbody.innerHTML = `<tr><td colspan="7" class="empty">No employees currently being tracked.</td></tr>`;
          return;
        }

        tbody.innerHTML = data.map(row => `
          <tr>
            <td class="name">${row.display_name}</td>
            <td class="mono">${row.global_id}</td>
            <td>${row.face_verified ? '<span class="badge badge-green">Present</span>' : '<span class="badge badge-blue">Unconfirmed</span>'}</td>
            <td>${camPill(row.current_camera)}</td>
            <td>${actBadge(row.current_activity)}</td>
            <td class="mono">${row.entry_time ? row.entry_time.slice(11,16) : '—'}</td>
            <td class="mono">${row.last_seen ? row.last_seen.slice(11,16) : '—'}</td>
          </tr>
        `).join('');
      })
      .catch(() => {
        tbody.innerHTML = `<tr><td colspan="7" class="empty">No employees currently being tracked.</td></tr>`;
      });
  }

  // ── Attendance ──
  function loadAttendance() {
    const d = document.getElementById('att-date').value || todayStr;
    const tbody = document.getElementById('att-body');
    tbody.innerHTML = `<tr><td colspan="6" class="empty">Loading…</td></tr>`;

    const ep = d === todayStr ? '/api/today' : `/api/date/${d}`;
    fetch(ep).then(r => r.json()).then(data => {
      const present  = data.filter(r => r.status !== 'Absent').length;
      const complete = data.filter(r => r.status === 'Complete').length;
      const short_   = data.filter(r => r.status === 'Short').length;
      const inoff    = data.filter(r => r.status === 'In office').length;

      document.getElementById('att-present').textContent  = present;
      document.getElementById('att-complete').textContent = complete;
      document.getElementById('att-short').textContent    = short_;
      document.getElementById('att-inoffice').textContent = inoff;

      if (!data.length) {
        tbody.innerHTML = `<tr><td colspan="4" class="empty">No records for ${d}.</td></tr>`;
        return;
      }
      tbody.innerHTML = data.map(row => `
        <tr>
          <td class="name">${row.name}</td>
          <td class="mono">${row.entry}</td>
          <td class="mono">${row.exit}</td>
          <td>${statusBadge(row.status)}</td>
        </tr>
      `).join('');
    }).catch(() => { tbody.innerHTML = `<tr><td colspan="4" class="empty">Error loading data.</td></tr>`; });
  }

  function exportAttendance() {
    const s = document.getElementById('exp-start').value;
    const e = document.getElementById('exp-end').value;
    if (!s || !e) { alert('Please set start and end dates.'); return; }
    window.location.href = `/api/export?start=${s}&end=${e}`;
  }

  // ── Activity ──
  function loadActivity() {
    const d = document.getElementById('act-date').value || todayStr;
    const tbody = document.getElementById('act-body');
    tbody.innerHTML = `<tr><td colspan="7" class="empty">Loading…</td></tr>`;

    fetch(`/api/tracking_activity?date=${d}`)
      .then(r => r.json())
      .then(data => {
        document.getElementById('act-date-display').textContent = d === todayStr ? 'Today' : d;
        document.getElementById('act-count').textContent = data.length;
        const avg = data.length
          ? Math.round(data.reduce((s,r) => s + (r.working_pct||0), 0) / data.length)
          : 0;
        document.getElementById('act-avg').textContent = avg + '%';

        // Verified count from live presence
        fetch('/api/live_tracking').then(r=>r.json()).then(p => {
          document.getElementById('act-verified').textContent =
            p.filter(x=>x.face_verified).length;
        }).catch(()=>{});

        if (!data.length) {
          tbody.innerHTML = `<tr><td colspan="7" class="empty">No activity data for ${d}.</td></tr>`;
          return;
        }
        tbody.innerHTML = data.map(row => `
          <tr>
            <td class="name">${row.display_name}</td>
            <td class="mono" style="color:var(--green)">${row.working_time}</td>
            <td class="mono" style="color:var(--yellow)">${row.idle_time}</td>
            <td class="mono" style="color:var(--orange)">${row.active_time}</td>
            <td class="mono">${row.total_time}</td>
            <td>
              <div style="display:flex;align-items:center;gap:8px;">
                <div class="meter"><div class="meter-fill" style="width:${row.working_pct}%"></div></div>
                <span class="mono">${row.working_pct}%</span>
              </div>
            </td>
            <td class="mono" style="color:var(--muted)">${row.last_seen ? row.last_seen.slice(11,16) : '—'}</td>
          </tr>
        `).join('');
      }).catch(() => { tbody.innerHTML = `<tr><td colspan="7" class="empty">Error.</td></tr>`; });
  }

  function exportActivity() {
    const s = document.getElementById('act-exp-start').value;
    const e = document.getElementById('act-exp-end').value;
    if (!s || !e) { alert('Please set start and end dates.'); return; }
    window.location.href = `/api/export_activity?start=${s}&end=${e}`;
  }

  // ── Monthly ──
  function loadMonthly() {
    const m = document.getElementById('month-select').value;
    if (!m) { alert('Select a month.'); return; }
    const tbody = document.getElementById('monthly-body');
    tbody.innerHTML = `<tr><td colspan="4" class="empty">Loading…</td></tr>`;
    fetch(`/api/monthly_report?month=${m}`)
      .then(r => r.json())
      .then(data => {
        if (!data.length) {
          tbody.innerHTML = `<tr><td colspan="4" class="empty">No data for ${m}.</td></tr>`;
          return;
        }
        tbody.innerHTML = data.map(row => `
          <tr>
            <td class="name">${row.name}</td>
            <td class="mono">${row.present_days}</td>
            <td class="mono">${row.total_days}</td>
            <td>
              <span class="mono" style="color:${parseFloat(row.percentage)>=80?'var(--green)':'var(--yellow)'}; font-weight:600;">
                ${row.percentage}
              </span>
            </td>
          </tr>
        `).join('');
      }).catch(() => { tbody.innerHTML = `<tr><td colspan="4" class="empty">Error.</td></tr>`; });
  }

  function exportMonthly() {
    const m = document.getElementById('month-select').value;
    if (!m) { alert('Select a month first.'); return; }
    window.location.href = `/api/export_monthly?month=${m}`;
  }

  // ── Security / Unknowns ──
  function loadUnknowns() {
    const container = document.getElementById('unknowns-container');
    fetch('/api/recent_unknown')
      .then(r => r.json())
      .then(data => {
        if (!data.length) {
          container.innerHTML = `<p style="color:var(--muted);font-style:italic;">No unrecognized detections recorded.</p>`;
          return;
        }
        container.innerHTML = `<div class="unknown-grid">` +
          data.map(item => {
            const [dp, tp] = item.timestamp.split(' ');
            return `
              <div class="unknown-card">
                <img src="/logs/unknown/${item.image_name}" class="unknown-img"
                     onerror="this.src='https://placehold.co/100x100?text=Face'" />
                <div class="unknown-src">${item.camera_source.replace(/_/g,' ')}</div>
                <div class="unknown-time">${dp}<br><b>${(tp||'').slice(0,5)}</b></div>
              </div>`;
          }).join('') + `</div>`;
      }).catch(() => {
        container.innerHTML = `<p style="color:var(--muted);">Error loading security logs.</p>`;
      });
  }

  // ── Enroll Employee ──
  let webcamStream = null;
  let capturedImages = [];

  function startWebcam() {
    const video = document.getElementById('webcam-video');
    const btnStart = document.getElementById('btn-start-cam');
    const btnCapture = document.getElementById('btn-capture');
    
    navigator.mediaDevices.getUserMedia({ video: true })
      .then(stream => {
        webcamStream = stream;
        video.srcObject = stream;
        video.style.display = 'block';
        btnStart.style.display = 'none';
        btnCapture.style.display = 'block';
      })
      .catch(err => {
        alert("Camera access denied or unavailable. Please ensure you are using HTTPS and allow camera access.");
      });
  }

  function capturePhoto() {
    if (capturedImages.length >= 3) {
      alert("Maximum 3 photos allowed.");
      return;
    }
    const video = document.getElementById('webcam-video');
    const canvas = document.getElementById('webcam-canvas');
    const preview = document.getElementById('captured-preview');
    
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    const dataUrl = canvas.toDataURL('image/jpeg');
    
    capturedImages.push(dataUrl);
    
    const img = document.createElement('img');
    img.src = dataUrl;
    img.style.height = '60px';
    img.style.borderRadius = '4px';
    img.style.border = '1px solid var(--border)';
    preview.appendChild(img);
  }

  function submitEnrollment() {
    const name = document.getElementById('enroll-name').value.trim();
    const files = document.getElementById('enroll-files').files;
    const status = document.getElementById('enroll-status');
    
    if (!name) { status.textContent = "Please enter a name."; status.style.color = "red"; return; }
    
    const totalPhotos = files.length + capturedImages.length;
    if (totalPhotos === 0) { status.textContent = "Please capture or upload at least 1 photo."; status.style.color = "red"; return; }
    if (totalPhotos > 3) { status.textContent = "Maximum 3 photos allowed in total."; status.style.color = "red"; return; }
    
    status.textContent = "Processing images...";
    status.style.color = "var(--text-muted)";
    
    const promises = Array.from(files).map(file => {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = e => resolve(e.target.result);
        reader.onerror = e => reject(e);
        reader.readAsDataURL(file);
      });
    });
    
    Promise.all(promises).then(base64Files => {
      const allImages = base64Files.concat(capturedImages);
      
      status.textContent = "Sending to cloud queue...";
      fetch('/api/enroll_employee', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ employee_name: name, images: allImages })
      })
      .then(r => r.json())
      .then(res => {
        if (res.success) {
          status.textContent = "Success! The photos are queued. The Office PC will process them shortly.";
          status.style.color = "var(--success)";
          document.getElementById('enroll-name').value = '';
          document.getElementById('enroll-files').value = '';
          capturedImages = [];
          document.getElementById('captured-preview').innerHTML = '';
          if (webcamStream) {
            webcamStream.getTracks().forEach(t => t.stop());
            document.getElementById('webcam-video').style.display = 'none';
            document.getElementById('btn-start-cam').style.display = 'block';
            document.getElementById('btn-capture').style.display = 'none';
            webcamStream = null;
          }
        } else {
          status.textContent = "Error: " + res.error;
          status.style.color = "red";
        }
      }).catch(err => {
        status.textContent = "Network error occurred.";
        status.style.color = "red";
      });
    });
  }

  // ── Manage Employees ──
  function loadEmployees() {
    const container = document.getElementById('employee-list-container');
    container.innerHTML = `<p style="color:var(--text-muted);font-style:italic;">Loading employees...</p>`;
    fetch('/api/list_employees')
      .then(r => r.json())
      .then(data => {
        if (!data || data.length === 0) {
          container.innerHTML = `<p style="color:var(--text-muted);font-style:italic;">No employees registered.</p>`;
          return;
        }
        let html = `<table style="width:100%; border-collapse: collapse; margin-top: 8px;">`;
        html += `<tr style="border-bottom: 2px solid var(--border); text-align: left;"><th style="padding: 8px;">Name</th><th style="padding: 8px;">Employee ID</th><th style="padding: 8px;">Images</th><th style="padding: 8px;">Action</th></tr>`;
        data.forEach(emp => {
          html += `<tr style="border-bottom: 1px solid var(--border);">`;
          html += `<td style="padding: 8px; font-weight: 600;">${emp.employee_name}</td>`;
          html += `<td style="padding: 8px; color: var(--text-muted); font-family: monospace; font-size: 12px;">${emp.employee_id}</td>`;
          html += `<td style="padding: 8px; color: var(--text-muted);">${emp.image_count}</td>`;
          html += `<td style="padding: 8px;"><button style="background: var(--danger); color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600;" onclick="removeEmployee('${emp.employee_id}', '${emp.employee_name.replace(/'/g, "\\'")}')">Remove</button></td>`;
          html += `</tr>`;
        });
        html += `</table>`;
        container.innerHTML = html;
      })
      .catch(err => {
        container.innerHTML = `<p style="color:red;">Error loading employee list.</p>`;
      });
  }

  function removeEmployee(empId, empName) {
    if (!confirm(`Are you sure you want to completely remove ${empName}? This will delete their face data and cannot be undone.`)) {
      return;
    }
    fetch(`/api/remove_employee/${empId}`, { method: 'DELETE' })
      .then(r => r.json())
      .then(res => {
        if (res.success) {
          alert(`${empName} has been successfully removed.`);
          loadEmployees();
        } else {
          alert(`Error removing employee: ${res.error || 'Unknown error'}`);
        }
      })
      .catch(err => {
        alert("Network error while trying to remove employee.");
      });
  }

  // ── Bootstrap ──
  const today = new Date().toISOString().slice(0,7);
  document.getElementById('att-date').value       = todayStr;
  document.getElementById('exp-start').value      = todayStr;
  document.getElementById('exp-end').value        = todayStr;
  document.getElementById('act-date').value       = todayStr;
  document.getElementById('act-exp-start').value  = todayStr;
  document.getElementById('act-exp-end').value    = todayStr;
  document.getElementById('month-select').value   = today;

  buildCameraGrid();    // build camera tiles
  loadCameraStatus();
  loadLive();
  loadAttendance();

  // Auto-refresh live tab every 5 seconds, camera status every 10s
  setInterval(() => {
    if (document.getElementById('tab-live').classList.contains('active')) {
      loadLive();
    }
  }, 5000);
  setInterval(loadCameraStatus, 10000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/today")
def api_today():
    return jsonify(get_today_summary())


@app.route("/api/date/<date_str>")
def api_date(date_str):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400
    rows = get_date_summary(date_str)
    result = []
    for row in rows:
        entry = row.get("first_entry")
        exit_ = row.get("last_exit")
        hours = row.get("hours_worked")
        if entry:
            entry = datetime.strptime(entry, "%Y-%m-%d %H:%M:%S").strftime("%I:%M %p")
        if exit_:
            exit_ = datetime.strptime(exit_, "%Y-%m-%d %H:%M:%S").strftime("%I:%M %p")
        if hours:
            h = int(hours); m = int(round((hours - h) * 60))
            if m == 60: h += 1; m = 0
            hours_str = f"{h}h {m:02d}m"
        else:
            hours_str = "—"
        result.append({
            "name": row["employee_name"],
            "entry": entry or "—",
            "exit": exit_ or "—",
            "hours": hours_str,
            "status": row["status"] or "Absent",
            "breakdown": row.get("session_breakdown") or "—",
        })
    return jsonify(result)


@app.route("/api/live_tracking")
def api_live_tracking():
    """Real-time presence data from tracking_db."""
    return jsonify(get_live_presence())


@app.route("/api/tracking_history/<global_id>")
def api_tracking_history(global_id):
    d = request.args.get("date") or strftime_today()
    return jsonify(get_tracking_history(global_id, d))


@app.route("/api/tracking_activity")
def api_tracking_activity():
    """Activity summary from tracking_db (per-employee, by global_id)."""
    target_date = request.args.get("date") or strftime_today()
    return jsonify(get_activity_summary_tracking(target_date))


@app.route("/api/export")
def api_export():
    start = request.args.get("start")
    end   = request.args.get("end")
    if not start or not end:
        return "Missing start or end date", 400
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False,
                                      prefix=f"attendance_{start}_to_{end}_")
    tmp.close()
    export_to_excel(start, end, tmp.name)
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"Attendance_{start}_to_{end}.xlsx")


@app.route("/api/camera_status")
def api_camera_status():
    return jsonify(get_camera_status())


@app.route("/api/monthly_report")
def api_monthly_report():
    month = request.args.get("month")
    if not month:
        return jsonify({"error": "Missing month"}), 400
    return jsonify(get_monthly_report(month))


@app.route("/api/export_monthly")
def api_export_monthly():
    month = request.args.get("month")
    if not month:
        return "Missing month", 400
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_monthly_to_excel(month, tmp.name)
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"Attendance_Monthly_{month}.xlsx")


@app.route("/api/recent_unknown")
def api_recent_unknown():
    return jsonify(get_recent_unknowns(12))


@app.route("/api/activity_summary")
def api_activity_summary():
    """Legacy endpoint — keeps Phase 2 activity_db data available."""
    target_date = request.args.get("date") or strftime_today()
    return jsonify(get_activity_summary(target_date))


@app.route("/api/recent_activity")
def api_recent_activity():
    return jsonify(get_recent_activity(25))


@app.route("/api/export_activity")
def api_export_activity():
    start = request.args.get("start")
    end   = request.args.get("end")
    if not start or not end:
        return "Missing dates", 400
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_activity_to_excel(start, end, tmp.name)
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"Work_Activity_{start}_to_{end}.xlsx")


@app.route("/api/captured_frames")
def api_captured_frames():
    from attendance_db import get_db
    db = get_db()
    docs = list(db.captured_frames.find({}, {"_id": 0}).sort("timestamp", -1).limit(50))
    # Convert datetime object to string
    for d in docs:
        if "timestamp" in d and hasattr(d["timestamp"], "isoformat"):
            d["timestamp"] = d["timestamp"].isoformat()
    return jsonify(docs)


@app.route("/api/list_employees")
def api_list_employees():
    try:
        from employee_db import EmployeeDB
        db = EmployeeDB()
        emps = db.list_employees()
        return jsonify(emps)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/remove_employee/<emp_id>", methods=["DELETE"])
def api_remove_employee(emp_id):
    try:
        from employee_db import EmployeeDB
        db = EmployeeDB()
        success = db.delete(emp_id)
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Employee not found."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/enroll_employee", methods=["POST"])
def api_enroll_employee():
    data = request.json
    employee_name = data.get("employee_name")
    images = data.get("images", [])
    if not employee_name or not images:
        return jsonify({"success": False, "error": "Missing name or images"}), 400
        
    employee_id = employee_name.lower().replace(" ", "_")
    from attendance_db import get_db
    from datetime import datetime, timezone
    db = get_db()
    
    # Insert into the pending queue for the local Office PC to process
    db.pending_enrollments.insert_one({
        "employee_id": employee_id,
        "employee_name": employee_name,
        "images": images,
        "status": "pending",
        "timestamp": datetime.now(timezone.utc)
    })
    
    return jsonify({"success": True})


@app.route("/logs/unknown/<path:filename>")
def serve_unknown_image(filename):
    root_dir    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    unknown_dir = os.path.join(root_dir, "logs", "unknown")
    return send_from_directory(unknown_dir, filename)


@app.route("/video_feed/<camera_name>")
def video_feed(camera_name):
    """MJPEG stream for a single camera. Starts the capture thread on first request."""
    # Validate name against known cameras
    known = {c["name"] for c in CAMERA_CONFIG}
    if camera_name not in known:
        return "Unknown camera", 404
    stream = _get_or_start_stream(camera_name)
    if stream is None:
        return "Camera not configured", 404
    return Response(
        _mjpeg_generator(stream),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


@app.route("/api/camera_list")
def api_camera_list():
    """Returns the list of configured cameras for the frontend."""
    return jsonify([{"name": c["name"], "role": c["role"]} for c in CAMERA_CONFIG])


if __name__ == "__main__":
    print("\n  Dashboard running at:  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
