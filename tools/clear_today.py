"""Clear today's attendance records. Uses MONGO_URI from .env / environment.

    python tools/clear_today.py            # clears today
    python tools/clear_today.py 2026-06-30 # clears a specific date
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from attendance_db import get_db
from tz_utils import strftime_today


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else strftime_today()
    res = get_db().daily_summary.delete_many({"work_date": day})
    print(f"Cleared {res.deleted_count} attendance record(s) for {day}.")


if __name__ == "__main__":
    main()
