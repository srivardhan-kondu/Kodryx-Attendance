# =============================================================
#  tools/webcam_test.py  —  live webcam recognition + liveness test
#
#  Run in YOUR terminal (needs camera access + a GUI window):
#      python tools/webcam_test.py
#
#  For each detected face it shows the attendance decision:
#    GREEN  "NAME  conf | REAL"   -> would be marked present
#    RED    "SPOOF  REAL=0.03"    -> photo/screen, rejected
#    YELLOW "Unknown  conf"       -> live face, not enrolled
#
#  This is a VIEWER only — it does NOT write to the database.
#  Press 'q' to quit.
# =============================================================

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from face_engine import FaceEngine, load_employee_database
from anti_spoofing import AntiSpoofing
from config import (EMBEDDINGS_FILE, CONFIDENCE_THRESHOLD,
                    MIN_FACE_SIZE, FACE_MIN_DET_SCORE)


def main():
    print("[TEST] Loading models (first run may take a moment)...")
    engine = FaceEngine()
    spoof = AntiSpoofing()
    if not engine.available():
        print("[TEST] FaceEngine unavailable. Aborting.")
        return

    # Load enrolled faces. Try the live DB (MongoDB) first; if empty/offline
    # this falls back to enrollment/embeddings.pkl automatically.
    db = load_employee_database()
    if not db:
        db = load_employee_database(EMBEDDINGS_FILE)
    print(f"[TEST] {len(db)} employee(s) enrolled. Liveness available: {spoof.available()}")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[TEST] Could not open webcam 0.")
        return

    print("[TEST] Running. Show your face, then hold up a phone/photo to test spoof. 'q' = quit.")
    while True:
        ok, frame = cap.read()
        if not ok:
            break

        for face in engine.detect_and_embed(frame):
            if face["det_score"] < FACE_MIN_DET_SCORE:
                continue
            x1, y1, x2, y2 = [int(v) for v in face["box"]]
            if (x2 - x1) < MIN_FACE_SIZE or (y2 - y1) < MIN_FACE_SIZE:
                continue

            is_real, live_score = spoof.check_liveness(frame, face["box"])
            if not is_real:
                color, label = (0, 0, 255), f"SPOOF  real={live_score:.2f}"
            else:
                emp_id, name, conf = engine.match(face["embedding"], db)
                if emp_id is None:
                    color = (0, 200, 255)
                    label = f"Unknown  {conf:.2f}"
                else:
                    color = (0, 200, 0)
                    label = f"{name}  {conf:.2f} | REAL {live_score:.2f}"

            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, label, (x1, max(22, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

        cv2.putText(frame, f"thresh: match>={CONFIDENCE_THRESHOLD:.2f}  (q=quit)",
                    (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
        cv2.imshow("Attendance webcam test", frame)
        if (cv2.waitKey(1) & 0xFF) == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
