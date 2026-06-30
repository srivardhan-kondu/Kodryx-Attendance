# =============================================================
#  scan_service.py  —  browser-camera attendance scanning
#
#  The web Kiosk page captures the camera in the BROWSER and POSTs
#  frames to /api/scan; this module runs the AI on each frame:
#     decode -> detect+embed -> liveness -> match -> log attendance
#
#  Models are lazy-loaded on first scan, so a reporting-only
#  dashboard deployment pays nothing unless scanning is used.
# =============================================================

import base64
import logging
import threading
from datetime import timedelta

import cv2
import numpy as np

from tz_utils import now_ist
from config import (
    MIN_FACE_SIZE, FACE_MIN_DET_SCORE, COOLDOWN_MINUTES,
    ENABLE_ANTI_SPOOFING,
)
from face_engine import FaceEngine, load_employee_database
from attendance_db import log_presence_event

try:
    from anti_spoofing import AntiSpoofing
except Exception:                       # pragma: no cover
    AntiSpoofing = None

log = logging.getLogger(__name__)

_engine = None
_spoof = None
_db = None
_lock = threading.Lock()
_last_seen = {}                         # employee_id -> datetime (cooldown)


def _ensure_loaded():
    global _engine, _spoof, _db
    if _engine is None:
        with _lock:
            if _engine is None:
                log.info("[SCAN] Loading models for browser scanning...")
                eng = FaceEngine()
                _spoof_local = (AntiSpoofing()
                                if ENABLE_ANTI_SPOOFING and AntiSpoofing else None)
                _db_local = load_employee_database()
                globals()["_spoof"] = _spoof_local
                globals()["_db"] = _db_local
                globals()["_engine"] = eng       # set last = "ready" flag
    return _engine, _spoof, _db


def reload_employees():
    """Refresh the enrolled-faces cache (call after enrolling someone)."""
    global _db
    with _lock:
        _db = load_employee_database()
    return len(_db or {})


def enroll_employee(employee_name, images_b64):
    """
    Enroll a new employee directly from the web (no terminal worker):
    detect the face in each uploaded photo, store every embedding, then
    refresh the live scanner cache so they're recognised immediately.
    """
    engine, _spoof_unused, _db_unused = _ensure_loaded()
    if not engine.available():
        return {"success": False, "error": "Face engine unavailable on the server."}

    name = (employee_name or "").strip()
    if not name:
        return {"success": False, "error": "Please enter a name."}
    if not images_b64:
        return {"success": False, "error": "Please add at least one photo."}

    embeddings = []
    for b in images_b64:
        frame = _decode(b)
        if frame is None:
            continue
        faces = engine.detect_and_embed(frame)
        if not faces:
            continue
        face = max(faces, key=lambda f: (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1]))
        embeddings.append(face["embedding"])

    if not embeddings:
        return {"success": False,
                "error": "No clear face found in the photo(s). Use bright, front-facing photos."}

    employee_id = name.lower().replace(" ", "_")
    mat = np.array(embeddings, dtype=np.float32).reshape(len(embeddings), -1)

    from employee_db import EmployeeDB
    edb = EmployeeDB(); edb.initialize()
    edb.upsert(employee_id, name, mat, len(embeddings))
    reload_employees()                       # so the scanner knows them at once

    log.info("[SCAN] Enrolled %s (%s) from %d photo(s).", name, employee_id, len(embeddings))
    return {"success": True, "employee_id": employee_id, "faces": len(embeddings)}


def _decode(image_b64):
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    arr = np.frombuffer(base64.b64decode(image_b64), np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _cooldown_ok(employee_id):
    last = _last_seen.get(employee_id)
    return last is None or (now_ist() - last) >= timedelta(minutes=COOLDOWN_MINUTES)


def scan_frame(image_b64):
    """
    Run one browser frame through the pipeline.

    Returns a dict the kiosk page renders:
      {"status": "present", "name", "confidence", "liveness", "marked"}
      {"status": "spoof", "liveness"}
      {"status": "unknown", "confidence", "liveness"}
      {"status": "no_face"}  |  {"status": "error", "message"}
    """
    engine, spoof, db = _ensure_loaded()
    if not engine.available():
        return {"status": "error", "message": "face engine unavailable"}

    frame = _decode(image_b64)
    if frame is None:
        return {"status": "error", "message": "could not decode image"}

    faces = engine.detect_and_embed(frame)
    if not faces:
        return {"status": "no_face"}

    # Kiosk = one person at a time -> evaluate the largest face.
    face = max(faces, key=lambda f: (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1]))
    if face["det_score"] < FACE_MIN_DET_SCORE:
        return {"status": "no_face"}
    x1, y1, x2, y2 = face["box"]
    if (x2 - x1) < MIN_FACE_SIZE or (y2 - y1) < MIN_FACE_SIZE:
        return {"status": "no_face"}

    liveness = 1.0
    if spoof is not None:
        is_real, liveness = spoof.check_liveness(frame, face["box"])
        if not is_real:
            return {"status": "spoof", "liveness": round(float(liveness), 3)}

    emp_id, name, conf = engine.match(face["embedding"], db)
    if emp_id is None:
        return {"status": "unknown",
                "confidence": round(float(conf), 3),
                "liveness": round(float(liveness), 3)}

    marked = False
    if _cooldown_ok(emp_id):
        log_presence_event(emp_id, name, conf, "kiosk_web")
        _last_seen[emp_id] = now_ist()
        marked = True

    return {"status": "present", "name": name,
            "confidence": round(float(conf), 3),
            "liveness": round(float(liveness), 3),
            "marked": marked}
