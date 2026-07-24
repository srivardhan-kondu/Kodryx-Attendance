# =============================================================
#  attendance_db.py  —  All database operations
#  Creates and manages the attendance database via PyMongo.
# =============================================================

import os
import calendar
from datetime import datetime, date
import pandas as pd
import pickle
from pymongo import MongoClient

from tz_utils import strftime_today, strftime_now, now_ist
from config import (
    EMBEDDINGS_FILE,
    ATTENDANCE_SPLIT_HOUR, EXIT_MIN_GAP_HOURS, MONGO_URI,
)

_client = None
_db = None


# ---------------------------------------------------------------
# Working-day policy (effective July 2026):
#   * Monday–Saturday are working days
#   * Sunday is off
#   * Every 2nd Saturday of the month is a holiday
# ---------------------------------------------------------------
def is_working_day(work_date) -> bool:
    """work_date: 'YYYY-MM-DD' string or a date/datetime."""
    if isinstance(work_date, str):
        d = datetime.strptime(work_date, "%Y-%m-%d").date()
    elif isinstance(work_date, datetime):
        d = work_date.date()
    else:
        d = work_date
    wd = d.weekday()            # Mon=0 … Sun=6
    if wd == 6:                 # Sunday off
        return False
    if wd == 5:                 # Saturday: 2nd Saturday (days 8–14) is a holiday
        if (d.day - 1) // 7 == 1:
            return False
    return True


def working_days_in_month(month_str: str) -> int:
    """Count working days in a 'YYYY-MM' month per the policy above."""
    y, m = (int(x) for x in month_str.split("-"))
    last = calendar.monthrange(y, m)[1]
    return sum(1 for day in range(1, last + 1) if is_working_day(date(y, m, day)))


def _hours_between(start_str, end_str):
    """Hours between two 'YYYY-MM-DD HH:MM:SS' strings (>= 0.0)."""
    if not start_str or not end_str:
        return 0.0
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        delta = datetime.strptime(end_str, fmt) - datetime.strptime(start_str, fmt)
        return max(0.0, delta.total_seconds() / 3600.0)
    except Exception:
        return 0.0


def get_db():
    global _client, _db
    if _db is None:
        _client = MongoClient(MONGO_URI)
        db_name = "attendance_db"
        try:
            parsed = MONGO_URI.split("?")[-2].split("/")[-1]
            if parsed and parsed not in ["localhost:27017"]:
                db_name = parsed
        except:
            pass
        _db = _client[db_name]
    return _db


def initialise_database():
    """Create indexes for MongoDB collections."""
    db = get_db()
    db.attendance_events.create_index([("employee_id", 1), ("work_date", 1)])
    db.daily_summary.create_index([("employee_id", 1), ("work_date", 1)], unique=True)
    db.daily_backup.create_index("work_date", unique=True)
    print("[DB] Database initialised successfully via PyMongo.")


def snapshot_daily_backup(work_date=None):
    """
    Consolidate a whole day's attendance into a single backup document in the
    `daily_backup` collection (one doc per date). Called automatically after
    every entry/exit so the archive always mirrors the live `daily_summary`,
    and can also be run manually to rebuild a day.

    Returns the number of employee records in the snapshot.
    """
    db = get_db()
    work_date = work_date or strftime_today()

    rows = list(db.daily_summary.find({"work_date": work_date}, {"_id": 0}))
    records = [{
        "employee_id":   r.get("employee_id"),
        "employee_name": r.get("employee_name"),
        "entry":         r.get("first_seen"),
        "exit":          r.get("last_seen"),
        "status":        r.get("status"),
        "hours_worked":  r.get("hours_worked", 0.0),
    } for r in rows]

    db.daily_backup.update_one(
        {"work_date": work_date},
        {"$set": {
            "work_date":     work_date,
            "updated_at":    strftime_now(),
            "total_present": len(records),
            "records":       records,
        }},
        upsert=True,
    )
    return len(records)


def get_daily_backup(work_date: str):
    """Return the consolidated backup document for a given date (or None)."""
    return get_db().daily_backup.find_one({"work_date": work_date}, {"_id": 0})


