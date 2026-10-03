import os
import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter()

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"

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
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")

    lang = LANG_NAMES.get((req.language or "en").split("-")[0], "English")

    system = (
        "You are GAIA, an expert African agricultural advisor built by Darkmoor Ltd in Nigeria. "
        "Always respond in " + lang + ". "
        "Keep answers practical, specific, and warm. Use local context: Nigerian crops, "
        "local disease names, affordable treatments in Naira, local plant names where relevant. "
        "Never mention that you are an AI model. You ARE GAIA. "
        "Keep responses under 120 words unless the user asks for more detail. "
        "Speak naturally - your reply will be read aloud."
    )

    messages = [{"role": "system", "content": system}]
    for turn in (req.history or [])[-6:]:
        messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": req.message})

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            GROQ_URL,
            headers={
                "Authorization": "Bearer " + GROQ_KEY,
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 500,
            },
        )

    if r.status_code != 200:
        raise HTTPException(r.status_code, "Chat failed: " + r.text[:300])

    reply = r.json()["choices"][0]["message"]["content"].strip()
    return {"reply": reply}
