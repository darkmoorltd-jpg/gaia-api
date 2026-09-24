from fastapi import APIRouter, File, UploadFile, Form, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List

from app.services.auth import verify_supabase_token
from app.services import groq_client

router = APIRouter(prefix="/agronomist", tags=["agronomist"])


def _auth(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    return verify_supabase_token(token)


class TextRequest(BaseModel):
    question: str
    history: Optional[List[dict]] = None


@router.post("/text")
async def agronomist_text(req: TextRequest, authorization: str = Header(None)):
    _auth(authorization)
    try:
        answer = groq_client.chat_text(req.question, req.history)
        return {"ok": True, "answer": answer}
    except Exception as e:
        raise HTTPException(500, "Agronomist text error: " + str(e))


@router.post("/audio")
async def agronomist_audio(
    audio: UploadFile = File(...),
    authorization: str = Header(None),
):
    _auth(authorization)
    try:
        audio_bytes = await audio.read()
        if len(audio_bytes) > 25 * 1024 * 1024:
            raise HTTPException(413, "Audio too large (max 25 MB)")
        transcript = groq_client.transcribe_audio(audio_bytes, audio.filename or "audio.m4a")
        if not transcript.strip():
            raise HTTPException(400, "Could not transcribe audio")
        answer = groq_client.chat_text(transcript)
        return {"ok": True, "transcript": transcript, "answer": answer}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "Agronomist audio error: " + str(e))


@router.post("/image")
async def agronomist_image(
    image: UploadFile = File(...),
    question: str = Form(""),
    authorization: str = Header(None),
):
    _auth(authorization)
    try:
        img_bytes = await image.read()
        if len(img_bytes) > 15 * 1024 * 1024:
            raise HTTPException(413, "Image too large (max 15 MB)")
        mime = image.content_type or "image/jpeg"
        answer = groq_client.chat_vision(img_bytes, question, mime)
        return {"ok": True, "answer": answer}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "Agronomist vision error: " + str(e))


@router.post("/file")
async def agronomist_file(
    file: UploadFile = File(...),
    question: str = Form(""),
    authorization: str = Header(None),
):
    _auth(authorization)
    try:
        content = await file.read()
        name = (file.filename or "").lower()
        mime = (file.content_type or "").lower()

        if mime.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp")):
            answer = groq_client.chat_vision(content, question or "Analyze this image.", mime or "image/jpeg")
        elif name.endswith(".pdf"):
            text = groq_client.extract_pdf_text(content)
            prompt = (question or "Summarize this document and advise the farmer.") + "\n\nDocument:\n" + text
            answer = groq_client.chat_text(prompt)
        elif mime.startswith("text/") or name.endswith((".txt", ".md", ".csv")):
            text = content.decode("utf-8", errors="ignore")[:8000]
            prompt = (question or "Summarize this document and advise the farmer.") + "\n\nDocument:\n" + text
            answer = groq_client.chat_text(prompt)
        else:
            raise HTTPException(400, "Unsupported file type: " + (mime or name or "unknown"))

        return {"ok": True, "answer": answer}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "Agronomist file error: " + str(e))