def log_event(employee_id, employee_name, event_type, confidence, camera_source):
    event_time = strftime_now()
    work_date  = strftime_today()

    get_db().attendance_events.insert_one({
        "employee_id": employee_id,
        "employee_name": employee_name,
        "event_type": event_type,
        "event_time": event_time,
        "work_date": work_date,
        "camera_source": camera_source,
        "confidence": confidence
    })

    print(f"[DB] Logged: {employee_name} | {event_type.upper()} | {event_time} | confidence: {confidence:.2f}")


def log_presence_event(employee_id, employee_name, confidence, camera_source, frame_b64=None):
    """
    Time-based entry/exit (one camera can't tell arriving from leaving, so the
    clock decides):
      • Scan BEFORE  ATTENDANCE_SPLIT_HOUR  -> ENTRY (login). first_seen set once.
      • Scan AT/AFTER ATTENDANCE_SPLIT_HOUR -> EXIT  (logout). last_seen = the
        latest evening scan; it stays blank until a real evening checkout, so
        entry and exit are never the same fake value.
      • Morning re-scans are ignored; an evening scan within EXIT_MIN_GAP_HOURS
        of entry is treated as lingering and ignored too.
    """
    event_time = strftime_now()
    work_date  = strftime_today()
    now = now_ist()
    is_evening = (now.hour + now.minute / 60.0) >= ATTENDANCE_SPLIT_HOUR
    db = get_db()

    doc = db.daily_summary.find_one({"employee_id": employee_id, "work_date": work_date})

    if doc is None:
        # First scan of the day = entry. Exit (last_seen) stays empty until a
        # genuine evening checkout.
        db.daily_summary.insert_one({
            "employee_id": employee_id,
            "employee_name": employee_name,
            "work_date": work_date,
            "first_seen": event_time,
            "last_seen": None,
            "status": "In office",
            "hours_worked": 0.0,
            "session_breakdown": None,
        })
        print(f"[DB] Entry (login): {employee_name} | {event_time}")
        snapshot_daily_backup(work_date)
        return

    # Already checked in. Morning re-scans never count as an exit.
    if not is_evening:
        return

    # Evening scan = checkout. Ignore lingering right after entry.
    hours = _hours_between(doc.get("first_seen"), event_time)
    if hours < EXIT_MIN_GAP_HOURS:
        return

    db.daily_summary.update_one(
        {"_id": doc["_id"]},
        {"$set": {"last_seen": event_time, "hours_worked": hours, "status": "Complete"}},
    )
    print(f"[DB] Exit (logout): {employee_name} | {event_time} | {hours:.2f}h")
    snapshot_daily_backup(work_date)


