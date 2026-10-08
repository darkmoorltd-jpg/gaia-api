# app/routers/translate.py
# Translate lesson text into Nigerian languages via Groq
import os
import httpx
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger('gaia.translate')
router = APIRouter()

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"


class TranslateBody(BaseModel):
    text: str
    target: str
    context: str = "agricultural training lesson for Nigerian farmers"


LANG_NAME = {
    "ha": "Hausa",
    "yo": "Yoruba",
    "ig": "Igbo",
    "pcm": "Nigerian Pidgin English",
}


@router.post("/translate")
async def translate(body: TranslateBody):
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")

    target = (body.target or "").lower()[:3]
    lang = LANG_NAME.get(target)
    if not lang:
        raise HTTPException(400, "Unsupported target language: " + target)

    system = (
        "You are a professional translator for Nigerian agricultural content. "
        "Translate faithfully into " + lang + ". Keep technical terms that don't have "
        "an established local translation in English (NPK 15-15-15, Urea, Fall Armyworm "
        "when commonly used in English). Use natural, farmer-friendly tone. "
        "Return ONLY the translated text. No explanations, no quotes around it."
    )

    user = "Context: " + body.context + "\n\nTranslate this to " + lang + ":\n\n" + body.text

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            GROQ_URL,
            headers={"Authorization": "Bearer " + GROQ_KEY, "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.3,
                "max_tokens": 2000,
            },
        )

    if r.status_code != 200:
        raise HTTPException(r.status_code, "Groq failed: " + r.text[:200])

    out = r.json()["choices"][0]["message"]["content"].strip()
    return {"ok": True, "language": target, "translation": out}
