import os
import requests
from fastapi import APIRouter, UploadFile, File, HTTPException, Header, Form
from typing import Optional

from app.services.auth import verify_supabase_token

router = APIRouter()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3-turbo"

# Supported languages (must match mobile src/utils/languages.ts)
SUPPORTED_LANGS = {"en", "ha", "yo", "ig", "sw", "am", "zu", "fr", "ar"}


@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: Optional[str] = Form("en"),
    authorization: str = Header(None),
):
    # Auth
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))

    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured on server")

    contents = await audio.read()
    if len(contents) > 25 * 1024 * 1024:
        raise HTTPException(413, "Audio too large (max 25 MB)")

    # Sanitize language hint — fall back to auto if unknown
    lang_hint = (language or "en").lower()
    if lang_hint not in SUPPORTED_LANGS and lang_hint != "auto":
        lang_hint = "auto"

    filename = audio.filename or "voice.m4a"
    content_type = audio.content_type or "audio/m4a"

    files = {"file": (filename, contents, content_type)}
    data = {
        "model": WHISPER_MODEL,
        "response_format": "json",
        "language": lang_hint,
    }
    headers = {"Authorization": "Bearer " + GROQ_API_KEY}

    try:
        r = requests.post(GROQ_URL, headers=headers, files=files, data=data, timeout=60)
    except Exception as e:
        raise HTTPException(502, "Groq request failed: " + str(e))

    if r.status_code != 200:
        raise HTTPException(502, "Groq error " + str(r.status_code) + ": " + r.text[:200])

    result = r.json()
    text = result.get("text", "").strip()
    return {"text": text, "language": lang_hint}
