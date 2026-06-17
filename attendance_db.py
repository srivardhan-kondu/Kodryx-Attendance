# =============================================================
#  attendance_db.py  —  All database operations
#  Creates and manages the attendance database via PyMongo.
# =============================================================

import os
from datetime import datetime, date
import pandas as pd
import pickle
from pymongo import MongoClient

from tz_utils import strftime_today, strftime_now
from config import (
    MONTHLY_WORKING_DAYS, EMBEDDINGS_FILE,
    WORKDAY_START_TIME, WORKDAY_END_TIME, TARGET_WORK_HOURS,
    MONGO_URI
)

_client = None
_db = None

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
    db.camera_status.create_index("camera_name", unique=True)
    db.unknown_detections.create_index("timestamp")
    db.captured_frames.create_index("timestamp", expireAfterSeconds=2592000)
    print("[DB] Database initialised successfully via PyMongo.")


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
    event_time = strftime_now()
    work_date  = strftime_today()
    db = get_db()

    # Log the captured frame if provided
    if frame_b64:
        db.captured_frames.insert_one({
            "employee_id": employee_id,
            "employee_name": employee_name,
            "timestamp": datetime.utcnow(),
            "event_time_local": event_time,
            "camera_source": camera_source,
            "frame_b64": frame_b64
        })

    doc = db.daily_summary.find_one({"employee_id": employee_id, "work_date": work_date})

    if doc:
        # Already marked today. Update last_seen.
        db.daily_summary.update_one(
            {"_id": doc["_id"]},
            {"$set": {"last_seen": event_time}}
        )
        print(f"[DB] Presence Updated (Last Seen): {employee_name} | {event_time}")
    else:
        # First detection of the day
        db.daily_summary.insert_one({
            "employee_id": employee_id,
            "employee_name": employee_name,
            "work_date": work_date,
            "first_seen": event_time,
            "last_seen": event_time,
            "status": "Present",
            "hours_worked": 0.0,
            "session_breakdown": None
        })
        print(f"[DB] Attendance Marked (First Seen): {employee_name} | {event_time}")


def get_last_event_type_today(employee_id):
    today = strftime_today()
    db = get_db()
    doc = db.attendance_events.find_one(
        {"employee_id": employee_id, "work_date": today},
        sort=[("event_time", -1)]
    )
    return doc["event_type"] if doc else None


def get_today_summary():
    today = strftime_today()
    db = get_db()
    
    docs = list(db.daily_summary.find({"work_date": today}))
    
    def sort_key(row):
        return row.get("first_seen") or "9999-99-99 99:99:99"
    
    docs = sorted(docs, key=sort_key)

    result = []
    for row in docs:
        first_time = row.get("first_seen")
        last_time  = row.get("last_seen")
        if first_time:
            first_time = datetime.strptime(first_time, "%Y-%m-%d %H:%M:%S").strftime("%I:%M %p")
        if last_time:
            last_time = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S").strftime("%I:%M %p")

        result.append({
            "name":         row.get("employee_name"),
            "entry":        first_time or "—",
            "exit":         last_time or "—",
            "status":       row.get("status", "Absent")
        })

    return result

def get_date_summary(target_date: str):
    db = get_db()
    docs = list(db.daily_summary.find({"work_date": target_date}, {"_id": 0}))
    
    def sort_key(row):
        return row.get("first_seen") or "9999-99-99 99:99:99"
        
    return sorted(docs, key=sort_key)


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


def get_monthly_report(month_str: str):
    employees = get_registered_employees()
    report = []
    db = get_db()
    
    for emp_id, emp_name in employees.items():
        present_days = db.daily_summary.count_documents({
            "employee_id": emp_id,
            "work_date": {"$regex": f"^{month_str}-"},
            "status": {"$in": ['Complete', 'Short', 'In office', 'Late Entry', 'Early Exit', 'Late & Early', 'Present']}
        })
        
        pct = (present_days / MONTHLY_WORKING_DAYS) * 100
        pct = min(100.0, round(pct, 1))
        
        report.append({
            "employee_id": emp_id,
            "name": emp_name,
            "present_days": present_days,
            "total_days": MONTHLY_WORKING_DAYS,
            "percentage": f"{pct}%"
        })
        
    report.sort(key=lambda x: float(x["percentage"].replace("%", "")), reverse=True)
    return report


def export_monthly_to_excel(month_str: str, filepath: str):
    report_data = get_monthly_report(month_str)
    
    if not report_data:
        # If database is completely empty, create an empty sheet with columns
        df = pd.DataFrame(columns=["Employee ID", "Employee Name", "Present Days", "Monthly Working Days", "Attendance %"])
    else:
        df = pd.DataFrame(report_data)
        df.columns = ["Employee ID", "Employee Name", "Present Days", "Monthly Working Days", "Attendance %"]
        
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
