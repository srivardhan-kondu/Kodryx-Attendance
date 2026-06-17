# =============================================================
#  face_engine.py  —  InsightFace-based face recognition engine
#
#  Replaces the old MTCNN + DeepFace (Facenet512) pipeline.
#  InsightFace's buffalo_l model gives:
#    • ArcFace recognition (faster + more accurate than Facenet512)
#    • Built-in RetinaFace detector (no MTCNN needed)
#    • 512-dim embeddings with cosine similarity
#
#  Public API:
#    engine = FaceEngine()
#    faces  = engine.detect_and_embed(frame_bgr)
#      → list of dicts: {box, embedding, det_score, kps}
#    emp_id, name, conf = engine.match(embedding, employee_db)
#
#  Database loading (preferred → fallback):
#    1. data/employees.db  (SQLite via EmployeeDB)
#    2. enrollment/embeddings.pkl  (legacy pickle)
# =============================================================

import logging
import pickle
import os
import numpy as np

log = logging.getLogger(__name__)

try:
    import insightface
    from insightface.app import FaceAnalysis
    INSIGHTFACE_AVAILABLE = True
except ImportError:
    INSIGHTFACE_AVAILABLE = False
    log.warning("[FACE] insightface not installed. Face recognition disabled.")

from sklearn.metrics.pairwise import cosine_similarity
from config import (
    FACE_RECOGNITION_MODEL,
    CONFIDENCE_THRESHOLD,
    EMBEDDINGS_FILE,
    EMPLOYEE_DB_PATH,
    FACE_DET_SIZE,
    FACE_DET_THRESH,
    USE_YOLO_PERSON_FACE_DETECT,
    YOLO_MODEL_NAME,
    YOLO_PERSON_CONF,
    YOLO_PERSON_PADDING,
    FACE_DEDUP_IOU,
    get_onnx_providers,
    get_torch_device,
    get_yolo_device,
)

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False


def _box_iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix1 >= ix2 or iy1 >= iy2:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _expand_box(box, shape, padding=0.12):
    h, w = shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    x1 = max(0, int(x1 - bw * padding))
    y1 = max(0, int(y1 - bh * padding))
    x2 = min(w, int(x2 + bw * padding))
    y2 = min(h, int(y2 + bh * padding))
    return x1, y1, x2, y2


def _dedupe_faces(faces, iou_thresh=0.45):
    """Keep highest det_score when two boxes overlap."""
    if len(faces) <= 1:
        return faces
    ranked = sorted(faces, key=lambda f: f["det_score"], reverse=True)
    kept = []
    for face in ranked:
        if any(_box_iou(face["box"], k["box"]) >= iou_thresh for k in kept):
            continue
        kept.append(face)
    return kept


