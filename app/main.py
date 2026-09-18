import io
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image

from app.services.auth import verify_supabase_token
from app.services.model_registry import ModelRegistry
from app.services.scan_service import deduct_scan, refund_scan
from app.services.history_service import save_diagnosis
from app.services.gradcam import generate_gradcam_base64
from app.schemas.diagnosis import DiagnosisResponse

registry = None

CONTEXT_MAP = {
    "maize": "crop",
    "rice_10class": "crop",
    "millet_3class": "crop",
    "soybean_14class": "crop",
    "pepper_13class": "crop",
    "cabbage_8class": "crop",
    "apple": "crop",
    "cassava": "crop",
    "coffee": "crop",
    "grape": "crop",
    "sugarcane": "crop",
    "tea": "crop",
    "pests_102class": "pest",
    "soil_11class": "soil",
    "cattle": "livestock",
    "poultry": "livestock",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global registry
    print("GAIA API starting")
    registry = ModelRegistry()
    print("Registry ready")
    yield
    print("GAIA API shutting down")


app = FastAPI(title="GAIA Model API", version="1.1.0", lifespan=lifespan)

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
        "version": "1.1.0",
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

    user_id = user.get("sub")
    if not user_id:
        raise HTTPException(401, "Token missing sub claim")

    if not registry.has(model):
        raise HTTPException(404, "Unknown model: " + model)

    contents = await image.read()
    if len(contents) > 15 * 1024 * 1024:
        raise HTTPException(413, "Image too large (max 15 MB)")

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception:
        raise HTTPException(400, "Invalid image")

    try:
        remaining = deduct_scan(user_id, cost=1)
    except Exception as e:
        raise HTTPException(402, "Scan error: " + str(e))

    try:
        preds, model_obj, tensor = registry.predict_with_tensor(model, img)
    except Exception as e:
        try:
            refund_scan(user_id, amount=1)
        except Exception:
            pass
        raise HTTPException(500, "Inference failed: " + str(e))

    context_type = CONTEXT_MAP.get(model, "crop")
    history_id = save_diagnosis(user_id, model, context_type, preds)

    gradcam_b64 = None
    try:
        from app.services.model_registry import MODEL_CONFIG
        labels = MODEL_CONFIG[model].get("labels")
        if labels:
            top_label = preds[0]["label"]
            if top_label in labels:
                idx = labels.index(top_label)
                gradcam_b64 = generate_gradcam_base64(model_obj, tensor, idx)
    except Exception:
        gradcam_b64 = None

    elapsed = int((time.time() - start) * 1000)

    return DiagnosisResponse(
        predictions=preds,
        top=preds[0],
        model=model,
        processingMs=elapsed,
        scansRemaining=remaining,
        historyId=history_id,
        gradcamBase64=gradcam_b64,
    )


@app.exception_handler(HTTPException)
async def http_exc(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
