# app/routers/farming_calendar.py
# AI farming calendar with organic + inorganic solutions per activity
import os
import re
import json
import httpx
from datetime import datetime, timedelta
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.services.auth import verify_supabase_token

router = APIRouter()

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"


class CalendarReq(BaseModel):
    crop: str
    crop_group: Optional[str] = None
    planting_date: str
    location: str
    state: Optional[str] = None
    hectares: float = 1.0
    lat: Optional[float] = None
    lon: Optional[float] = None


def auth_user(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        return verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))


async def geocode(location: str):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": location + ", Nigeria", "format": "json", "limit": 1},
                headers={"User-Agent": "GAIA-Agriculture/1.0"},
            )
        if r.status_code == 200 and r.json():
            return float(r.json()[0]["lat"]), float(r.json()[0]["lon"])
    except Exception:
        pass
    return None, None


async def climate_summary(lat, lon, planting_date_str):
    try:
        start = datetime.fromisoformat(planting_date_str)
        end = start + timedelta(days=140)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                "https://climate-api.open-meteo.com/v1/climate",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "start_date": start.strftime("%Y-%m-%d"),
                    "end_date": end.strftime("%Y-%m-%d"),
                    "daily": "temperature_2m_mean,precipitation_sum",
                    "models": "EC_Earth3P_HR",
                    "timezone": "auto",
                },
            )
        if r.status_code != 200:
            return None
        data = r.json().get("daily", {})
        times = data.get("time", [])
        temps = data.get("temperature_2m_mean", [])
        precs = data.get("precipitation_sum", [])
        if not times:
            return None
        monthly = {}
        for d, t, p in zip(times, temps, precs):
            m = d[:7]
            if m not in monthly:
                monthly[m] = {"t": [], "p": []}
            if t is not None:
                monthly[m]["t"].append(t)
            if p is not None:
                monthly[m]["p"].append(p)
        lines = []
        for m, vals in sorted(monthly.items()):
            avg_t = sum(vals["t"]) / len(vals["t"]) if vals["t"] else 0
            total_p = sum(vals["p"])
            lines.append(m + ": avg " + format(avg_t, ".1f") + "C, " + format(total_p, ".0f") + "mm rain")
        return "\n".join(lines)
    except Exception:
        return None


