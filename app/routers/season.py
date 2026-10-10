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


# ============================================================
# LIVING SEASON — live value, forecast, weather, market price
# ============================================================

from datetime import datetime, timedelta


class LiveValueRequest(BaseModel):
    season_id: str
    crop: str
    stage: str
    days_since_planting: int
    days_to_harvest: int
    plot_hectares: Optional[float] = None
    state: Optional[str] = None
    recent_events: Optional[list] = []
    total_cost_naira: Optional[float] = 0
    language: Optional[str] = "en"


CROP_BASE_YIELD_KG_PER_HA = {
    "maize": 2500, "rice": 3500, "sorghum": 1500, "millet": 1200,
    "cowpea (beans)": 1000, "beans": 1000, "soybean": 1500,
    "groundnut": 1200, "tomato": 25000, "pepper": 8000,
    "onion": 20000, "cabbage": 30000, "cassava": 15000, "yam": 12000,
}


@router.post("/season/live-value")
async def live_value(req: LiveValueRequest, authorization: str = Header(None)):
    """
    Return the live value of the standing crop + health + forecast.
    Uses Groq for a farmer-facing narrative; math is deterministic.
    """
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")

    lang = LANG_NAMES.get((req.language or "en").split("-")[0], "English")

    crop_key = req.crop.lower()
    base = CROP_BASE_YIELD_KG_PER_HA.get(crop_key, 2000)
    ha = req.plot_hectares or 1.0

    # Disease / pest penalties from recent events
    disease_hits = sum(1 for e in (req.recent_events or [])
                       if e.get("kind") in ("disease", "pest"))
    spray_hits = sum(1 for e in (req.recent_events or [])
                     if e.get("kind") == "sprayed")
    net_pressure = max(0, disease_hits - spray_hits)

    # Stage progress from days
    total_days = max(1, req.days_since_planting + max(0, req.days_to_harvest))
    stage_frac = min(1.0, req.days_since_planting / total_days)

    # Projected yield: base * ha * progress adjustments
    stage_multiplier = min(1.0, 0.35 + 0.65 * stage_frac)  # early = smaller accumulated value
    pressure_penalty = max(0.55, 1.0 - 0.08 * net_pressure)
    forecast_kg = round(base * ha * stage_multiplier * pressure_penalty, 0)

    # Health score
    health = int(max(30, min(100, 90 - 12 * net_pressure + 5 * spray_hits)))

    # Loss estimate from untreated pressure
    loss_kg = round(base * ha * 0.08 * net_pressure, 0)

    # Price per kg by crop (Nigerian mid-2026 reference)
    PRICES = {
        "maize": 380, "rice": 950, "sorghum": 420, "millet": 460,
        "cowpea (beans)": 780, "beans": 780, "soybean": 620,
        "groundnut": 700, "tomato": 850, "pepper": 900,
        "onion": 700, "cabbage": 450, "cassava": 180, "yam": 500,
    }
    price = PRICES.get(crop_key, 400)

    # Ask Groq for a one-line narrative + action
    system = (
        "You are GAIA, a Nigerian agronomist. Respond in " + lang + ". "
        "Be brief, warm, specific. Never mention AI. "
        "Reply with exactly two sections labeled 'HEADLINE:' and 'ACTION:'. "
        "HEADLINE max 15 words. ACTION max 18 words. Nothing else."
    )
    user = (
        f"Crop: {req.crop}. Stage: {req.stage}. "
        f"Day {req.days_since_planting}. {req.days_to_harvest} days to harvest. "
        f"Forecast yield: {forecast_kg} kg. Value: N{forecast_kg * price:,.0f}. "
        f"Health: {health}/100. Recent disease events: {disease_hits}, sprays: {spray_hits}."
    )

    headline = ""
    action = ""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                GROQ_URL,
                headers={"Authorization": "Bearer " + GROQ_KEY, "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.5,
                    "max_tokens": 120,
                },
            )
        if r.status_code == 200:
            text = r.json()["choices"][0]["message"]["content"].strip()
            for line in text.splitlines():
                l = line.strip()
                if l.upper().startswith("HEADLINE:"):
                    headline = l.split(":", 1)[1].strip()
                elif l.upper().startswith("ACTION:"):
                    action = l.split(":", 1)[1].strip()
    except Exception:
        pass

    if not headline:
        headline = f"Your {req.crop} is on track."
    if not action:
        action = "Keep scouting weekly for pests and disease."

    return {
        "ok": True,
        "forecast_kg": forecast_kg,
        "price_per_kg_naira": price,
        "live_value_naira": forecast_kg * price,
        "health_score": health,
        "loss_estimate_naira": loss_kg * price,
        "loss_kg": loss_kg,
        "headline": headline,
        "action": action,
        "language": req.language,
    }


