# =============================================================
#  tools/calibrate_liveness.py  —  one-time liveness calibration
#
#  Run this ONCE on the actual kiosk PC + webcam to confirm the
#  MiniFASNet liveness model separates a real face from a photo.
#
#      python tools/calibrate_liveness.py
#
#  What to do:
#    1. Sit in front of the webcam  -> the number should be HIGH (green).
#    2. Hold up a phone/printed photo of a face
#                                   -> the number should be LOW (red).
#
#  If it is BACKWARDS (real face low, photo high), press 'b' to flip
#  the colour order, re-test, and once correct set
#  ANTI_SPOOF_USE_BGR in config.py to the value shown in the title bar.
#
#  Tune LIVENESS_THRESHOLD in config.py so it sits comfortably between
#  your real-face scores and your photo scores.  Press 'q' to quit.
# =============================================================

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from face_engine import FaceEngine
from anti_spoofing import AntiSpoofing
from config import LIVENESS_THRESHOLD


def main():
    engine = FaceEngine()
    spoof = AntiSpoofing()

    if not engine.available():
        print("[CAL] FaceEngine unavailable (InsightFace not installed?). Aborting.")
        return
    if not spoof.available():
        print("[CAL] Liveness model unavailable — check ANTI_SPOOF_MODEL_PATH. Aborting.")
        return

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[CAL] Could not open webcam 0.")
        return

    print("[CAL] Running. real face = HIGH, photo = LOW. 'b'=flip color, 'q'=quit.")
    while True:
        ok, frame = cap.read()
        if not ok:
            break

        for face in engine.detect_and_embed(frame):
            x1, y1, x2, y2 = [int(v) for v in face["box"]]
            prob = spoof.score(frame, face["box"])
            if prob is None:
                continue
            is_real = prob >= LIVENESS_THRESHOLD
            color = (0, 200, 0) if is_real else (0, 0, 255)
            label = f"{'REAL' if is_real else 'FAKE'} {prob:.2f}"
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, label, (x1, max(20, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

        cv2.putText(frame,
                    f"USE_BGR={spoof._use_bgr}  thresh={LIVENESS_THRESHOLD:.2f}  (b=flip, q=quit)",
                    (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
        cv2.imshow("Liveness calibration", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord("b"):
            spoof._use_bgr = not spoof._use_bgr
            print(f"[CAL] Flipped colour order -> USE_BGR={spoof._use_bgr}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
