import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# =============================================================
#  config.py  —  All settings for the attendance system
#  Phase 3: Multi-Camera Tracking + ReID + Activity Recognition
# =============================================================

# ---------------------------------------------------------------
# ATTENDANCE CAMERAS
# These cameras are used for presence-based logging (first_seen/last_seen).
# ---------------------------------------------------------------
GLOBAL_FRAME_BUFFER = {}

ATTENDANCE_CAMERA_URLS = [0]

# ---------------------------------------------------------------
OFFICE_CAMERA_A_URL = 0
# OFFICE_CAMERA_A_URL = "rtsp://user:pass@192.168.1.11/stream1"
# OFFICE_CAMERA_B_URL = "rtsp://user:pass@192.168.1.12/stream1"

# ---------------------------------------------------------------
# ALL FOUR CAMERAS — unified config for the tracking engine
# name:     short identifier used in DB + dashboard
# url:      RTSP or integer (webcam)
# role:     'entry' | 'exit' | 'office'
# ---------------------------------------------------------------
CAMERA_CONFIG = [
    {"name": "camera_1", "url": OFFICE_CAMERA_A_URL, "role": "office"},
]

# ---------------------------------------------------------------
# OFFICE HOURS
# ---------------------------------------------------------------
OFFICE_START_HOUR   = 9
OFFICE_START_MINUTE = 0
OFFICE_END_HOUR     = 21
OFFICE_END_MINUTE   = 0

# ---------------------------------------------------------------
# FRAME SAMPLING INTERVALS (seconds)
# ---------------------------------------------------------------
FRAME_INTERVAL_SECONDS          = 1.0    # Changed from 0.2 to prevent GPU OOM crashes
TRACKING_FRAME_INTERVAL_SECONDS = 0.5   # office tracking cameras (faster)
ACTIVITY_FRAME_INTERVAL_SECONDS = 10   # legacy alias kept for compat
ACTIVITY_SAMPLE_DURATION_SECONDS = ACTIVITY_FRAME_INTERVAL_SECONDS

# ---------------------------------------------------------------
# INFERENCE DEVICE  (Forced to CPU per user request)
# ---------------------------------------------------------------
USE_GPU = False

def get_torch_device() -> str:
    """Return 'cpu'."""
    return "cpu"

def get_yolo_device():
    """Device argument for Ultralytics YOLO ('cpu')."""
    return "cpu"

def get_onnx_providers() -> list:
    """ONNX Runtime provider chain for InsightFace."""
    return ["CPUExecutionProvider"]


# ---------------------------------------------------------------
# YOLO  (person detection model)
# Options: yolov8n.pt (fastest) | yolov8s.pt | yolov8m.pt | yolov8l.pt | yolov8x.pt (most accurate)
# ---------------------------------------------------------------
YOLO_MODEL_NAME = "yolov8n.pt"

# ---------------------------------------------------------------
# FACE RECOGNITION  (InsightFace — buffalo_l model)
# ---------------------------------------------------------------
FACE_RECOGNITION_MODEL  = "buffalo_l"   # InsightFace model pack name
CONFIDENCE_THRESHOLD    = 0.48          # minimum cosine similarity for a match (0.48 prevents false positives)
MIN_FACE_SIZE           = 35            # min face width/height in px (ignores far away blurry faces until they get closer)
FACE_DETECTION_INTERVAL = 5             # run face recognition every N frames
                                        # (tracking covers the rest)

# RetinaFace detector tuning — lower values detect more / smaller / farther faces
FACE_DET_SIZE           = (640, 640)    # 640 is plenty for a close-range kiosk and ~2x faster on CPU
FACE_DET_THRESH         = 0.35          # RetinaFace confidence threshold
FACE_MIN_DET_SCORE      = 0.35          # post-filter in camera_processor (must be <= FACE_DET_THRESH)

