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
import hmac
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, send_file, request, send_from_directory, Response

from attendance_db import (
    get_today_summary, get_date_summary, export_to_excel,
    get_monthly_report, export_monthly_to_excel,
    snapshot_daily_backup, get_daily_backup, correct_attendance,
    CorrectionError,
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
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


# ---------------------------------------------------------------
# Authentication (HTTP Basic) — protects the page AND every /api
# endpoint before the app is exposed to the internet (e.g. via a
# Tailscale Funnel / Cloudflare tunnel).
#
# Enabled only when ADMIN_PASSWORD is set, so local development on
# localhost stays friction-free. SET IT before exposing publicly:
#     ADMIN_USER=admin  ADMIN_PASSWORD=your-secret  python serve.py
# ---------------------------------------------------------------
AUTH_USER = os.environ.get("ADMIN_USER", "admin")
AUTH_PASSWORD = os.environ.get("ADMIN_PASSWORD")
_PUBLIC_PATHS = {"/api/health"}

if not AUTH_PASSWORD:
    print("[AUTH] ⚠  ADMIN_PASSWORD not set — the dashboard is UNPROTECTED. "
          "Set ADMIN_PASSWORD before exposing it to the internet.")


@app.before_request
def _require_auth():
    if not AUTH_PASSWORD:                     # auth disabled (local dev)
        return
    if request.method == "OPTIONS" or request.path in _PUBLIC_PATHS:
        return
    auth = request.authorization
    ok = (auth is not None
          and auth.username == AUTH_USER
          and hmac.compare_digest(auth.password or "", AUTH_PASSWORD))
    if not ok:
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": 'Basic realm="Kodryx Attendance"'},
        )


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


@app.route("/api/correct", methods=["POST"])
def api_correct():
    """HR manual correction of a person's entry/exit for a date."""
    data = request.get_json(silent=True) or {}
    emp_id = data.get("employee_id")
    work_date = data.get("work_date")
    if not emp_id or not work_date:
        return jsonify({"success": False, "error": "employee_id and work_date are required."}), 400
    try:
        rec = correct_attendance(
            emp_id, work_date,
            entry_time=data.get("entry_time"),
            exit_time=data.get("exit_time"),
        )
    except CorrectionError as e:
        # Bad/rejected times (e.g. entry after exit) — show HR the reason.
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "record": rec})


@app.route("/api/note", methods=["POST"])
def api_note():
    """HR adds/updates/clears an optional note for a person on a date
    (e.g. the reason they were on leave). Works even if there's no scan."""
    data = request.get_json(silent=True) or {}
    emp_id = data.get("employee_id")
    work_date = data.get("work_date")
    if not emp_id or not work_date:
        return jsonify({"success": False, "error": "employee_id and work_date are required."}), 400
    from attendance_db import set_attendance_note
    note = set_attendance_note(emp_id, work_date, data.get("note", ""))
    return jsonify({"success": True, "note": note})


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


@app.route("/api/mode")
def api_mode():
    """Tell the UI whether it's running as a protected admin dashboard.
    When a password is set (the cloud HR site), the edit controls appear
    automatically after login — no secret URL needed. On the public kiosk
    (no password) they stay hidden unless the secret hash is used."""
    return jsonify({"admin": bool(AUTH_PASSWORD)})


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
    # get_date_summary already returns fully-formatted rows
    # (name / entry / exit / hours / status), same shape as /api/today.
    return jsonify(get_date_summary(date_str))


@app.route("/api/monthly_report")
def api_monthly_report():
    month = request.args.get("month")
    if not month:
        return jsonify({"error": "Missing month"}), 400
    return jsonify(get_monthly_report(month))


@app.route("/api/weekly_averages")
def api_weekly_averages():
    """Company-wide weekly averages (all employees) for ?month=YYYY-MM."""
    month = request.args.get("month")
    if not month:
        return jsonify({"error": "Missing month"}), 400
    from attendance_db import get_weekly_averages
    return jsonify(get_weekly_averages(month))


@app.route("/api/employee/<emp_id>/daily/<month>")
def api_employee_daily(emp_id, month):
    """Get employee's daily attendance for a month."""
    from attendance_db import get_employee_daily
    return jsonify(get_employee_daily(emp_id, month))


@app.route("/api/employee/<emp_id>/weekly/<month>")
def api_employee_weekly(emp_id, month):
    """Get employee's weekly attendance for a month."""
    from attendance_db import get_employee_weekly
    return jsonify(get_employee_weekly(emp_id, month))


@app.route("/api/employee/<emp_id>/monthly/<month>")
def api_employee_monthly(emp_id, month):
    """Get employee's monthly summary for a month."""
    from attendance_db import get_employee_monthly
    result = get_employee_monthly(emp_id, month)
    if not result:
        return jsonify({"error": "No records found"}), 404
    return jsonify(result)


@app.route("/api/employee/<emp_id>/monthly_breakdown")
def api_employee_monthly_breakdown(emp_id):
    """One summary row per month across the employee's entire history."""
    from attendance_db import get_employee_monthly_breakdown
    return jsonify(get_employee_monthly_breakdown(emp_id))


@app.route("/api/employee/<emp_id>/export/<month>")
def api_employee_export(emp_id, month):
    """Download one employee's attendance as Excel. month can be 'all'."""
    from attendance_db import export_employee_to_excel
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_employee_to_excel(emp_id, month, tmp.name)
    tag = "all-time" if str(month).lower() == "all" else month
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"{emp_id}_{tag}_attendance.xlsx")


@app.route("/api/export_all/<month>")
def api_export_all(month):
    """Master Excel of ALL employees. month can be 'all' for entire history."""
    from attendance_db import export_all_employees_to_excel
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_all_employees_to_excel(tmp.name, month)
    tag = "all-time" if str(month).lower() == "all" else month
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"All_Employees_{tag}_attendance.xlsx")


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


@app.route("/api/rename_employee/<emp_id>", methods=["POST"])
def api_rename_employee(emp_id):
    """Rename an employee — no face re-processing needed, safe on the
    lightweight cloud deployment (no InsightFace required)."""
    data = request.get_json(silent=True) or {}
    new_name = (data.get("employee_name") or "").strip()
    if not new_name:
        return jsonify({"success": False, "error": "Name is required."}), 400
    try:
        from employee_db import EmployeeDB
        db = EmployeeDB()
        if not db.employee_exists(emp_id):
            return jsonify({"success": False, "error": "Employee not found."}), 404
        db.collection.update_one({"employee_id": emp_id}, {"$set": {"employee_name": new_name}})
        # Keep past + future attendance rows showing the new name too.
        from attendance_db import get_db as _get_attendance_db
        adb = _get_attendance_db()
        adb.daily_summary.update_many({"employee_id": emp_id}, {"$set": {"employee_name": new_name}})
        adb.daily_backup.update_many(
            {"records.employee_id": emp_id},
            {"$set": {"records.$[r].employee_name": new_name}},
            array_filters=[{"r.employee_id": emp_id}],
        )
        try:
            from scan_service import reload_employees
            reload_employees()
        except Exception:
            pass
        return jsonify({"success": True, "employee_id": emp_id, "employee_name": new_name})
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
    except ImportError:
        return jsonify({
            "success": False,
            "error": "Adding new employees requires face-recognition processing, "
                     "which isn't available on this deployment. Please add new "
                     "employees from the office kiosk instead.",
        }), 501
    try:
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
