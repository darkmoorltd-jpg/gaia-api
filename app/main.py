
import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image
import io

from app.services.auth import verify_supabase_token
from app.services.model_registry import ModelRegistry
from app.services.scan_service import deduct_scan
from app.schemas.diagnosis import DiagnosisResponse

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

    try:
        remaining = deduct_scan(user_id, cost=1)
    except Exception as e:
        raise HTTPException(402, "Insufficient scans: " + str(e))

    try:
        preds = registry.predict(model, img)
    except Exception as e:
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
    )


@app.exception_handler(HTTPException)
async def http_exc(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