# YOLO person-crop assist was for far-away faces across multiple cameras.
# A single kiosk webcam sees faces up close, so this is off (saves a whole
# extra YOLO model + a second full face-detection pass per frame).
USE_YOLO_PERSON_FACE_DETECT = False
YOLO_PERSON_CONF          = 0.30        # person detection confidence (lower = catch farther people)
YOLO_PERSON_PADDING       = 0.12        # expand person box before face search
FACE_DEDUP_IOU            = 0.45        # merge duplicate face boxes from full-frame + crop passes

# ---------------------------------------------------------------
# ANTI-SPOOFING / LIVENESS DETECTION  (Silent-Face MiniFASNet V2)
# Passive liveness: rejects printed photos and phone/laptop/TV screens
# by scoring face TEXTURE, not geometry. A real face scores high on the
# "real" class; a photo/screen scores high on a "fake" class.
# ---------------------------------------------------------------
# Both are env-overridable so you can relax liveness for testing on a
# webcam where it over-rejects, WITHOUT editing code (production default
# stays secure):
#   ENABLE_ANTI_SPOOFING=0   -> turn liveness off entirely (demo only)
#   LIVENESS_THRESHOLD=0.30  -> lower the bar (less strict)
ENABLE_ANTI_SPOOFING  = os.environ.get("ENABLE_ANTI_SPOOFING", "1").strip().lower() \
                            not in ("0", "false", "no", "off")
LIVENESS_THRESHOLD    = float(os.environ.get("LIVENESS_THRESHOLD", "0.60"))

ANTI_SPOOF_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "data", "minifasnet_v2.onnx")
ANTI_SPOOF_SCALE      = 2.7             # MiniFASNet V2 crop scale (do NOT change — baked into the model)
ANTI_SPOOF_INPUT_SIZE = 80             # model input is 80x80
ANTI_SPOOF_USE_BGR    = True           # cv2 native order. If calibration shows real faces scoring
                                        # LOW and photos scoring HIGH, flip this to False (RGB).
ANTI_SPOOF_FAIL_OPEN  = False          # If the model can't load: False = reject everyone (secure,
                                        # tamper-proof) ; True = wave everyone through (convenient).

# ---------------------------------------------------------------
# DEDUPLICATION COOLDOWNS
# ---------------------------------------------------------------
COOLDOWN_MINUTES         = 1
UNKNOWN_COOLDOWN_MINUTES = 0

# ---------------------------------------------------------------
# PERSON RE-IDENTIFICATION  (OSNet via torchreid / FastReID)
# ---------------------------------------------------------------
REID_MODEL_NAME        = "osnet_x1_0"   # model architecture
REID_SIMILARITY_THRESH = 0.55           # cosine similarity threshold (OSNet quality)
REID_GALLERY_MAX_AGE   = 14400          # 4 hours before a gallery embedding expires (keeps identity all morning)
REID_OSNET_ONLY        = False          # if True, disable histogram fallback (requires torchreid)

# ---------------------------------------------------------------
# BYTETRACK  (tracker settings)
# ---------------------------------------------------------------
BYTETRACK_TRACK_THRESH   = 0.5
BYTETRACK_TRACK_BUFFER   = 30   # frames before a track is considered lost
BYTETRACK_MATCH_THRESH   = 0.8
BYTETRACK_MIN_BOX_AREA   = 10   # px² minimum bounding box area
BYTETRACK_FRAME_RATE     = 10   # estimated camera FPS fed to ByteTrack

# ---------------------------------------------------------------
# ACTIVITY RECOGNITION
# ---------------------------------------------------------------
ENABLE_ACTIVITY_ANALYSIS      = True
ACTIVITY_CONFIDENCE_THRESHOLD = 0.45

# Idle threshold: if a person hasn't moved (IOU > threshold) for this
# many consecutive frames, mark them as idle.
IDLE_FRAME_COUNT  = 30
IDLE_IOU_THRESH   = 0.85

# ---------------------------------------------------------------
# 8-HOUR WORK DAY TARGET
# Flexible Workday Timings
WORKDAY_START_TIME = "09:00:00"
WORKDAY_END_TIME   = "21:00:00"

