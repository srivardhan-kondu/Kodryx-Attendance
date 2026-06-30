# =============================================================
#  camera_processor.py  —  Entry / Exit attendance cameras
#
#  Phase 3 upgrade:
#    • Replaces MTCNN + DeepFace (Facenet512) with InsightFace
#    • Face detection + recognition in a single model call
#    • Entry camera (Camera 1) + Exit camera (Camera 2) only
#    • Office tracking (Camera 3 + 4) is handled by office_tracker.py
#
#  Everything else (cooldowns, DB logging, email alerts) is unchanged.
# =============================================================

import cv2
import threading
import time
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime, timedelta
import base64

from face_engine import FaceEngine, load_employee_database
from config import (
    ATTENDANCE_CAMERA_URLS,
    OFFICE_START_HOUR, OFFICE_START_MINUTE,
    OFFICE_END_HOUR, OFFICE_END_MINUTE,
    FRAME_INTERVAL_SECONDS, CONFIDENCE_THRESHOLD,
    MIN_FACE_SIZE, FACE_MIN_DET_SCORE, COOLDOWN_MINUTES, LOG_FILE,
    UNKNOWN_COOLDOWN_MINUTES, ENABLE_EMAIL_ALERTS,
    ADMIN_EMAIL, SMTP_SERVER, SMTP_PORT,
    EMAIL_USERNAME, EMAIL_PASSWORD
)
from attendance_db import log_presence_event, update_camera_status, log_unknown_detection
from config import ENABLE_ANTI_SPOOFING
try:
    from anti_spoofing import AntiSpoofing
except ImportError:
    AntiSpoofing = None


# ---------------------------------------------------------------
# Logging
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


from tz_utils import now_ist

# ---------------------------------------------------------------
# Office hours check
# ---------------------------------------------------------------
def is_within_office_hours():
    now   = now_ist().time()
    start = now_ist().replace(
        hour=OFFICE_START_HOUR, minute=OFFICE_START_MINUTE, second=0
    ).time()
    end   = now_ist().replace(
        hour=OFFICE_END_HOUR, minute=OFFICE_END_MINUTE, second=0
    ).time()
    return start <= now <= end


# ---------------------------------------------------------------
# Cooldown trackers (unchanged from Phase 2)
# ---------------------------------------------------------------
class CooldownTracker:
    def __init__(self):
        self._lock      = threading.Lock()
        self._last_seen = {}

    def can_log(self, employee_id):
        with self._lock:
            last = self._last_seen.get(employee_id)
            if last is None:
                return True
            return (now_ist() - last) >= timedelta(minutes=COOLDOWN_MINUTES)

    def mark_seen(self, employee_id):
        with self._lock:
            self._last_seen[employee_id] = now_ist()

    def reset_day(self):
        with self._lock:
            self._last_seen.clear()


class UnknownCooldownTracker:
    def __init__(self):
        self._lock      = threading.Lock()
        self._last_seen = {}

    def can_log(self, camera_name):
        with self._lock:
            last = self._last_seen.get(camera_name)
            if last is None:
                return True
            return (now_ist() - last) >= timedelta(minutes=UNKNOWN_COOLDOWN_MINUTES)

    def mark_seen(self, camera_name):
        with self._lock:
            self._last_seen[camera_name] = now_ist()


# ---------------------------------------------------------------
# Email alerts (unchanged)
# ---------------------------------------------------------------
def _send_email_worker(subject, body, image_path):
    try:
        msg = MIMEMultipart()
        msg["From"]    = EMAIL_USERNAME
        msg["To"]      = ADMIN_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                img_data = f.read()
            msg.attach(MIMEImage(img_data, name=os.path.basename(image_path)))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.send_message(msg)
        log.info("[SMTP] Alert sent: %s", subject)
    except Exception as exc:
        log.error("[SMTP] Alert failed: %s", exc)


def send_email_alert_async(subject, body, image_path=None):
    if not ENABLE_EMAIL_ALERTS:
        return
    threading.Thread(
        target=_send_email_worker,
        args=(subject, body, image_path),
        daemon=True
    ).start()