FALLBACK_TEMPLATES = {
    "Maize": [
        {"week": 0, "type": "land", "title": "Land prep",
         "activity": "Plough and harrow to 20cm depth",
         "organic": "Incorporate 5t/ha compost or poultry manure 2 weeks before planting",
         "inorganic": "Apply glyphosate 1L/ha 2 weeks before ploughing if weeds are heavy"},
        {"week": 0, "type": "fertilizer", "title": "Basal fertilizer",
         "activity": "Apply at planting for early root growth",
         "organic": "2t/ha poultry manure or 5t/ha compost in planting furrow",
         "inorganic": "NPK 15:15:15 at 200kg/ha (4 x 50kg bags). N40,000 per bag in 2025"},
        {"week": 0, "type": "planting", "title": "Plant seed",
         "activity": "Plant SAMMAZ 52 at 75cm x 25cm, 2 seeds/hole",
         "organic": "Treat seed with wood ash slurry before planting",
         "inorganic": "Treat seed with Apron Star at 10g per 4kg seed"},
        {"week": 4, "type": "fertilizer", "title": "First top-dress",
         "activity": "Apply nitrogen for vegetative growth",
         "organic": "Side-dress 1t/ha poultry manure or liquid manure tea",
         "inorganic": "Urea 46% at 100kg/ha. N30,000 per bag"},
        {"week": 5, "type": "pest", "title": "Fall armyworm scouting",
         "activity": "Scout 20 plants per field, count damaged whorls",
         "organic": "Neem oil 3% spray at dusk. Wood ash in whorls. Handpick larvae.",
         "inorganic": "Emamectin benzoate 1.9EC at 200ml/ha if over 20% damage. N4,500/L"},
        {"week": 6, "type": "weed", "title": "First weeding",
         "activity": "Remove weeds before they compete for nutrients",
         "organic": "Hand hoe or mechanical weeding",
         "inorganic": "Atrazine 80WP at 2kg/ha pre-emergence (apply before weeds emerge)"},
        {"week": 8, "type": "fertilizer", "title": "Second top-dress",
         "activity": "Support ear formation",
         "organic": "Foliar spray with vermicompost tea",
         "inorganic": "Urea 46% at 50kg/ha + Muriate of Potash 50kg/ha"},
        {"week": 9, "type": "weed", "title": "Second weeding",
         "activity": "Keep field clean during ear formation",
         "organic": "Manual weeding around plant base",
         "inorganic": "Glyphosate 1L/ha directed spray between rows"},
        {"week": 11, "type": "disease", "title": "Northern Leaf Blight check",
         "activity": "Inspect lower leaves for grey-green lesions",
         "organic": "Bacillus subtilis spray. Remove and burn infected leaves.",
         "inorganic": "Mancozeb 80WP at 2.5kg/ha every 10 days. N35,000 per bag"},
        {"week": 12, "type": "pest", "title": "Stem borer check",
         "activity": "Look for dead hearts and tunnelling in stems",
         "organic": "Release Trichogramma wasps. Apply wood ash in whorls.",
         "inorganic": "Cypermethrin 10EC at 500ml/ha"},
        {"week": 14, "type": "harvest", "title": "Harvest",
         "activity": "Harvest at physiological maturity, 13% moisture",
         "organic": "Hand harvest, dry on raised platform for aflatoxin prevention",
         "inorganic": "Mechanical harvest if available"},
        {"week": 15, "type": "postharvest", "title": "Storage",
         "activity": "Dry to 13%, store in airtight PICS bags",
         "organic": "Aflasafe biocontrol 3 weeks before harvest. Neem leaves in storage.",
         "inorganic": "Actellic dust at 50g per 100kg grain"},
    ],
    "Rice": [
        {"week": 0, "type": "land", "title": "Land prep", "activity": "Puddle and level field", "organic": "Green manure (Sesbania) ploughed under", "inorganic": "Glyphosate 1L/ha before puddling"},
        {"week": 0, "type": "planting", "title": "Nursery", "activity": "Sow FARO 44 in nursery, 40kg/ha seed rate", "organic": "Compost-based seedbed", "inorganic": "Soak seed in 0.1% Carbendazim"},
        {"week": 3, "type": "planting", "title": "Transplant", "activity": "Transplant 20cm x 20cm, 2-3 seedlings/hill", "organic": "Push with organic amendment at base", "inorganic": "Apply 100kg/ha DAP at transplant"},
        {"week": 5, "type": "fertilizer", "title": "First top-dress", "activity": "Nitrogen split", "organic": "Azolla cover or green manure top-dress", "inorganic": "Urea 46% at 65kg/ha"},
        {"week": 7, "type": "pest", "title": "Rice stem borer", "activity": "Scout for dead hearts", "organic": "Trichogramma release", "inorganic": "Cartap hydrochloride 4G at 18kg/ha"},
        {"week": 8, "type": "disease", "title": "Rice blast", "activity": "Check for diamond lesions", "organic": "Silicon amendment (rice husk ash)", "inorganic": "Tricyclazole 75WP at 300g/ha at booting"},
        {"week": 10, "type": "fertilizer", "title": "Second top-dress", "activity": "Panicle initiation", "organic": "Liquid manure spray", "inorganic": "Urea 46% at 35kg/ha"},
        {"week": 14, "type": "harvest", "title": "Harvest", "activity": "Harvest at 80% golden grains", "organic": "Hand harvest, dry on mats", "inorganic": "Combine harvest if available"},
    ],
    "Beans": [
        {"week": 0, "type": "land", "title": "Land prep", "activity": "Make ridges 60cm apart", "organic": "Compost 2t/ha incorporated", "inorganic": "Single Super Phosphate 100kg/ha"},
        {"week": 0, "type": "planting", "title": "Plant", "activity": "Plant IT96D-610 at 10cm x 60cm", "organic": "Inoculate seed with rhizobium", "inorganic": "Treat seed with Apron Star"},
        {"week": 2, "type": "weed", "title": "First weeding", "activity": "Clear weeds", "organic": "Hand hoeing", "inorganic": "Pendimethalin 1.5L/ha pre-emergence"},
        {"week": 3, "type": "fertilizer", "title": "Starter fertilizer", "activity": "Support early growth", "organic": "Liquid manure tea at 200L/ha", "inorganic": "NPK 15:15:15 at 100kg/ha"},
        {"week": 4, "type": "pest", "title": "Aphid check", "activity": "Inspect leaf undersides", "organic": "Neem oil 3% spray, ladybird release", "inorganic": "Imidacloprid 200SL at 250ml/ha"},
        {"week": 6, "type": "disease", "title": "Angular leaf spot", "activity": "Check for angular brown spots", "organic": "Bacillus-based biofungicide", "inorganic": "Mancozeb 80WP at 2kg/ha"},
        {"week": 9, "type": "harvest", "title": "Harvest", "activity": "Harvest when pods turn yellow", "organic": "Hand pick, sun dry", "inorganic": "Mechanical thresh if available"},
    ],
    "Tomato": [
        {"week": 0, "type": "planting", "title": "Nursery", "activity": "Sow Roma VF in nursery", "organic": "Compost-based seedbed", "inorganic": "Drench with Ridomil at transplant"},
        {"week": 3, "type": "planting", "title": "Transplant", "activity": "Transplant 60cm x 60cm", "organic": "Add 1kg compost per planting hole", "inorganic": "Apply 100kg/ha NPK 15:15:15 at base"},
        {"week": 5, "type": "crop", "title": "Staking", "activity": "Install stakes, tie plants", "organic": None, "inorganic": None},
        {"week": 6, "type": "disease", "title": "Early blight", "activity": "Inspect lower leaves for concentric rings", "organic": "Bacillus subtilis spray weekly", "inorganic": "Chlorothalonil 2kg/ha every 10 days"},
        {"week": 7, "type": "fertilizer", "title": "Top-dress", "activity": "Support fruiting", "organic": "Compost side-dress + foliar seaweed", "inorganic": "Calcium nitrate at 100kg/ha. N45,000 per bag"},
        {"week": 8, "type": "pest", "title": "Spider mite", "activity": "Check undersides of leaves", "organic": "Neem oil + insecticidal soap", "inorganic": "Abamectin 1.8EC at 400ml/ha"},
        {"week": 11, "type": "harvest", "title": "Harvest", "activity": "Pick at breaker stage for transport", "organic": "Hand pick into clean crates", "inorganic": None},
    ],
    "Pepper": [
        {"week": 0, "type": "planting", "title": "Nursery", "activity": "Sow pepper in nursery", "organic": "Compost seedbed", "inorganic": "Seed treat with Thiram"},
        {"week": 4, "type": "planting", "title": "Transplant", "activity": "Transplant 60cm x 45cm", "organic": "Compost at base", "inorganic": "100kg/ha NPK 15:15:15"},
        {"week": 6, "type": "pest", "title": "Thrips", "activity": "Check flowers for silvering", "organic": "Neem spray + blue sticky traps", "inorganic": "Spinosad 250ml/ha"},
        {"week": 9, "type": "disease", "title": "Bacterial spot", "activity": "Check leaves for water-soaked spots", "organic": "Copper soap spray", "inorganic": "Copper hydroxide 2g/L"},
        {"week": 12, "type": "harvest", "title": "Harvest", "activity": "Pick firm fruits", "organic": "Hand pick", "inorganic": None},
    ],
    "Cabbage": [
        {"week": 0, "type": "planting", "title": "Nursery", "activity": "Sow cabbage seed", "organic": "Compost seedbed", "inorganic": "Drench with Previcur"},
        {"week": 4, "type": "planting", "title": "Transplant", "activity": "Transplant 45cm x 45cm", "organic": "Compost 2kg per hole", "inorganic": "NPK 15:15:15 at 200kg/ha"},
        {"week": 6, "type": "pest", "title": "Diamondback moth", "activity": "Check undersides of leaves", "organic": "Bacillus thuringiensis spray", "inorganic": "Emamectin benzoate 200ml/ha"},
        {"week": 9, "type": "disease", "title": "Black rot", "activity": "Check leaf margins for V-shaped yellow", "organic": "Copper spray", "inorganic": "Copper hydroxide 2g/L"},
        {"week": 14, "type": "harvest", "title": "Harvest", "activity": "Harvest firm heads", "organic": "Hand cut", "inorganic": None},
    ],
}


