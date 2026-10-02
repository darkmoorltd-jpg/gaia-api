import os
import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter()
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

LANG_NAMES = {
    "en": "English", "ha": "Hausa", "yo": "Yoruba",
    "ig": "Igbo", "fr": "French", "sw": "Kiswahili",
}


class ChatTurn(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    language: Optional[str] = "en"
    history: Optional[List[ChatTurn]] = []


@router.post("/chat")
async def chat(req: ChatRequest, authorization: str = Header(None)):
    if not DEEPSEEK_KEY:
        raise HTTPException(500, "DEEPSEEK_API_KEY not configured")

    lang = LANG_NAMES.get((req.language or "en").split("-")[0], "English")

    system = (
        "You are GAIA, an expert African agricultural advisor built by Darkmoor Ltd in Nigeria. "
        f"Always respond in {lang}. "
        "Keep answers practical, specific, and warm. Use local context: Nigerian crops, "
        "local disease names, affordable treatments (₦ prices), Hausa/Yoruba/Igbo plant names where relevant. "
        "Never mention you are an AI model. You ARE GAIA. "
        "Keep responses under 120 words unless the user asks for detail. Speak naturally — your reply will be read aloud."
    )

    messages = [{"role": "system", "content": system}]
    for turn in (req.history or [])[-6:]:
        messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": req.message})

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": "Bearer " + DEEPSEEK_KEY,
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 500,
            },
        )

    if r.status_code != 200:
        raise HTTPException(r.status_code, "Chat failed: " + r.text[:200])

    reply = r.json()["choices"][0]["message"]["content"].strip()
    return {"reply": reply}