class FaceEngine:
    """
    Thin wrapper around InsightFace FaceAnalysis.

    Usage:
        engine = FaceEngine()
        faces  = engine.detect_and_embed(bgr_frame)
        for face in faces:
            emp_id, name, conf = engine.match(face["embedding"], employee_db)
    """

    def __init__(self):
        self._app = None
        self._yolo = None
        self._yolo_device = get_yolo_device()
        self.employee_db = {}
        self._available = False

        if not INSIGHTFACE_AVAILABLE:
            return

        try:
            providers = get_onnx_providers()
            use_gpu = get_torch_device().startswith("cuda")
            ctx_id = 0 if use_gpu else -1
            self._app = FaceAnalysis(
                name=FACE_RECOGNITION_MODEL,
                allowed_modules=['detection', 'recognition'],
                providers=providers,
            )
            self._app.prepare(ctx_id=ctx_id, det_size=FACE_DET_SIZE)
            
            if 'detection' in self._app.models:
                det = self._app.models['detection']
                det.det_thresh = FACE_DET_THRESH
                if hasattr(det, 'nms_thresh'):
                    det.nms_thresh = 0.35
                
            self._available = True
            active = providers[0] if providers else "unknown"
            log.info(
                "[FACE] InsightFace (%s) loaded on %s (ctx_id=%s, det_size=%s, det_thresh=%.2f).",
                FACE_RECOGNITION_MODEL, active, ctx_id, FACE_DET_SIZE, FACE_DET_THRESH,
            )
        except Exception as exc:
            log.error("[FACE] Failed to initialise InsightFace: %s", exc)

    def _ensure_yolo(self):
        if self._yolo is not None or not YOLO_AVAILABLE or not USE_YOLO_PERSON_FACE_DETECT:
            return
        try:
            self._yolo = YOLO(YOLO_MODEL_NAME)
            log.info("[FACE] YOLO person assist enabled for multi-face detection.")
        except Exception as exc:
            log.warning("[FACE] YOLO person assist unavailable: %s", exc)

    def _detect_persons(self, frame_bgr):
        self._ensure_yolo()
        if self._yolo is None:
            return []
        try:
            results = self._yolo(
                frame_bgr, classes=[0], conf=YOLO_PERSON_CONF,
                verbose=False, device=self._yolo_device,
            )
        except Exception as exc:
            log.debug("[FACE] YOLO person detect error: %s", exc)
            return []
        persons = []
        for result in results:
            for box in result.boxes:
                xyxy = box.xyxy[0].cpu().numpy()
                persons.append([int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])])
        return persons

    def _insightface_on_image(self, image_bgr, offset_x=0, offset_y=0):
        if image_bgr is None or image_bgr.size == 0:
            return []
        try:
            raw = self._app.get(image_bgr)
        except Exception as exc:
            log.debug("[FACE] insightface error: %s", exc)
            return []
        results = []
        for face in raw:
            box = face.bbox.tolist()
            results.append({
                "box": [
                    int(box[0]) + offset_x, int(box[1]) + offset_y,
                    int(box[2]) + offset_x, int(box[3]) + offset_y,
                ],
                "embedding": face.embedding,
                "det_score": float(face.det_score),
                "kps": face.kps if hasattr(face, "kps") else None,
            })
        return results

    def available(self):
        return self._available

    def load_db(self, db_path: str = None) -> dict:
        """
        Convenience method: load the employee database from SQLite
        (or pkl fallback) and cache it in self.employee_db.

        Returns the loaded database dict so callers can also use it
        directly without going through self.employee_db.
        """
        self.employee_db = load_employee_database(db_path)
        log.info(
            "[FACE] Cached %d employee(s) in engine.", len(self.employee_db)
        )
        return self.employee_db

    def embed_face_crop(self, face_img_bgr) -> "np.ndarray | None":
        """
        Embed a PRE-CROPPED face image directly using the ArcFace
        recognition model — bypasses RetinaFace detection entirely.

        Use this when the input image IS the face (no surrounding
        context), as is the case with employee gallery face crops.
        Detection would fail on tightly-cropped faces; this method
        feeds the crop straight to the recognition head.

        Args:
            face_img_bgr: BGR numpy array of any size.

        Returns:
            L2-normalised 512-dim float32 embedding, or None on error.
        """
        if not self._available or self._app is None:
            return None

        try:
            # Locate the recognition model inside the FaceAnalysis bundle.
            # InsightFace stores models in _app.models (dict keyed by
            # model file stem or task name depending on version).
            rec_model = None
            models_src = (
                self._app.models.values()
                if isinstance(self._app.models, dict)
                else self._app.models
            )
            for m in models_src:
                taskname = getattr(m, "taskname", "") or ""
                if "recognition" in taskname.lower():
                    rec_model = m
                    break

            if rec_model is None:
                log.warning("[FACE] Recognition model not found in FaceAnalysis bundle.")
                return None

            # get_feat() handles resize + normalisation internally (BGR input).
            feat = rec_model.get_feat([face_img_bgr])
            if feat is None or len(feat) == 0:
                return None

            emb = feat[0].astype(np.float32)
            norm = np.linalg.norm(emb)
            if norm > 0:
                emb = emb / norm
            return emb

        except Exception as exc:
            log.debug("[FACE] embed_face_crop error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Detection + embedding extraction
    # ------------------------------------------------------------------
    def detect_and_embed(self, frame_bgr):
        """
        Run face detection + recognition on a BGR frame.
        Uses full-frame InsightFace plus YOLO person crops so everyone
        in view is detected, including people far from the camera.

        Returns list of dicts:
            {
                "box":        [x1, y1, x2, y2],   # ints
                "embedding":  np.ndarray (512,),
                "det_score":  float,
                "kps":        np.ndarray (5x2) landmark keypoints or None
            }
        """
        if not self._available or self._app is None:
            return []

        all_faces = self._insightface_on_image(frame_bgr)

        if USE_YOLO_PERSON_FACE_DETECT:
            for person_box in self._detect_persons(frame_bgr):
                x1, y1, x2, y2 = _expand_box(
                    person_box, frame_bgr.shape, padding=YOLO_PERSON_PADDING,
                )
                crop = frame_bgr[y1:y2, x1:x2]
                all_faces.extend(
                    self._insightface_on_image(crop, offset_x=x1, offset_y=y1)
                )

        return _dedupe_faces(all_faces, iou_thresh=FACE_DEDUP_IOU)

    # ------------------------------------------------------------------
    # Employee matching
    # ------------------------------------------------------------------
    def match(self, embedding, employee_db):
        """
        Match a face embedding against the enrolled employee database.

        Returns:
            (employee_id, employee_name, confidence)  if matched
            (None, None, best_conf)                   if no match
        """
        if not employee_db:
            return None, None, 0.0

        best_id   = None
        best_name = None
        best_conf = 0.0
        face_vec  = np.array(embedding).reshape(1, -1)

        for emp_id, emp_data in employee_db.items():
            stored = emp_data.get("embeddings", [])
            if not stored:
                continue
            stored_mat = np.array(stored)
            sims = cosine_similarity(face_vec, stored_mat)
            max_sim = float(np.max(sims))

            if max_sim > best_conf:
                best_conf = max_sim
                best_id   = emp_id
                best_name = emp_data.get("name", emp_id)

        if best_conf >= CONFIDENCE_THRESHOLD:
            return best_id, best_name, best_conf
        return None, None, best_conf


# ------------------------------------------------------------------
# Employee database helpers
# ------------------------------------------------------------------

def load_employee_database_from_mongo() -> dict:
    """
    Load the employee embedding database from MongoDB.
    """
    try:
        from employee_db import EmployeeDB
        edb = EmployeeDB()
        edb.initialize()
        data = edb.get_all()
        log.info("[FACE] Loaded %d employee(s) from MongoDB.", len(data))
        return data
    except Exception as exc:
        log.error("[FACE] Could not load MongoDB: %s", exc)
        return {}


def load_employee_database(path: str = None) -> dict:
    """
    Load the employee embedding database.

    Strategy (preferred → fallback):
      1. MongoDB   — primary store (Phase 3+)
      2. Pickle  enrollment/embeddings.pkl — legacy fallback

    Pass an explicit `path` to override the default (e.g. pass the pkl path directly to force pickle loading).

    Returns {} if neither source is available.
    """
    # If caller passes an explicit path that looks like a pkl, use pkl directly
    if path and path.endswith(".pkl"):
        return _load_pkl(path)

    # Try MongoDB first
    data = load_employee_database_from_mongo()
    if data:
        return data
        
    log.warning("[FACE] MongoDB empty or failed — falling back to pkl.")

    # Fallback to pickle
    return _load_pkl(EMBEDDINGS_FILE)


def _load_pkl(path: str) -> dict:
    """Internal helper: load from a pickle file."""
    if not os.path.exists(path):
        log.error("[FACE] Embeddings file not found: %s", path)
        return {}
    try:
        with open(path, "rb") as f:
            db = pickle.load(f)
        log.info("[FACE] Loaded %d employee(s) from pkl: %s", len(db), path)
        return db
    except Exception as exc:
        log.error("[FACE] Could not load pkl: %s", exc)
        return {}
