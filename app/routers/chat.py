from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import List
import os
import requests

router = APIRouter()

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    max_tokens: int = 1500


class ChatResponse(BaseModel):
    reply: str


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, authorization: str = Header(None)):
    if not DEEPSEEK_API_KEY:
        raise HTTPException(500, "DEEPSEEK_API_KEY not configured")
    if not authorization:
        raise HTTPException(401, "Missing Bearer token")

    system = {
        "role": "system",
        "content": (
            "You are GAIA, an expert African agricultural advisor built by "
            "Darkmoor Ltd in Nigeria. Answer concisely and practically. "
            "Use Nigerian context: local crop names, market prices in Naira, "
            "state extension programmes. Never mention any AI company."
        ),
    }
    messages = [system] + [m.dict() for m in req.messages]

    r = requests.post(
        DEEPSEEK_URL,
        headers={
            "Authorization": "Bearer " + DEEPSEEK_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "model": "deepseek-chat",
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": 0.7,
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise HTTPException(500, "Upstream error: " + r.text[:200])
    data = r.json()
    return ChatResponse(reply=data["choices"][0]["message"]["content"])