class WeatherRequest(BaseModel):
    lat: float
    lng: float


@router.post("/season/weather")
async def season_weather(req: WeatherRequest):
    """14-day forecast from Open-Meteo. Free, no key."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": req.lat,
        "longitude": req.lng,
        "daily": ["temperature_2m_max", "temperature_2m_min",
                  "precipitation_sum", "relative_humidity_2m_max"],
        "forecast_days": 14,
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            raise HTTPException(r.status_code, "weather failed")
        d = r.json().get("daily", {})
        days = []
        for i in range(len(d.get("time", []))):
            days.append({
                "date": d["time"][i],
                "t_max": d["temperature_2m_max"][i],
                "t_min": d["temperature_2m_min"][i],
                "rain_mm": d["precipitation_sum"][i],
                "humidity": d["relative_humidity_2m_max"][i],
            })
        total_rain = sum(day["rain_mm"] or 0 for day in days)
        return {"ok": True, "days": days, "total_rain_14d_mm": round(total_rain, 1)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "weather: " + str(e)[:120])


# ============================================================
# FIELD RADIO — 30 min segments of weather + action + price + tip
# ============================================================

class RadioRequest(BaseModel):
    crop: str
    stage: str
    days_since_planting: int
    days_to_harvest: int
    state: Optional[str] = None
    language: Optional[str] = "en"
    recent_events: Optional[list] = []
    weather: Optional[dict] = None
    market_price_naira_per_kg: Optional[float] = None


@router.post("/season/radio-segment")
async def radio_segment(req: RadioRequest, authorization: str = Header(None)):
    """Return a 45-90 second radio segment as structured text for TTS."""
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")

    lang = LANG_NAMES.get((req.language or "en").split("-")[0], "English")

    system = (
        "You are GAIA, a Nigerian farm radio host. Respond in " + lang + ". "
        "Warm, clear, unhurried — like a real radio presenter. "
        "Never mention AI. Read like a spoken segment, not written text. "
        "Reply with EXACTLY five sections, each on its own line: "
        "WEATHER: one sentence about next 3 days (max 18 words). "
        "ACTION: the single most important task today (max 18 words). "
        "PRICE: current market price and trend (max 15 words). "
        "TIP: one agronomy tip for this stage (max 20 words). "
        "SIGN_OFF: one warm closing sentence (max 12 words)."
    )

    recent = ", ".join([e.get("label", "") for e in (req.recent_events or [])[-5:]]) or "no recent activity"
    weather_summary = "no weather data"
    if req.weather and req.weather.get("days"):
        days = req.weather["days"][:3]
        weather_summary = " | ".join(
            f"{d['date']}: {d['t_max']:.0f}C {d.get('rain_mm', 0):.0f}mm" for d in days
        )

    user = (
        f"Crop: {req.crop}. Stage: {req.stage}. "
        f"Day {req.days_since_planting} since planting. "
        f"{req.days_to_harvest} days to harvest. "
        f"State: {req.state or 'Nigeria'}. "
        f"Weather: {weather_summary}. "
        f"Recent: {recent}. "
        f"Market price today: N{req.market_price_naira_per_kg or 'unknown'} per kg."
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
                "temperature": 0.55,
                "max_tokens": 300,
            },
        )

    if r.status_code != 200:
        raise HTTPException(r.status_code, "Groq failed: " + r.text[:200])

    text = r.json()["choices"][0]["message"]["content"].strip()

    segments = {"weather": "", "action": "", "price": "", "tip": "", "sign_off": ""}
    for line in text.splitlines():
        l = line.strip()
        for key, label in [("weather", "WEATHER:"), ("action", "ACTION:"),
                           ("price", "PRICE:"), ("tip", "TIP:"),
                           ("sign_off", "SIGN_OFF:")]:
            if l.upper().startswith(label):
                segments[key] = l.split(":", 1)[1].strip()
                break

    full_text = (
        segments["weather"] + ". " +
        segments["action"] + ". " +
        segments["price"] + ". " +
        segments["tip"] + ". " +
        segments["sign_off"]
    ).strip()

    return {
        "ok": True,
        "segments": segments,
        "full_text": full_text,
        "language": req.language,
        "duration_estimate_sec": max(30, len(full_text) // 14),
    }


# ============================================================
# SOS — sends SMS to emergency contacts via Termii
# ============================================================

class SOSRequest(BaseModel):
    lat: float
    lng: float
    kind: str = "general"
    message: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    emergency_contacts: Optional[list] = []


def _normalize_ng_phone(p: str) -> str:
    if not p: return ""
    p = p.replace("+", "").replace(" ", "").replace("-", "").strip()
    if p.startswith("0"): return "234" + p[1:]
    if p.startswith("234"): return p
    return "234" + p


@router.post("/sos")
async def sos(req: SOSRequest, authorization: str = Header(None)):
    """Send SOS SMS to emergency contacts. Fire and forget."""
    termii_key = os.environ.get("TERMII_API_KEY", "")
    if not termii_key:
        return {"ok": False, "reason": "SMS not configured"}

    maps_url = f"https://maps.google.com/?q={req.lat},{req.lng}"
    who = req.user_name or req.user_email or "A GAIA farmer"
    body = (
        f"GAIA SOS from {who}. "
        f"Kind: {req.kind}. "
        f"{('Message: ' + req.message + '. ') if req.message else ''}"
        f"Location: {maps_url}"
    )[:300]

    sent = 0
    failed = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for c in (req.emergency_contacts or []):
            phone = _normalize_ng_phone(c.get("phone", ""))
            if not phone or len(phone) < 12:
                continue
            try:
                r = await client.post(
                    "https://api.ng.termii.com/api/sms/send",
                    json={
                        "api_key": termii_key,
                        "to": phone,
                        "from": "GAIA",
                        "sms": body,
                        "type": "plain",
                        "channel": "generic",
                    },
                )
                if r.status_code == 200:
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

    return {"ok": True, "sent": sent, "failed": failed, "body_preview": body[:140]}


# ============================================================
# SOS — sends SMS to emergency contacts via Termii
# ============================================================

class SOSRequest(BaseModel):
    lat: float
    lng: float
    kind: str = "general"
    message: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    emergency_contacts: Optional[list] = []


def _normalize_ng_phone(p: str) -> str:
    if not p: return ""
    p = p.replace("+", "").replace(" ", "").replace("-", "").strip()
    if p.startswith("0"): return "234" + p[1:]
    if p.startswith("234"): return p
    return "234" + p


@router.post("/sos")
async def sos(req: SOSRequest, authorization: str = Header(None)):
    """Send SOS SMS to emergency contacts. Fire and forget."""
    termii_key = os.environ.get("TERMII_API_KEY", "")
    if not termii_key:
        return {"ok": False, "reason": "SMS not configured"}

    maps_url = f"https://maps.google.com/?q={req.lat},{req.lng}"
    who = req.user_name or req.user_email or "A GAIA farmer"
    body = (
        f"GAIA SOS from {who}. "
        f"Kind: {req.kind}. "
        f"{('Message: ' + req.message + '. ') if req.message else ''}"
        f"Location: {maps_url}"
    )[:300]

    sent = 0
    failed = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for c in (req.emergency_contacts or []):
            phone = _normalize_ng_phone(c.get("phone", ""))
            if not phone or len(phone) < 12:
                continue
            try:
                r = await client.post(
                    "https://api.ng.termii.com/api/sms/send",
                    json={
                        "api_key": termii_key,
                        "to": phone,
                        "from": "GAIA",
                        "sms": body,
                        "type": "plain",
                        "channel": "generic",
                    },
                )
                if r.status_code == 200:
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

    return {"ok": True, "sent": sent, "failed": failed, "body_preview": body[:140]}
