"""
Run this ONCE on any new machine after cloning:
    python download_models.py

Downloads the InsightFace buffalo_l face recognition models (~325 MB)
into data/buffalo_l/ so the attendance system works without internet
on subsequent runs.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from insightface.app import FaceAnalysis
except ImportError:
    print("ERROR: insightface not installed. Run:  pip install -r requirements.txt")
    sys.exit(1)

from config import FACE_RECOGNITION_MODEL, BASE_DIR, get_onnx_providers

model_dir = os.path.join(BASE_DIR, "data", "buffalo_l")
print(f"Downloading InsightFace '{FACE_RECOGNITION_MODEL}' models (~325 MB)...")
print(f"Saving to: {model_dir}")
print("This only runs once — please wait...\n")

app = FaceAnalysis(
    name=FACE_RECOGNITION_MODEL,
    root=os.path.join(BASE_DIR, "data"),
    allowed_modules=["detection", "recognition"],
    providers=get_onnx_providers(),
)
app.prepare(ctx_id=-1, det_size=(640, 640))

files = os.listdir(model_dir) if os.path.isdir(model_dir) else []
print(f"\nDone! {len(files)} model files ready in data/buffalo_l/")
print("You can now start the system:  python serve.py")
