import os
import httpx
from fastapi import APIRouter, UploadFile, File, Form, Header, HTTPException

router = APIRouter()
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")


@router.post("/stt")
async def speech_to_text(
    audio: UploadFile = File(...),
    language: str = Form("en"),
    authorization: str = Header(None),
):
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")

    contents = await audio.read()

    files = {
        "file": ("audio.m4a", contents, "audio/m4a"),
    }
    data = {
        "model": "whisper-large-v3",
        "language": language,
        "response_format": "json",
    }
    headers = {"Authorization": "Bearer " + GROQ_KEY}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers, files=files, data=data,
        )

    if r.status_code != 200:
        raise HTTPException(r.status_code, "STT failed: " + r.text[:200])

    return {"text": r.json().get("text", "")}
