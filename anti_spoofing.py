# =============================================================
#  anti_spoofing.py  —  Passive liveness via Silent-Face MiniFASNet V2
#
#  Replaces the old YOLO "is the face inside a screen box" geometry
#  heuristic (which rejected real employees and let normal-sized
#  photos through) with a real anti-spoof CNN.
#
#  The model scores face TEXTURE into 3 classes; index 1 = "real".
#  A printed photo or a phone/laptop/TV screen scores as fake, so a
#  scan of a photo will NOT be marked present.
#
#  Public API (unchanged — drop-in for camera_processor.py):
#      spoof = AntiSpoofing()
#      spoof.available()                       -> bool
#      is_real, score = spoof.check_liveness(frame_bgr, [x1,y1,x2,y2])
# =============================================================

import logging
import numpy as np

from config import (
    LIVENESS_THRESHOLD,
    ANTI_SPOOF_MODEL_PATH,
    ANTI_SPOOF_SCALE,
    ANTI_SPOOF_INPUT_SIZE,
    ANTI_SPOOF_USE_BGR,
    ANTI_SPOOF_FAIL_OPEN,
    get_onnx_providers,
)

log = logging.getLogger(__name__)

try:
    import cv2
    import onnxruntime as ort
    _DEPS_OK = True
except ImportError as exc:          # pragma: no cover
    _DEPS_OK = False
    log.error("[SPOOF] Missing dependency for liveness (%s).", exc)


def _softmax(x):
    e = np.exp(x - np.max(x))
    return e / e.sum()


def _crop_face(img, bbox, scale, out_size):
    """
    Reproduce Silent-Face CropImage exactly: expand the face box by
    `scale` about its centre, clamp to the frame, then resize to a
    square `out_size`. MiniFASNet was trained on this exact crop, so
    getting it right is what makes the scores meaningful.

    bbox is [x1, y1, x2, y2]; converted internally to x/y/w/h.
    """
    src_h, src_w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    box_w = max(1.0, x2 - x1)
    box_h = max(1.0, y2 - y1)

    scale = min((src_h - 1) / box_h, (src_w - 1) / box_w, scale)
    new_w = box_w * scale
    new_h = box_h * scale
    cx = x1 + box_w / 2.0
    cy = y1 + box_h / 2.0

    lt_x = cx - new_w / 2.0
    lt_y = cy - new_h / 2.0
    rb_x = cx + new_w / 2.0
    rb_y = cy + new_h / 2.0

    if lt_x < 0:
        rb_x -= lt_x; lt_x = 0
    if lt_y < 0:
        rb_y -= lt_y; lt_y = 0
    if rb_x > src_w - 1:
        lt_x -= (rb_x - src_w + 1); rb_x = src_w - 1
    if rb_y > src_h - 1:
        lt_y -= (rb_y - src_h + 1); rb_y = src_h - 1

    crop = img[int(lt_y):int(rb_y) + 1, int(lt_x):int(rb_x) + 1]
    if crop.size == 0:
        return None
    return cv2.resize(crop, (out_size, out_size))


class AntiSpoofing:
    """Passive liveness check backed by MiniFASNet V2 (ONNX)."""

    def __init__(self):
        self._sess = None
        self._input_name = None
        self._scale = ANTI_SPOOF_SCALE
        self._size = ANTI_SPOOF_INPUT_SIZE
        self._use_bgr = ANTI_SPOOF_USE_BGR

        if not _DEPS_OK:
            return
        try:
            self._sess = ort.InferenceSession(
                ANTI_SPOOF_MODEL_PATH, providers=get_onnx_providers()
            )
            self._input_name = self._sess.get_inputs()[0].name
            log.info(
                "[SPOOF] MiniFASNet liveness loaded (scale=%.1f, size=%d, bgr=%s, thresh=%.2f).",
                self._scale, self._size, self._use_bgr, LIVENESS_THRESHOLD,
            )
        except Exception as exc:
            log.error("[SPOOF] Failed to load liveness model %s: %s",
                      ANTI_SPOOF_MODEL_PATH, exc)
            self._sess = None

    def available(self):
        return self._sess is not None

    def score(self, frame_bgr, bbox):
        """Return the raw 'real' probability (0.0–1.0), or None on error."""
        if self._sess is None:
            return None
        crop = _crop_face(frame_bgr, bbox, self._scale, self._size)
        if crop is None:
            return None

        img = crop if self._use_bgr else cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        # IMPORTANT: this ONNX export expects RAW 0-255 pixel values (NOT /255).
        # Calibrated against real faces: raw input -> class 1 ("real") ~0.99,
        # whereas dividing by 255 wrongly collapses real faces onto class 2.
        x = img.astype(np.float32)                    # raw 0-255, no normalisation
        x = np.transpose(x, (2, 0, 1))[None, ...]     # HWC -> NCHW
        try:
            out = self._sess.run(None, {self._input_name: x})[0][0]
        except Exception as exc:
            log.debug("[SPOOF] inference error: %s", exc)
            return None
        probs = _softmax(out)
        return float(probs[1])                         # class 1 == real

    def check_liveness(self, frame, bbox):
        """
        Returns (is_real: bool, score: float).

        If the model is unavailable, behaviour is governed by
        ANTI_SPOOF_FAIL_OPEN: secure deployments reject (fail closed).
        """
        if self._sess is None:
            if ANTI_SPOOF_FAIL_OPEN:
                return True, 1.0
            log.warning("[SPOOF] Liveness unavailable — rejecting (fail-closed).")
            return False, 0.0

        real_prob = self.score(frame, bbox)
        if real_prob is None:
            return (True, 1.0) if ANTI_SPOOF_FAIL_OPEN else (False, 0.0)

        is_real = real_prob >= LIVENESS_THRESHOLD
        if not is_real:
            log.warning("[SPOOF] Spoof/photo rejected (real_prob=%.2f < %.2f).",
                        real_prob, LIVENESS_THRESHOLD)
        return is_real, real_prob