# TIME-BASED entry/exit:
#   A scan BEFORE this hour  = morning  -> sets ENTRY (login).
#   A scan AT/AFTER this hour = evening -> sets EXIT (logout, latest wins).
# 13.0 = 1:00 PM. Env-overridable (ATTENDANCE_SPLIT_HOUR).
ATTENDANCE_SPLIT_HOUR = float(os.environ.get("ATTENDANCE_SPLIT_HOUR", "13.0"))

# Guard against lingering: an exit scan only counts if it is at least this
# many hours after entry (stops standing at the camera from moving the exit
# time). Env-overridable; set EXIT_MIN_GAP_HOURS=0.01 (~36s) for a demo.
EXIT_MIN_GAP_HOURS = float(os.environ.get("EXIT_MIN_GAP_HOURS", "1.0"))

MONTHLY_WORKING_DAYS  = 22

# ---------------------------------------------------------------
# EMAIL ALERTS  (optional)
# ---------------------------------------------------------------
ENABLE_EMAIL_ALERTS = False
ADMIN_EMAIL         = "admin@example.com"
SMTP_SERVER         = "smtp.gmail.com"
SMTP_PORT           = 587
EMAIL_USERNAME      = "your_email@gmail.com"
EMAIL_PASSWORD      = "your_app_password"

# ---------------------------------------------------------------
# LEGACY COMPAT — activity_processor.py still imports these
# ---------------------------------------------------------------
OFFICE_CAMERA_URLS = []   # legacy office camera config (unused by new engine)

# ---------------------------------------------------------------
# FILE PATHS
# ---------------------------------------------------------------
import os

BASE_DIR           = os.path.dirname(os.path.abspath(__file__))
EMPLOYEE_DATA_DIR  = os.path.join(BASE_DIR, "enrollment")
DATABASE_PATH      = os.path.join(BASE_DIR, "data", "attendance.db")
LOG_FILE           = os.path.join(BASE_DIR, "logs", "system.log")
EMBEDDINGS_FILE    = os.path.join(BASE_DIR, "enrollment", "embeddings.pkl")
REID_GALLERY_FILE  = os.path.join(BASE_DIR, "enrollment", "reid_gallery.pkl")

# ---------------------------------------------------------------
# LOCAL CAPTURE STORAGE
# Scan snapshots are saved to the local filesystem (NOT MongoDB) as an
# attendance evidence trail, and auto-purged after CAPTURE_RETENTION_DAYS.
# ---------------------------------------------------------------
CAPTURE_DIR            = os.path.join(BASE_DIR, "captures")
CAPTURE_RETENTION_DAYS = int(os.environ.get("CAPTURE_RETENTION_DAYS", "30"))

# ---------------------------------------------------------------
# ABSENCE RULE
# An enrolled employee who has not logged in by this hour (IST) is
# marked Absent for the day. Past days always count missing = Absent.
# ---------------------------------------------------------------
ABSENT_CUTOFF_HOUR = float(os.environ.get("ABSENT_CUTOFF_HOUR", "19.0"))  # 7:00 PM

# ---------------------------------------------------------------
# EMPLOYEE ENROLLMENT DATABASE  (SQLite — Phase 3 upgrade)
# ---------------------------------------------------------------
EMPLOYEE_DB_PATH   = os.path.join(BASE_DIR, "data", "employees.db")

# ---------------------------------------------------------------
# DATABASE CONFIGURATION (MongoDB)
# ---------------------------------------------------------------
MONGO_URI = os.environ.get("MONGO_URI")

if not MONGO_URI:
    # Fallback to local MongoDB
    MONGO_URI = "mongodb://localhost:27017/attendance_db"

# ---------------------------------------------------------------
# EMPLOYEE GALLERY  (pre-extracted face crops + augmented images)
# Each sub-folder name becomes the employee display name.
# ---------------------------------------------------------------
GALLERY_DIR        = os.path.join(BASE_DIR, "employee gallery")
