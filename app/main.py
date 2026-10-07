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
from app.routers.voice import router as voice_router
from app.routers.farming_calendar import router as farming_calendar_router
from app.routers.chat import router as chat_router
from app.routers.rag import router as rag_router
from app.routers.tts import router as tts_router
from app.routers.satellite import router as satellite_router
from app.routers.purchases import router as purchases_router
from app.routers.wallet import router as wallet_router
from app.routers.wallet_pin import router as wallet_pin_router
from app.routers.wallet_pdf import router as wallet_pdf_router
from app.routers.rosca_pdf import router as rosca_pdf_router
from app.routers.marketplace import router as marketplace_router
from app.routers.badge import router as badge_router
from app.routers.notifications import router as notifications_router
from app.routers.payment import router as payment_router
from app.routers.agro_tools import router as agro_router

registry = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global registry
    print("GAIA API starting...", flush=True)
    try:
        registry = ModelRegistry()
        print("Registry ready", flush=True)
    except Exception as e:
        print("Registry failed:", e, flush=True)
        registry = None
    yield
    print("GAIA API shutting down", flush=True)


app = FastAPI(title="GAIA Model API", version="1.0.0", lifespan=lifespan)

app.include_router(voice_router)
app.include_router(farming_calendar_router)
app.include_router(chat_router)
app.include_router(rag_router)
app.include_router(tts_router)
app.include_router(satellite_router)
app.include_router(purchases_router)
app.include_router(wallet_router)
app.include_router(wallet_pdf_router)
app.include_router(rosca_pdf_router)
app.include_router(wallet_pin_router)
app.include_router(marketplace_router)
app.include_router(badge_router)
app.include_router(notifications_router)
app.include_router(payment_router)
app.include_router(agro_router)

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
        "env": {
            "SUPABASE_URL": bool(os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")),
            "SUPABASE_SERVICE_KEY": bool(os.environ.get("SUPABASE_SERVICE_KEY")),
            "GROQ_API_KEY": bool(os.environ.get("GROQ_API_KEY")),
            "SUPABASE_JWT_SECRET": bool(os.environ.get("SUPABASE_JWT_SECRET")),
        },
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

    if not registry or not registry.has(model):
        raise HTTPException(404, "Unknown model: " + model)

    contents = await image.read()
    if len(contents) > 15 * 1024 * 1024:
        raise HTTPException(413, "Image too large")

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
