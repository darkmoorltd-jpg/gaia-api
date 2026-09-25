import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image
import io
import traceback

from app.services.auth import verify_supabase_token
from app.services.model_registry import ModelRegistry
from app.services.scan_service import deduct_scan
from app.schemas.diagnosis import DiagnosisResponse
from app.services.recommendations import get_recommendations
from app.routers.agronomist import router as agronomist_router

registry = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global registry
    print("GAIA API starting")
    registry = ModelRegistry()
    print("Registry ready")
    yield
    print("GAIA API shutting down")


app = FastAPI(title="GAIA Model API", version="1.0.0", lifespan=lifespan)

app.include_router(agronomist_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": "1.0.0",
        "models_loaded": list(registry.loaded_keys()) if registry else [],
    }


@app.get("/models")
async def list_models():
    if not registry:
        raise HTTPException(503, "Registry not ready")
    return {"models": registry.list_models()}


@app.post("/diagnose", response_model=DiagnosisResponse)
async def diagnose(
    image: UploadFile = File(...),
    model: str = Form(...),
    authorization: str = Header(None),
):
    start = time.time()

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]

    try:
        user = verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))

    user_id = user["sub"]

    if not registry.has(model):
        raise HTTPException(404, "Unknown model: " + model)

    contents = await image.read()
    if len(contents) > 15 * 1024 * 1024:
        raise HTTPException(413, "Image too large (max 15 MB)")

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception:
        raise HTTPException(400, "Invalid image")

    remaining = 30
    try:
        remaining = deduct_scan(user_id, cost=1)
        print("Scans deducted. Remaining: " + str(remaining))
    except Exception as e:
        print("Scan deduction error: " + str(e))
        raise HTTPException(402, "Insufficient scans: " + str(e))

    recommendations = None
    try:
        preds, gradcam_b64, top_idx = registry.predict_with_cam(model, img)
        print("Grad-CAM present: " + str(gradcam_b64 is not None))

        # Generate recommendations unless disabled
        try:
            recommendations = get_recommendations(
                model_key=model,
                top_label=preds[0]["label"],
                confidence=preds[0]["confidence"],
            )
        except Exception as rec_err:
            print("Recommendation error: " + str(rec_err))
    except Exception as e:
        print("Inference error: " + str(e))
        traceback.print_exc()
        try:
            deduct_scan(user_id, cost=-1)
        except Exception:
            pass
        raise HTTPException(500, "Inference failed: " + str(e))

    elapsed = int((time.time() - start) * 1000)

    return DiagnosisResponse(
        predictions=preds,
        top=preds[0],
        model=model,
        processingMs=elapsed,
        scansRemaining=remaining,
        gradcam_image=gradcam_b64,
        top_class_index=top_idx,
        recommendations=recommendations,
    )


@app.exception_handler(HTTPException)
async def http_exc(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

@app.get("/debug/gradcam-test/{model_key}")
async def debug_gradcam(model_key: str):
    """Test Grad-CAM directly without auth or image upload."""
    if not registry or not registry.has(model_key):
        raise HTTPException(404, "Unknown model: " + model_key)

    from PIL import Image
    import numpy as np

    test_img = Image.new("RGB", (224, 224), (60, 100, 50))
    arr = np.array(test_img)
    arr[80:140, 90:150] = [120, 80, 40]
    test_img = Image.fromarray(arr)

    try:
        preds, cam_b64, top_idx = registry.predict_with_cam(model_key, test_img)
        return {
            "ok": True,
            "model": model_key,
            "top_label": preds[0]["label"],
            "gradcam_present": cam_b64 is not None,
            "gradcam_size_chars": len(cam_b64) if cam_b64 else 0,
        }
    except Exception as e:
        import traceback
        return {
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }

