# =============================================================
#  dashboard/app.py  —  JSON API backend (client-server)
#
#  Frontend is a separate static site (../frontend) deployed to
#  Vercel; this backend (Render) exposes only JSON + the scan API.
#  For local/single-host use it also serves the static frontend at /.
#
#  CORS is enabled so the Vercel frontend can call this backend.
# =============================================================

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, send_file, request, send_from_directory

from attendance_db import (
    get_today_summary, get_date_summary, export_to_excel,
    get_monthly_report, export_monthly_to_excel,
    snapshot_daily_backup, get_daily_backup,
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

app = Flask(__name__)


# ---------------------------------------------------------------
# CORS — let the separately-deployed frontend call this backend.
# ---------------------------------------------------------------
@app.before_request
def _preflight():
    if request.method == "OPTIONS":
        return ("", 204)


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


# ---------------------------------------------------------------
# Static frontend (local / single-host convenience)
# ---------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    full = os.path.join(FRONTEND_DIR, filename)
    if os.path.isfile(full):
        return send_from_directory(FRONTEND_DIR, filename)
    return ("Not found", 404)


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True})


@app.route("/api/backup/<date_str>")
def api_backup_get(date_str):
    """Return the consolidated day-wise backup document for a date."""
    doc = get_daily_backup(date_str)
    if not doc:
        return jsonify({"work_date": date_str, "total_present": 0, "records": []})
    return jsonify(doc)


@app.route("/api/backup/<date_str>", methods=["POST"])
def api_backup_rebuild(date_str):
    """Manually (re)build the backup snapshot for a date from daily_summary."""
    count = snapshot_daily_backup(date_str)
    return jsonify({"success": True, "work_date": date_str, "records": count})


@app.route("/api/config")
def api_config():
    """Expose the live attendance rules so the UI can explain them accurately."""
    from config import (
        ATTENDANCE_SPLIT_HOUR, EXIT_MIN_GAP_HOURS, COOLDOWN_MINUTES,
        OFFICE_START_HOUR, OFFICE_END_HOUR,
    )
    return jsonify({
        "split_hour": ATTENDANCE_SPLIT_HOUR,
        "exit_min_gap_hours": EXIT_MIN_GAP_HOURS,
        "cooldown_minutes": COOLDOWN_MINUTES,
        "office_start_hour": OFFICE_START_HOUR,
        "office_end_hour": OFFICE_END_HOUR,
    })


# ---------------------------------------------------------------
# Attendance reads
# ---------------------------------------------------------------
@app.route("/api/today")
def api_today():
    return jsonify(get_today_summary())


@app.route("/api/date/<date_str>")
def api_date(date_str):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400
    result = []
    for row in get_date_summary(date_str):
        entry = row.get("first_seen")
        exit_ = row.get("last_seen")
        if entry:
            entry = datetime.strptime(entry, "%Y-%m-%d %H:%M:%S").strftime("%I:%M %p")
        if exit_:
            exit_ = datetime.strptime(exit_, "%Y-%m-%d %H:%M:%S").strftime("%I:%M %p")
        result.append({
            "name": row.get("employee_name"),
            "entry": entry or "—",
            "exit": exit_ or "—",
            "status": row.get("status") or "Absent",
        })
    return jsonify(result)


@app.route("/api/monthly_report")
def api_monthly_report():
    month = request.args.get("month")
    if not month:
        return jsonify({"error": "Missing month"}), 400
    return jsonify(get_monthly_report(month))


@app.route("/api/export")
def api_export():
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return "Missing start or end date", 400
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_to_excel(start, end, tmp.name)
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"Attendance_{start}_to_{end}.xlsx")


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


# ---------------------------------------------------------------
# Employees
# ---------------------------------------------------------------
@app.route("/api/list_employees")
def api_list_employees():
    try:
        from employee_db import EmployeeDB
        return jsonify(EmployeeDB().list_employees())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/remove_employee/<emp_id>", methods=["DELETE"])
def api_remove_employee(emp_id):
    try:
        from employee_db import EmployeeDB
        if EmployeeDB().delete(emp_id):
            try:
                from scan_service import reload_employees
                reload_employees()
            except Exception:
                pass
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Employee not found."})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/enroll_employee", methods=["POST"])
def api_enroll_employee():
    data = request.get_json(silent=True) or {}
    name = data.get("employee_name")
    images = data.get("images", [])
    if not name or not images:
        return jsonify({"success": False, "error": "Missing name or images"}), 400
    try:
        from scan_service import enroll_employee
        result = enroll_employee(name, images)
        return jsonify(result), (200 if result.get("success") else 400)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------
# Browser-camera attendance scan
# ---------------------------------------------------------------
@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(silent=True) or {}
    img = data.get("image")
    if not img:
        return jsonify({"status": "error", "message": "no image"}), 400
    try:
        from scan_service import scan_frame
        return jsonify(scan_frame(img))
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/reload_db", methods=["POST"])
def api_reload_db():
    try:
        from scan_service import reload_employees
        return jsonify({"success": True, "employees": reload_employees()})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
