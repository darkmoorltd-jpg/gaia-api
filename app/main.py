import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from PIL import Image
import io
import requests
from pydantic import BaseModel
from typing import List, Optional

from app.services.auth import verify_supabase_token
from app.services.model_registry import ModelRegistry
from app.services.scan_service import deduct_scan
from app.services.satellite import fetch_tile
from app.services.paystack_wallet import list_banks, create_recipient, initiate_transfer
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


# ============================================
# Health & models
# ============================================
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


# ============================================
# Diagnose
# ============================================
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

    return DiagnosisResponse(
        predictions=preds,
        top=preds[0],
        model=model,
        processingMs=int((time.time() - start) * 1000),
        scansRemaining=remaining,
    )


# ============================================
# Chat (DeepSeek proxy)
# ============================================
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    max_tokens: Optional[int] = 1500


GAIA_SYSTEM_PROMPT = (
    "You are GAIA, an expert agricultural advisor built by Darkmoor Ltd in Nigeria. "
    "Give practical, specific, Nigeria and Africa-context answers. "
    "Be concise — 2 to 6 short paragraphs. Use plain language. "
    "Never mention DeepSeek, OpenAI, or any AI provider — you ARE GAIA."
)


@app.post("/chat")
async def chat(body: ChatRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise HTTPException(500, "DEEPSEEK_API_KEY not configured")

    msgs = [{"role": "system", "content": GAIA_SYSTEM_PROMPT}]
    for m in body.messages:
        msgs.append({
            "role": "assistant" if m.role == "assistant" else "user",
            "content": m.content,
        })

    r = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json={
            "model": "deepseek-chat",
            "messages": msgs,
            "max_tokens": body.max_tokens or 1500,
            "temperature": 0.7,
        },
        timeout=90,
    )
    if r.status_code != 200:
        raise HTTPException(500, "DeepSeek " + str(r.status_code) + ": " + r.text[:200])
    try:
        reply = r.json()["choices"][0]["message"]["content"]
    except Exception:
        raise HTTPException(500, "Bad model response")
    return {"reply": reply}


# ============================================
# Satellite tile
# ============================================
@app.get("/satellite/tile")
async def satellite_tile(
    lat: float = Query(...),
    lon: float = Query(...),
    layer: str = Query("TRUE_COLOR"),
    authorization: str = Header(None),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    try:
        verify_supabase_token(authorization.split(" ", 1)[1])
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))

    try:
        png = fetch_tile(lat, lon, layer)
    except Exception as e:
        raise HTTPException(500, "Satellite fetch failed: " + str(e))

    return Response(content=png, media_type="image/png")


# ============================================
# Wallet transfers
# ============================================
@app.get("/wallet/banks")
async def wallet_banks(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    try:
        verify_supabase_token(authorization.split(" ", 1)[1])
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))
    try:
        return {"banks": list_banks()}
    except Exception as e:
        raise HTTPException(500, "Paystack banks failed: " + str(e))


class RecipientRequest(BaseModel):
    name: str
    account_number: str
    bank_code: str


@app.post("/wallet/recipient")
async def wallet_recipient(body: RecipientRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    try:
        verify_supabase_token(authorization.split(" ", 1)[1])
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))
    try:
        data = create_recipient(body.name, body.account_number, body.bank_code)
        return {"recipient_code": data.get("recipient_code")}
    except Exception as e:
        raise HTTPException(500, "Recipient creation failed: " + str(e))


class TransferRequest(BaseModel):
    recipient_code: str
    amount: float
    reason: Optional[str] = "GAIA wallet withdrawal"


@app.post("/wallet/transfer")
async def wallet_transfer(body: TransferRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    try:
        verify_supabase_token(authorization.split(" ", 1)[1])
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))
    try:
        data = initiate_transfer(body.recipient_code, body.amount, body.reason)
        return {"transfer_code": data.get("transfer_code"), "status": data.get("status")}
    except Exception as e:
        raise HTTPException(500, "Transfer failed: " + str(e))


@app.exception_handler(HTTPException)
async def http_exc(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
