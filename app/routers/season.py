import os
import json
import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"

LANG_NAMES = {
    "en": "English", "ha": "Hausa", "yo": "Yoruba",
    "ig": "Igbo", "fr": "French", "sw": "Kiswahili",
}


class BriefRequest(BaseModel):
    crop: str
    stage: str
    days_since_planting: int
    days_to_harvest: int
    state: Optional[str] = None
    language: Optional[str] = "en"
    recent_events: Optional[list] = []
    total_cost_naira: Optional[float] = 0


class HarvestRequest(BaseModel):
    crop: str
    days_since_planting: int
    days_to_harvest: int
    state: Optional[str] = None
    language: Optional[str] = "en"
    recent_events: Optional[list] = []
    market_price_naira_per_kg: Optional[float] = None


@router.post("/season/daily-brief")
async def daily_brief(req: BriefRequest, authorization: str = Header(None)):
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")

    lang = LANG_NAMES.get((req.language or "en").split("-")[0], "English")

    recent = "\n".join([
        f"- Day {e.get('day', '?')}: {e.get('label', '')}"
        for e in (req.recent_events or [])[-8:]
    ]) or "No events logged yet."

    system = (
        "You are GAIA, an expert African agronomist. "
        "Respond in " + lang + ". "
        "Be practical, specific, warm. Use Nigerian context and Naira. "
        "Never say you are an AI. You ARE GAIA. "
        "Reply with exactly two short sections labeled: "
        "TODAY: one specific action the farmer should take today (max 25 words). "
        "WHY: one sentence explaining why (max 25 words). "
        "Do not add any other text."
    )

    user = (
        f"Crop: {req.crop}\n"
        f"Stage: {req.stage}\n"
        f"Days since planting: {req.days_since_planting}\n"
        f"Days to expected harvest: {req.days_to_harvest}\n"
        f"State: {req.state or 'unknown'}\n"
        f"Total spent so far: N{req.total_cost_naira or 0:,.0f}\n"
        f"Recent activity:\n{recent}\n\n"
        "Give today's action."
    )

    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(
            GROQ_URL,
            headers={"Authorization": "Bearer " + GROQ_KEY, "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.4,
                "max_tokens": 220,
            },
        )

    if r.status_code != 200:
        raise HTTPException(r.status_code, "Groq failed: " + r.text[:200])

    text = r.json()["choices"][0]["message"]["content"].strip()

    today = ""
    why = ""
    for line in text.splitlines():
        l = line.strip()
        if l.upper().startswith("TODAY:"):
            today = l.split(":", 1)[1].strip()
        elif l.upper().startswith("WHY:"):
            why = l.split(":", 1)[1].strip()

    if not today:
        today = text[:200]

    return {"ok": True, "action": today, "reasoning": why, "language": req.language}


@router.post("/season/harvest-decision")
async def harvest_decision(req: HarvestRequest, authorization: str = Header(None)):
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")

    lang = LANG_NAMES.get((req.language or "en").split("-")[0], "English")

    system = (
        "You are GAIA, an expert African agronomist. Respond in " + lang + ". "
        "Give a clear harvest recommendation. Be specific about timing. "
        "Never mention AI. Reply with exactly three short sections labeled: "
        "DECISION: HARVEST NOW / WAIT X DAYS / HARVEST WITHIN A WEEK (max 12 words). "
        "REASON: one sentence (max 30 words). "
        "ACTION: one concrete next step (max 20 words)."
    )

    user = (
        f"Crop: {req.crop}\n"
        f"Days since planting: {req.days_since_planting}\n"
        f"Days to expected harvest: {req.days_to_harvest}\n"
        f"State: {req.state or 'unknown'}\n"
        f"Market price: N{req.market_price_naira_per_kg or 'unknown'} per kg\n"
        "Recent field activity:\n" +
        "\n".join([f"- {e.get('label', '')}" for e in (req.recent_events or [])[-6:]])
    )

    async with httpx.AsyncClient(timeout=45) as client:
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
                "max_tokens": 220,
            },
        )

    if r.status_code != 200:
        raise HTTPException(r.status_code, "Groq failed: " + r.text[:200])

    text = r.json()["choices"][0]["message"]["content"].strip()

    decision = reason = action = ""
    for line in text.splitlines():
        l = line.strip()
        if l.upper().startswith("DECISION:"):
            decision = l.split(":", 1)[1].strip()
        elif l.upper().startswith("REASON:"):
            reason = l.split(":", 1)[1].strip()
        elif l.upper().startswith("ACTION:"):
            action = l.split(":", 1)[1].strip()

    if not decision:
        decision = text[:120]

    return {"ok": True, "decision": decision, "reason": reason, "action": action, "language": req.language}