# ---------------------------------------------------------------
# Single camera loop (upgraded to InsightFace)
# ---------------------------------------------------------------
def process_camera(camera_url, camera_name, employee_db,
                   face_engine, cooldown_tracker, unknown_cooldown, stop_event,
                   anti_spoofing=None):
    """
    Read frames from one attendance camera (entry or exit).
    Uses InsightFace for detection + recognition.
    """
    log.info("[%s] Starting attendance camera thread.", camera_name.upper())

    cap              = None
    camera_is_online = True
    last_success_time = now_ist()
    downtime_start   = None

    try:
        update_camera_status(camera_name, "online",
                             last_seen=now_ist().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as exc:
        log.error("[%s] DB status error: %s", camera_name.upper(), exc)

    last_process_time = 0
    last_drawn_boxes = []
    is_processing = False
    
    def ml_task(work_frame, dt_now, ts_now):
        nonlocal last_process_time, last_drawn_boxes, is_processing
        try:
            faces = face_engine.detect_and_embed(work_frame)
            current_drawn = []
            if faces:
                log.info("[%s] %d face(s) detected.", camera_name.upper(), len(faces))
                for face in faces:
                    if face["det_score"] < FACE_MIN_DET_SCORE:
                        continue

                    # Filter out tiny faces in the background
                    fx1, fy1, fx2, fy2 = face["box"]
                    face_w = fx2 - fx1
                    face_h = fy2 - fy1
                    if face_w < MIN_FACE_SIZE or face_h < MIN_FACE_SIZE:
                        continue

                    # Anti-spoofing / liveness check.
                    # Call whenever a checker is configured — check_liveness()
                    # enforces its own fail-open/closed policy internally, so a
                    # failed model load rejects instead of waving people through.
                    if anti_spoofing is not None:
                        is_real, liveness_score = anti_spoofing.check_liveness(work_frame, face["box"])
                        if not is_real:
                            log.warning("[%s] SPOOF DETECTED! Score: %.2f. Rejecting attendance.", camera_name.upper(), liveness_score)
                            current_drawn.append({
                                "box": face["box"],
                                "name": "SPOOF / FAKE",
                                "conf": liveness_score
                            })
                            try:
                                h, w = work_frame.shape[:2]
                                sx1, sy1 = max(0, int(fx1)), max(0, int(fy1))
                                sx2, sy2 = min(w, int(fx2)), min(h, int(fy2))
                                face_crop = work_frame[sy1:sy2, sx1:sx2]
                                if face_crop.size > 0:
                                    ret, buffer = cv2.imencode('.jpg', face_crop, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                                    if ret:
                                        from attendance_db import get_db
                                        get_db().captured_frames.insert_one({
                                            "employee_id": "spoof",
                                            "employee_name": f"SPOOF ({liveness_score:.2f})",
                                            "timestamp": datetime.utcnow(),
                                            "event_time_local": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                                            "camera_source": camera_name,
                                            "frame_b64": base64.b64encode(buffer).decode('utf-8')
                                        })
                            except Exception as e:
                                log.error("[%s] Failed to log spoof frame: %s", camera_name.upper(), e)
                            continue

                    emp_id, emp_name, confidence = face_engine.match(
                        face["embedding"], employee_db
                    )
                    
                    if emp_id is None:
                        continue
                    
                    current_drawn.append({
                        "box": face["box"],
                        "name": emp_name,
                        "conf": confidence
                    })

                    if not cooldown_tracker.can_log(emp_id):
                        continue
                        
                    try:
                        log.info("[%s] MATCH: %s | confidence: %.2f", camera_name.upper(), emp_name, confidence)
                        
                        # Encode the face crop for cloud storage
                        h, w = work_frame.shape[:2]
                        x1, y1, x2, y2 = face["box"]
                        x1, y1 = max(0, x1), max(0, y1)
                        x2, y2 = min(w, x2), min(h, y2)
                        face_crop = work_frame[y1:y2, x1:x2]
                        
                        frame_b64 = None
                        if face_crop.size > 0:
                            ret, buffer = cv2.imencode('.jpg', face_crop, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                            if ret:
                                frame_b64 = base64.b64encode(buffer).decode('utf-8')
                        
                        log_presence_event(emp_id, emp_name, confidence, camera_name, frame_b64=frame_b64)
                        cooldown_tracker.mark_seen(emp_id)
                    except Exception as exc:
                        log.error("[%s] DB log_presence_event error: %s", camera_name.upper(), exc)
                    
            last_drawn_boxes = current_drawn
            last_process_time = ts_now
        finally:
            is_processing = False

    while not stop_event.is_set():

        # ------ office hours gate ------
        if not is_within_office_hours():
            if cap is not None:
                cap.release()
                cap = None
            cooldown_tracker.reset_day()
            try:
                update_camera_status(camera_name, "offline (outside hours)")
            except Exception:
                pass
            time.sleep(60)
            continue

        # ------ open camera ------
        if cap is None or not cap.isOpened():
            cap = cv2.VideoCapture(camera_url)
            if not cap.isOpened():
                if camera_is_online and (now_ist() - last_success_time).total_seconds() > 15:
                    camera_is_online = False
                    downtime_start   = now_ist()
                    try:
                        update_camera_status(
                            camera_name, "offline",
                            downtime_start=downtime_start.strftime("%Y-%m-%d %H:%M:%S")
                        )
                    except Exception:
                        pass
                    send_email_alert_async(
                        f"Camera {camera_name} OFFLINE",
                        f"{camera_name} disconnected at {last_success_time.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                time.sleep(10)
                continue

        # ------ read frame ------
        ret, frame = cap.read()
        if not ret or frame is None:
            if camera_is_online and (now_ist() - last_success_time).total_seconds() > 15:
                camera_is_online = False
                downtime_start   = now_ist()
                try:
                    update_camera_status(
                        camera_name, "offline",
                        downtime_start=downtime_start.strftime("%Y-%m-%d %H:%M:%S")
                    )
                except Exception:
                    pass
            cap.release()
            cap = None
            time.sleep(5)
            continue

        now_dt = now_ist()
        if not camera_is_online:
            camera_is_online = True
            ds = str(now_dt - downtime_start).split(".")[0] if downtime_start else "unknown"
            log.info("[%s] Camera recovered after %s.", camera_name.upper(), ds)
            try:
                update_camera_status(camera_name, "online",
                                     last_seen=now_dt.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception:
                pass
            send_email_alert_async(f"Camera {camera_name} ONLINE",
                                   f"Back online. Downtime: {ds}")
            downtime_start = None
        else:
            try:
                update_camera_status(camera_name, "online",
                                     last_seen=now_dt.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception:
                pass

        last_success_time = now_dt

        # ------ Zero-Lag Frame Processing ------
        now_ts = time.time()
        
        if now_ts - last_process_time >= FRAME_INTERVAL_SECONDS and not is_processing and face_engine.available():
            is_processing = True
            threading.Thread(target=ml_task, args=(frame.copy(), now_dt, now_ts), daemon=True).start()

        # User requested to remove bounding boxes; we still detect and log attendance
        # but do not draw the visual boxes over the frame anymore.
        # for b in last_drawn_boxes:
        #     x1, y1, x2, y2 = b["box"]
        #     color = (0, 255, 0) if b["name"] != "Unknown" else (0, 0, 255)
        #     cv2.rectangle(frame, (x1, y1), (x2, y2), color, 4)
        #     label = f"{b['name']} | {b['conf']:.2f}"
        #     (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        #     cv2.rectangle(frame, (x1, y1 - th - 10), (x1 + tw, y1), color, -1)
        #     cv2.putText(frame, label, (x1, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        _push_frame(camera_name, frame)

    if cap is not None:
        cap.release()
    log.info("[%s] Camera thread stopped.", camera_name.upper())

def _push_frame(camera_name, frame):
    try:
        frame_resized = cv2.resize(frame, (960, 540))
        ok, buf = cv2.imencode('.jpg', frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            import tempfile
            import os
            import uuid
            import time
            temp_path = os.path.join(tempfile.gettempdir(), f"{camera_name}_latest.jpg")
            tmp_path = temp_path + f".{uuid.uuid4().hex[:6]}.tmp"
            with open(tmp_path, "wb") as f:
                f.write(buf.tobytes())
            
            for _ in range(5):
                try:
                    os.replace(tmp_path, temp_path)
                    break
                except PermissionError:
                    time.sleep(0.01)
            else:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------
# AttendanceProcessor
# ---------------------------------------------------------------
class AttendanceProcessor:
    """Manages the presence-based attendance camera threads."""

    def __init__(self):
        # Shared InsightFace engine and employee database
        self._face_engine   = FaceEngine()
        self._employee_db   = load_employee_database()
        self._anti_spoofing = AntiSpoofing() if ENABLE_ANTI_SPOOFING and AntiSpoofing else None
        self._stop_event    = threading.Event()

        self._cooldown         = CooldownTracker()
        self._unknown_cooldown = UnknownCooldownTracker()

        self._camera_threads = []

    def start(self):
        log.info("[SYSTEM] Starting attendance processor (InsightFace)...")

        if not ATTENDANCE_CAMERA_URLS:
            log.warning("[SYSTEM] ATTENDANCE_CAMERA_URLS is empty in config.py. No cameras started.")
        
        for idx, cam_url in enumerate(ATTENDANCE_CAMERA_URLS):
            if cam_url is None:
                continue
                
            cam_name = f"camera_{idx+1}"
            thread = threading.Thread(
                target=process_camera,
                args=(
                    cam_url, cam_name,
                    self._employee_db, self._face_engine,
                    self._cooldown, self._unknown_cooldown, self._stop_event,
                    self._anti_spoofing
                ),
                daemon=True, name=f"PresenceCam_{idx+1}"
            )
            thread.start()
            self._camera_threads.append(thread)
            log.info("[SYSTEM] Presence camera thread started (URL: %s)", cam_url)

        log.info("[SYSTEM] Attendance processor ready.")

    def stop(self):
        log.info("[SYSTEM] Stopping attendance processor...")
        self._stop_event.set()
        for t in self._camera_threads:
            if t:
                t.join(timeout=15)
        log.info("[SYSTEM] Attendance processor stopped.")

    def is_running(self):
        return any(t and t.is_alive() for t in self._camera_threads)