def get_last_event_type_today(employee_id):
    today = strftime_today()
    db = get_db()
    doc = db.attendance_events.find_one(
        {"employee_id": employee_id, "work_date": today},
        sort=[("event_time", -1)]
    )
    return doc["event_type"] if doc else None


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _date_fields(work_date):
    """Weekday + dd-mm-yyyy + weekend/holiday flags for a 'YYYY-MM-DD' string.
    Computed here (server-side) so the dashboard never has to re-derive the
    weekday from a string and risk an off-by-one — this is the single source
    of truth, the same parse is_working_day() uses."""
    if not work_date:
        return {"weekday": "", "weekday_short": "", "date_dmy": "",
                "is_weekend": False, "is_holiday": False}
    try:
        d = datetime.strptime(work_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return {"weekday": "", "weekday_short": "", "date_dmy": work_date,
                "is_weekend": False, "is_holiday": False}
    return {
        "weekday":       d.strftime("%A"),        # Monday … Sunday
        "weekday_short": d.strftime("%a"),        # Mon … Sun
        "date_dmy":      d.strftime("%d-%m-%Y"),  # e.g. 25-07-2026
        "is_weekend":    d.weekday() >= 5,        # Sat(5) or Sun(6)
        "is_holiday":    not is_working_day(d),   # Sunday or 2nd Saturday
    }


def _summary_row(row):
    """Format one daily_summary doc for the dashboard, including the
    employee_id + raw 24h times needed by the HR correction editor."""
    fdt = _parse_dt(row.get("first_seen"))
    ldt = _parse_dt(row.get("last_seen"))
    work_date = row.get("work_date")
    return {
        "employee_id": row.get("employee_id"),
        "name":        row.get("employee_name"),
        "work_date":   work_date,
        "entry":       fdt.strftime("%I:%M %p") if fdt else "—",
        "exit":        ldt.strftime("%I:%M %p") if ldt else "—",
        "entry_raw":   fdt.strftime("%H:%M") if fdt else "",
        "exit_raw":    ldt.strftime("%H:%M") if ldt else "",
        "hours":       round(float(row.get("hours_worked") or 0.0), 2),
        "status":      row.get("status", "Absent"),
        **_date_fields(work_date),
    }


def _summaries_for(target_date):
    db = get_db()
    docs = list(db.daily_summary.find({"work_date": target_date}))
    docs.sort(key=lambda r: r.get("first_seen") or "9999-99-99 99:99:99")
    today = strftime_today()

    rows = []
    for d in docs:
        row = _summary_row(d)
        # Forgot to log off on a PAST day -> just FLAG it "Auto logout" for HR.
        # Do NOT estimate an exit or count hours; HR edits the real timing.
        if (target_date < today and d.get("first_seen") and not d.get("last_seen")
                and row["status"] == "In office"):
            row["status"] = "Auto logout"
            row["auto"] = True
        rows.append(row)

    # Absence rule: on any working day up to today, EVERY enrolled employee
    # starts Absent by default and flips to present only once they have a scan
    # record. So the Absent count = registered employees − present, and it drops
    # live through the day as people arrive. (Future dates are not filled, and
    # non-working days — Sunday / 2nd Saturday — are holidays: nobody is Absent.)
    if target_date <= today and is_working_day(target_date):
        present_ids = {r.get("employee_id") for r in docs}
        absentees = [
            {"employee_id": eid, "name": name, "work_date": target_date,
             "entry": "—", "exit": "—", "entry_raw": "", "exit_raw": "",
             "hours": 0.0, "status": "Absent"}
            for eid, name in get_registered_employees().items()
            if eid not in present_ids
        ]
        absentees.sort(key=lambda r: (r["name"] or "").lower())
        rows.extend(absentees)

    # Attach HR notes (optional per-person, per-day remark, e.g. leave reason).
    notes = _notes_for(target_date)
    for r in rows:
        r["note"] = notes.get(r.get("employee_id"), "")
    return rows


# ---------------------------------------------------------------
# HR notes — optional per-person, per-day remark (e.g. leave reason).
# Stored separately so a note can attach to a day with no scan record.
# ---------------------------------------------------------------
def _notes_for(target_date):
    db = get_db()
    return {
        n["employee_id"]: n.get("note", "")
        for n in db.attendance_notes.find({"work_date": target_date}, {"_id": 0})
    }


def set_attendance_note(employee_id, work_date, note):
    """Add/update (or clear, if blank) an HR note for a person on a date."""
    db = get_db()
    note = (note or "").strip()
    if note:
        db.attendance_notes.update_one(
            {"employee_id": employee_id, "work_date": work_date},
            {"$set": {"note": note, "updated_at": strftime_now()}},
            upsert=True,
        )
    else:
        db.attendance_notes.delete_one(
            {"employee_id": employee_id, "work_date": work_date})
    return note


def get_today_summary():
    return _summaries_for(strftime_today())


def get_date_summary(target_date: str):
    return _summaries_for(target_date)


def _employee_name(employee_id):
    """Best-effort display name for an id: their most recent recorded name,
    else the id itself. Used when HR adds attendance for a day that has no
    existing record to copy the name from."""
    db = get_db()
    prev = (db.daily_summary.find({"employee_id": employee_id}, {"employee_name": 1})
            .sort("work_date", -1).limit(1))
    for p in prev:
        if p.get("employee_name"):
            return p["employee_name"]
    return employee_id


class CorrectionError(ValueError):
    """HR correction rejected for a reason worth showing the user verbatim."""


def correct_attendance(employee_id, work_date, entry_time=None, exit_time=None):
    """
    HR manual correction of a person's entry/exit for a given date.
    `entry_time` / `exit_time` are 'HH:MM' (24h) strings:
      • a time string  -> set that time (combined with work_date)
      • empty string '' -> clear it (exit '' => back to 'In office')
      • None            -> leave unchanged
    If no record exists yet (e.g. the person was Absent), one is CREATED as
    long as an entry time is given — so HR can fill in a missed day.
    Recomputes hours_worked + status and refreshes the daily backup.
    Returns the updated summary row. Raises CorrectionError with a
    user-facing message if the requested times are invalid.
    """
    db = get_db()
    doc = db.daily_summary.find_one({"employee_id": employee_id, "work_date": work_date})

    def _mk(t):
        t = (t or "").strip()
        if not t:
            return None
        if len(t) == 5:            # 'HH:MM' -> add seconds
            t = t + ":00"
        return f"{work_date} {t}"

    # Start from the existing times, then apply whatever HR changed.
    first_seen = doc.get("first_seen") if doc else None
    last_seen  = doc.get("last_seen") if doc else None
    if entry_time is not None and entry_time.strip() != "":
        first_seen = _mk(entry_time)
    if exit_time is not None:
        last_seen = _mk(exit_time)             # '' -> None (cleared)

    # Guard: entry must come before exit. Previously this silently clamped to
    # 0.00 hours and still saved "Complete", so a correction looked like it
    # "didn't work" (time stuck at 0:00). Now we tell HR exactly what's wrong.
    if first_seen and last_seen:
        fdt, ldt = _parse_dt(first_seen), _parse_dt(last_seen)
        if fdt and ldt and fdt >= ldt:
            raise CorrectionError(
                f"Entry ({fdt.strftime('%I:%M %p')}) must be before "
                f"exit ({ldt.strftime('%I:%M %p')}). Fix the entry time too.")

    # Nothing exists yet and no entry supplied -> nothing to record.
    if not doc and not first_seen:
        raise CorrectionError(
            "This person has no record for that day. Enter an entry time to add one.")

    update = {"first_seen": first_seen, "last_seen": last_seen}
    if last_seen:
        update["status"] = "Complete"
        update["hours_worked"] = round(_hours_between(first_seen, last_seen), 2)
    else:
        update["status"] = "In office"
        update["hours_worked"] = 0.0
    if not doc:                                # creating a fresh record
        update["employee_id"]   = employee_id
        update["employee_name"] = _employee_name(employee_id)
        update["work_date"]     = work_date
        update["session_breakdown"] = None
        update["hr_created"]    = True         # marker: added by HR, not scanned

    db.daily_summary.update_one(
        {"employee_id": employee_id, "work_date": work_date},
        {"$set": update}, upsert=True,
    )
    snapshot_daily_backup(work_date)
    updated = db.daily_summary.find_one({"employee_id": employee_id, "work_date": work_date})
    return _summary_row(updated)


def export_to_excel(start_date: str, end_date: str, filepath: str):
    db = get_db()
    docs = list(db.daily_summary.find({
        "work_date": {"$gte": start_date, "$lte": end_date}
    }).sort([("work_date", 1), ("employee_name", 1)]))

    formatted_docs = []
    for d in docs:
        formatted_docs.append({
            "Employee": d.get("employee_name"),
            "Date": d.get("work_date"),
            "First Seen": d.get("first_seen"),
            "Last Seen": d.get("last_seen"),
            "Status": d.get("status")
        })

    if not formatted_docs:
        df = pd.DataFrame(columns=["Employee", "Date", "First Seen", "Last Seen", "Status"])
    else:
        df = pd.DataFrame(formatted_docs)

        for col in ["First Seen", "Last Seen"]:
            if col in df.columns:
                df[col] = df[col].apply(lambda x: datetime.strptime(x, "%Y-%m-%d %H:%M:%S").strftime("%I:%M %p") if pd.notna(x) and x != "" and x is not None else "—")

    df.to_excel(filepath, index=False)
    print(f"[DB] Exported to {filepath}")
    return filepath


def get_registered_employees():
    from employee_db import EmployeeDB
    try:
        emp_db = EmployeeDB()
        emps = emp_db.get_all()
        if emps:
            return {emp_id: data["name"] for emp_id, data in emps.items()}
    except Exception as e:
        print(f"[DB] Could not load from EmployeeDB: {e}")

    try:
        with open(EMBEDDINGS_FILE, "rb") as f:
            data = pickle.load(f)
        return {emp_id: emp_data["name"] for emp_id, emp_data in data.items()}
    except Exception:
        return {}


def _avg_time_of_day(dt_strings):
    """Average clock time (ignoring the date) across 'YYYY-MM-DD HH:MM:SS'
    values, returned as '10:58 AM' — or '' if there are none. Averages the
    seconds-since-midnight, so it reflects the typical arrival/leaving time."""
    secs = []
    for s in dt_strings:
        d = _parse_dt(s)
        if d:
            secs.append(d.hour * 3600 + d.minute * 60 + d.second)
    if not secs:
        return ""
    avg = int(round(sum(secs) / len(secs)))
    h, m = (avg // 3600) % 24, (avg % 3600) // 60
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12:02d}:{m:02d} {ap}"


def get_monthly_report(month_str: str):
    employees = get_registered_employees()
    report = []
    db = get_db()

    present_statuses = {'Complete', 'In office', 'Late Entry',
                        'Early Exit', 'Late & Early', 'Present'}
    working_days = working_days_in_month(month_str)   # Mon–Sat minus 2nd Saturday

    for emp_id, emp_name in employees.items():
        docs = list(db.daily_summary.find(
            {"employee_id": emp_id, "work_date": {"$regex": f"^{month_str}-"}},
            {"_id": 0, "status": 1, "hours_worked": 1,
             "first_seen": 1, "last_seen": 1},
        ))
        present_days = sum(1 for d in docs if d.get("status") in present_statuses)
        # Forgotten logouts have 0 stored hours and are NOT estimated — they
        # stay uncounted until HR edits the real timing.
        total_hours = round(sum(float(d.get("hours_worked") or 0.0) for d in docs), 1)

        pct = (present_days / working_days) * 100 if working_days else 0.0
        pct = min(100.0, round(pct, 1))

        # Per-employee averages HR asked for: typical shift length and the
        # typical clock-in / clock-out times, over the days they were present.
        hour_vals = [float(d.get("hours_worked") or 0.0) for d in docs
                     if d.get("status") in present_statuses and (d.get("hours_worked") or 0)]
        avg_hours = round(sum(hour_vals) / len(hour_vals), 2) if hour_vals else 0.0
        avg_in  = _avg_time_of_day(d.get("first_seen") for d in docs)
        avg_out = _avg_time_of_day(d.get("last_seen") for d in docs)

        report.append({
            "employee_id": emp_id,
            "name": emp_name,
            "present_days": present_days,
            "total_days": working_days,
            "total_hours": total_hours,
            "percentage": f"{pct}%",
            "avg_hours_per_day": avg_hours,
            "avg_in": avg_in,      # "10:58 AM" or ""
            "avg_out": avg_out,    # "06:52 PM" or ""
        })

    report.sort(key=lambda x: float(x["percentage"].replace("%", "")), reverse=True)
    return report


def get_weekly_averages(month_str: str):
    """Company-wide weekly averages for a month (YYYY-MM).

    For each Mon–Sun week that has activity in the month, returns the average
    hours and average days-present PER EMPLOYEE across the whole team, plus the
    raw totals. 'Per employee' divides by the enrolled head-count so the number
    is comparable week to week even if some people never scanned that week.
    """
    from datetime import timedelta
    db = get_db()
    docs = list(db.daily_summary.find(
        {"work_date": {"$regex": f"^{month_str}-"}},
        {"_id": 0, "work_date": 1, "status": 1, "hours_worked": 1},
    ))

    # Head-count = enrolled employees (stable denominator). Fall back to the
    # number of distinct people seen this month if the roster can't be read.
    try:
        from employee_db import EmployeeDB
        head = len(EmployeeDB().get_all())
    except Exception:
        head = 0
    head = head or len({d["work_date"] for d in docs}) or 1

    weeks = {}
    for d in docs:
        try:
            dt = datetime.strptime(d["work_date"], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        monday = dt - timedelta(days=dt.weekday())        # week starts Monday
        w = weeks.setdefault(monday, {"hours": 0.0, "present": 0})
        w["hours"] += float(d.get("hours_worked") or 0.0)
        if d.get("status") in ("Complete", "In office"):
            w["present"] += 1

    out = []
    for monday in sorted(weeks):
        w = weeks[monday]
        sunday = monday + timedelta(days=6)
        out.append({
            "week_start": monday.strftime("%Y-%m-%d"),
            "week_end":   sunday.strftime("%Y-%m-%d"),
            "label":      f"{monday.strftime('%d %b')} – {sunday.strftime('%d %b')}",
            "employees":  head,
            "total_hours":   round(w["hours"], 2),
            "total_present": w["present"],
            "avg_hours_per_employee": round(w["hours"] / head, 2),
            "avg_days_per_employee":  round(w["present"] / head, 2),
        })
    return out


def _is_all(month_str):
    return not month_str or str(month_str).lower() == "all"


def get_employee_daily(employee_id: str, month_str: str = None):
    """Employee's daily attendance — for one month (YYYY-MM) or, if month_str
    is None/'all', their ENTIRE timesheet. Includes HR notes and the
    Auto-logout flag for past days where they forgot to log off."""
    db = get_db()
    query = {"employee_id": employee_id}
    note_q = {"employee_id": employee_id}
    if not _is_all(month_str):
        query["work_date"] = {"$regex": f"^{month_str}-"}
        note_q["work_date"] = {"$regex": f"^{month_str}-"}
    docs = list(db.daily_summary.find(query, {"_id": 0}).sort("work_date", 1))
    notes = {n["work_date"]: n.get("note", "")
             for n in db.attendance_notes.find(note_q, {"_id": 0})}
    today = strftime_today()

    rows = []
    for d in docs:
        row = _summary_row(d)
        wd = d.get("work_date")
        if (wd and wd < today and d.get("first_seen") and not d.get("last_seen")
                and row["status"] == "In office"):
            row["status"] = "Auto logout"
            row["auto"] = True
        row["note"] = notes.get(wd, "")
        rows.append(row)
    return rows


def get_employee_weekly(employee_id: str, month_str: str = None):
    """Employee's weekly totals — for one month or their entire history."""
    from datetime import datetime
    docs = get_employee_daily(employee_id, month_str)

    weeks = {}
    for row in docs:
        dt = datetime.strptime(row["work_date"], "%Y-%m-%d")
        # ISO week: Monday=0, Sunday=6; week starts Monday
        week_start = dt - __import__("datetime").timedelta(days=dt.weekday())
        week_key = week_start.strftime("%Y-W%U")  # "2026-W27"

        if week_key not in weeks:
            weeks[week_key] = {
                "week": week_key,
                "start_date": week_start.strftime("%Y-%m-%d"),
                "days_present": 0,
                "total_hours": 0.0,
                "days": []
            }

        weeks[week_key]["days"].append(row)
        if row["status"] not in ["Absent", "Leave", "Auto logout"]:
            weeks[week_key]["days_present"] += 1
        weeks[week_key]["total_hours"] += row["hours"]

    return sorted(weeks.values(), key=lambda w: w["start_date"])


def get_employee_monthly(employee_id: str, month_str: str):
    """Get employee's monthly summary for a month."""
    db = get_db()
    docs = list(db.daily_summary.find(
        {"employee_id": employee_id, "work_date": {"$regex": f"^{month_str}-"}},
        {"_id": 0, "status": 1, "hours_worked": 1, "employee_name": 1}
    ))

    if not docs:
        return None

    emp_name = docs[0].get("employee_name", "")
    present_statuses = {"Complete", "In office"}
    present_days = sum(1 for d in docs if d.get("status") in present_statuses)
    total_hours = round(sum(float(d.get("hours_worked") or 0.0) for d in docs), 1)
    working_days = working_days_in_month(month_str)   # Mon–Sat minus 2nd Saturday
    pct = (present_days / working_days) * 100 if working_days else 0.0
    pct = min(100.0, round(pct, 1))

    return {
        "employee_id": employee_id,
        "employee_name": emp_name,
        "month": month_str,
        "present_days": present_days,
        "total_days": working_days,
        "total_hours": total_hours,
        "percentage": f"{pct}%"
    }


def get_employee_monthly_breakdown(employee_id: str):
    """One summary row PER MONTH the employee has any record — used for the
    'entire timesheet' Monthly tab."""
    db = get_db()
    docs = list(db.daily_summary.find(
        {"employee_id": employee_id},
        {"_id": 0, "work_date": 1, "status": 1, "hours_worked": 1},
    ))
    present_statuses = {"Complete", "In office"}
    months = {}
    for d in docs:
        mkey = (d.get("work_date") or "")[:7]
        if not mkey:
            continue
        agg = months.setdefault(mkey, {"present": 0, "hours": 0.0})
        if d.get("status") in present_statuses:
            agg["present"] += 1
        agg["hours"] += float(d.get("hours_worked") or 0.0)

    out = []
    for mkey in sorted(months):
        wd = working_days_in_month(mkey)
        present = months[mkey]["present"]
        pct = min(100.0, round((present / wd) * 100, 1)) if wd else 0.0
        out.append({
            "month": mkey,
            "present_days": present,
            "total_days": wd,
            "total_hours": round(months[mkey]["hours"], 1),
            "percentage": f"{pct}%",
        })
    return out


def _fmt_hm(dec):
    """Decimal hours -> 'Xh Ym'."""
    dec = float(dec or 0.0)
    h = int(dec)
    m = round((dec - h) * 60)
    if m == 60:
        h += 1
        m = 0
    return f"{h}h {m}m"


def export_employee_to_excel(employee_id: str, month_str: str, filepath: str):
    """Export one employee's Daily + Weekly + Monthly attendance to a 3-sheet
    Excel. month_str None/'all' => their entire timesheet."""
    import pandas as pd
    daily = get_employee_daily(employee_id, month_str)
    weekly = get_employee_weekly(employee_id, month_str)

    with pd.ExcelWriter(filepath) as writer:
        # --- Daily sheet (full timesheet, includes any HR note) ---
        if daily:
            df_d = pd.DataFrame([{
                "Date": r["work_date"], "Entry": r["entry"], "Exit": r["exit"],
                "Hours": _fmt_hm(r["hours"]), "Status": r["status"],
                "Note": r.get("note", ""),
            } for r in daily])
        else:
            df_d = pd.DataFrame(columns=["Date", "Entry", "Exit", "Hours", "Status", "Note"])
        df_d.to_excel(writer, sheet_name="Daily", index=False)

        # --- Weekly sheet ---
        if weekly:
            df_w = pd.DataFrame([{
                "Week": w["week"], "Start Date": w["start_date"],
                "Days Present": w["days_present"], "Total Hours": _fmt_hm(w["total_hours"]),
            } for w in weekly])
        else:
            df_w = pd.DataFrame(columns=["Week", "Start Date", "Days Present", "Total Hours"])
        df_w.to_excel(writer, sheet_name="Weekly", index=False)

        # --- Monthly sheet: single month, or a per-month breakdown for all-time ---
        if _is_all(month_str):
            breakdown = get_employee_monthly_breakdown(employee_id)
            rows = [{
                "Month": b["month"], "Present Days": b["present_days"],
                "Working Days": b["total_days"], "Total Hours": _fmt_hm(b["total_hours"]),
                "Attendance %": b["percentage"],
            } for b in breakdown]
            df_m = pd.DataFrame(rows) if rows else pd.DataFrame(
                columns=["Month", "Present Days", "Working Days", "Total Hours", "Attendance %"])
        else:
            monthly = get_employee_monthly(employee_id, month_str)
            if monthly:
                df_m = pd.DataFrame([{
                    "Employee": monthly["employee_name"], "Month": monthly["month"],
                    "Present Days": monthly["present_days"], "Working Days": monthly["total_days"],
                    "Total Hours": _fmt_hm(monthly["total_hours"]), "Attendance %": monthly["percentage"],
                }])
            else:
                df_m = pd.DataFrame(columns=["Employee", "Month", "Present Days",
                                             "Working Days", "Total Hours", "Attendance %"])
        df_m.to_excel(writer, sheet_name="Monthly", index=False)

    return filepath


def export_all_employees_to_excel(filepath: str, month_str: str = None):
    """Master export of EVERY employee. month_str None/'all' => entire history.
    Sheet 1: a Summary row per employee. Sheet 2: all daily records combined."""
    import pandas as pd
    employees = get_registered_employees()
    present_statuses = {"Complete", "In office"}

    summary_rows, all_rows = [], []
    for eid, name in sorted(employees.items(), key=lambda kv: (kv[1] or "").lower()):
        daily = get_employee_daily(eid, month_str)
        present = sum(1 for r in daily if r["status"] in present_statuses)
        hours = round(sum(r["hours"] for r in daily), 1)
        summary_rows.append({
            "Employee": name, "Days Present": present,
            "Total Hours": _fmt_hm(hours), "Records": len(daily),
        })
        for r in daily:
            all_rows.append({
                "Employee": name, "Date": r["work_date"], "Entry": r["entry"],
                "Exit": r["exit"], "Hours": _fmt_hm(r["hours"]),
                "Status": r["status"], "Note": r.get("note", ""),
            })
    all_rows.sort(key=lambda x: (x["Date"], x["Employee"]))

    with pd.ExcelWriter(filepath) as writer:
        (pd.DataFrame(summary_rows) if summary_rows else
         pd.DataFrame(columns=["Employee", "Days Present", "Total Hours", "Records"])
         ).to_excel(writer, sheet_name="Summary", index=False)
        (pd.DataFrame(all_rows) if all_rows else
         pd.DataFrame(columns=["Employee", "Date", "Entry", "Exit", "Hours", "Status", "Note"])
         ).to_excel(writer, sheet_name="All Records", index=False)

    return filepath


def export_monthly_to_excel(month_str: str, filepath: str):
    report_data = get_monthly_report(month_str)
    
    cols = ["Employee ID", "Employee Name", "Present Days",
            "Monthly Working Days", "Total Hours", "Attendance %"]
    if not report_data:
        # If database is completely empty, create an empty sheet with columns
        df = pd.DataFrame(columns=cols)
    else:
        df = pd.DataFrame(report_data)
        df.columns = cols

    df.to_excel(filepath, index=False)
    return filepath


def update_camera_status(camera_name: str, status: str, downtime_start: str = None, last_seen: str = None):
    db = get_db()
    
    update_fields = {"status": status}
    if downtime_start is not None:
        update_fields["downtime_start"] = downtime_start
    if last_seen is not None:
        update_fields["last_seen"] = last_seen
        
    db.camera_status.update_one(
        {"camera_name": camera_name},
        {"$set": update_fields},
        upsert=True
    )


def get_camera_status():
    db = get_db()
    docs = list(db.camera_status.find({}, {"_id": 0}))
    return docs


def log_unknown_detection(timestamp: str, camera_source: str, image_name: str):
    db = get_db()
    db.unknown_detections.insert_one({
        "timestamp": timestamp,
        "camera_source": camera_source,
        "image_name": image_name
    })


def get_recent_unknowns(limit: int = 10):
    db = get_db()
    docs = list(db.unknown_detections.find({}, {"_id": 0}).sort("timestamp", -1).limit(limit))
    return docs
