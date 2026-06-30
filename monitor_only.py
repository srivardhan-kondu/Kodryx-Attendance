import sys
import os
import time
import signal
from tz_utils import now_ist

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from attendance_db import initialise_database
from camera_processor import AttendanceProcessor
from config import EMBEDDINGS_FILE
from employee_db import EmployeeDB
import threading
import base64
import numpy as np
import cv2

def enrollment_worker_loop(attendance_processor):
    from attendance_db import get_db
    db = get_db()
    emp_db = EmployeeDB()
    face_engine = attendance_processor._face_engine
    
    while True:
        try:
            pending = db.pending_enrollments.find_one({"status": "pending"})
            if pending:
                employee_id = pending["employee_id"]
                employee_name = pending["employee_name"]
                b64_images = pending.get("images", [])
                
                embeddings = []
                for b64 in b64_images:
                    try:
                        # Decode base64 to image
                        if b64.startswith("data:image"):
                            b64 = b64.split(",")[1]
                        img_data = base64.b64decode(b64)
                        nparr = np.frombuffer(img_data, np.uint8)
                        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if img is not None:
                            faces = face_engine.detect_and_embed(img)
                            if faces:
                                # take the largest face
                                best_face = max(faces, key=lambda f: (f["box"][2]-f["box"][0])*(f["box"][3]-f["box"][1]))
                                embeddings.append(best_face["embedding"])
                    except Exception as e:
                        print(f"[ENROLLMENT] Error processing image for {employee_name}: {e}")
                
                if embeddings:
                    # Store EACH photo's embedding (shape N x 512), not the
                    # average. match() compares against all and takes the best
                    # cosine similarity, so keeping them separate is strictly
                    # more accurate across different poses/lighting.
                    emb_matrix = np.array(embeddings, dtype=np.float32).reshape(len(embeddings), -1)
                    emp_db.upsert(employee_id, employee_name, emb_matrix, len(embeddings))
                    print(f"\n[ENROLLMENT] Successfully enrolled {employee_name} from cloud queue.\n")
                    # Update the live processor's in-memory db
                    attendance_processor._employee_db = emp_db.get_all()
                else:
                    print(f"\n[ENROLLMENT] Failed to find faces in uploaded photos for {employee_name}.\n")
                    
                # Delete the pending task so it takes up 0 space
                db.pending_enrollments.delete_one({"_id": pending["_id"]})
                
        except Exception as e:
            pass
            
        time.sleep(10)

def check_prerequisites():
    print("\n" + "=" * 60)
    print("  OFFICE ATTENDANCE MONITOR (Phase 3)")
    print("=" * 60)

    db_ok = False
    
    try:
        emp_db = EmployeeDB()
        count = emp_db.count()
        if count > 0:
            print(f"\n[OK] Employee database found ({count} employee(s) enrolled).")
            db_ok = True
        else:
            print("\n[WARNING] employees database exists but has 0 employees enrolled.")
    except Exception as e:
        pass

    if not db_ok:
        # Fallback: check legacy pkl
        if os.path.exists(EMBEDDINGS_FILE):
            print(f"\n[WARNING] employees database empty/missing — using legacy embeddings.pkl.")
            db_ok = True
        else:
            print("\n[ERROR] No employee database found.")
            print("  Primary: data/employees.db (run: python enroll_employees.py)")
            print("  Legacy : enrollment/embeddings.pkl")
            sys.exit(1)

    print("[OK] Prerequisites met.\n")


def main():
    check_prerequisites()

    print("[STEP 1] Initialising databases...")
    initialise_database()

    print("[STEP 2] Starting entry / exit attendance cameras...")
    attendance_processor = AttendanceProcessor()
    attendance_processor.start()

    print("\n" + "=" * 60)
    print("  MONITOR IS RUNNING")
    print("=" * 60)
    print("  Attendance is currently tracking via your camera(s).")
    print("  Background Enrollment Worker is polling the cloud...")
    print("  Press Ctrl+C to stop")
    print("=" * 60 + "\n")
    print("=" * 60 + "\n")

    # Start the distributed enrollment worker thread
    enroll_thread = threading.Thread(
        target=enrollment_worker_loop,
        args=(attendance_processor,),
        daemon=True
    )
    enroll_thread.start()

    def handle_shutdown(sig, frame):
        print("\n\n[SYSTEM] Shutdown signal received. Stopping...")
        attendance_processor.stop()
        print("[SYSTEM] All systems stopped. Goodbye.\n")
        sys.exit(0)

    signal.signal(signal.SIGINT,  handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    while True:
        time.sleep(5)
        now = now_ist()
        if now.second < 5 and now.minute % 5 == 0:
            att_ok   = attendance_processor.is_running()
            status   = "OK" if att_ok else "WARNING"
            print(f"[HEARTBEAT] {now.strftime('%H:%M')} — "
                  f"Attendance={att_ok}  [{status}]")


if __name__ == "__main__":
    main()