def fallback_for(crop: str):
    return FALLBACK_TEMPLATES.get(crop)


@router.post("/calendar/generate")
async def calendar_generate(req: CalendarReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]

    if not req.crop or not req.planting_date or not req.location:
        raise HTTPException(400, "crop, planting_date, and location required")

    lat, lon = (req.lat, req.lon)
    if lat is None or lon is None:
        lat, lon = await geocode(req.location)

    climate = None
    if lat is not None and lon is not None:
        climate = await climate_summary(lat, lon, req.planting_date)

    if not GROQ_KEY:
        fb = fallback_for(req.crop)
        if fb:
            return {"ok": True, "source": "fallback", "activities": fb}
        raise HTTPException(500, "AI unavailable and no fallback for this crop")

    climate_block = climate or ("(no climate data available, use typical Nigerian seasonal patterns for " + req.location + ")")

    prompt = """Generate a week-by-week farming calendar for the following Nigerian farm.

CROP: """ + req.crop + """
CROP GROUP: """ + (req.crop_group or "unknown") + """
PLANTING DATE: """ + req.planting_date + """
LOCATION: """ + req.location + (", " + req.state if req.state else "") + """
FARM SIZE: """ + format(req.hectares, ".2f") + """ hectares

CLIMATE (monthly averages for the growing season):
""" + climate_block + """

Return ONLY a JSON array of 12-18 activities. No prose. No markdown. No explanation. Just the array.

Each activity must match this exact schema:
{
  "week": integer,
  "type": "land" | "planting" | "fertilizer" | "pest" | "disease" | "water" | "weed" | "harvest" | "postharvest" | "crop",
  "title": "short 2-4 word title",
  "activity": "specific instruction for what to do this week",
  "organic": "organic/natural/low-cost solution using locally available materials (neem, wood ash, compost, manure, biological control, crop rotation, handpicking, etc.) with specific quantities",
  "inorganic": "commercial/chemical solution with REAL product names sold in Nigeria, correct dose (kg/ha, ml/ha, L/ha), and Naira cost where relevant"
}

STRICT RULES:
- Cover the FULL season: land prep, planting, fertilizer(s), weeding, pest checks, disease checks, harvest, postharvest
- For EVERY fertilizer, pest, and disease activity, provide BOTH organic AND inorganic solutions
- For land/planting/weed/harvest activities, organic is REQUIRED and inorganic may be null
- Use REAL Nigerian product names: NPK 15:15:15, Urea 46%, Emamectin benzoate 1.9EC, Mancozeb 80WP, Tricyclazole 75WP, Apron Star, Aflasafe, Glyphosate, Atrazine, Imidacloprid, Abamectin, Copper hydroxide, Metalaxyl, etc.
- Use REAL quantities: kg/ha, ml/ha, L/ha, bags, t/ha
- Use REAL Naira prices from 2025/2026 where relevant
- Use REAL Nigerian variety names where relevant: SAMMAZ 52 (maize), FARO 44 (rice), IT96D-610 (cowpea), TGX 1835 (soybean), TMS 30572 (cassava), Roma VF (tomato), etc.
- Reference the climate data. If a month shows high rain, note disease timing. If dry, note irrigation needs.
- Be specific, practical, actionable. This is for a smallholder farmer.
- Return ONLY the JSON array. Nothing before or after."""

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                GROQ_URL,
                headers={"Authorization": "Bearer " + GROQ_KEY, "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": "You are GAIA, an expert Nigerian agricultural advisor. Reply only with valid JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.5,
                    "max_tokens": 4000,
                },
            )
        if r.status_code != 200:
            raise Exception("Groq " + str(r.status_code))
        content = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\[.*\]", content, re.DOTALL)
        if not m:
            raise Exception("no JSON in response")
        activities = json.loads(m.group())
    except Exception as e:
        fb = fallback_for(req.crop)
        if fb:
            return {"ok": True, "source": "fallback", "note": str(e)[:200], "activities": fb}
        raise HTTPException(502, "Calendar generation failed: " + str(e)[:200])

    try:
        start = datetime.fromisoformat(req.planting_date).date()
        for a in activities:
            w = int(a.get("week", 0))
            a["date"] = (start + timedelta(weeks=w)).isoformat()
    except Exception:
        pass

    return {
        "ok": True,
        "source": "ai",
        "crop": req.crop,
        "location": req.location,
        "lat": lat,
        "lon": lon,
        "climate_used": bool(climate),
        "activities": activities,
    }
