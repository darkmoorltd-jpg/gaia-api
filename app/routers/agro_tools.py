import os
import json
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"


class ToolRequest(BaseModel):
    tool: str
    crop: str
    state: str
    hectares: Optional[float] = 1.0
    budget_naira: Optional[float] = 0
    growth_stage: Optional[str] = None
    planting_month: Optional[str] = None
    notes: Optional[str] = None


BASE_RULES = (
    "You are GAIA, an expert Nigerian agronomist and farm economist. "
    "Use REAL current Nigerian market prices (2025/2026 averages) for inputs and produce. "
    "Use REAL fertilizer products sold in Nigeria (NPK 15:15:15, NPK 20:10:10, Urea 46% N, "
    "Single Super Phosphate, Muriate of Potash, Calcium Ammonium Nitrate). "
    "Use REAL seed varieties (SAMMAZ, Oba Super for maize; FARO 44/52 for rice; "
    "TGX for soybean; TMS for cassava). Prices in Nigerian Naira only. "
    "Be specific, practical, realistic. Never invent unrealistic numbers. "
    "Reply ONLY with valid JSON matching the requested schema. No prose outside JSON."
)

SCHEMAS = {
    "yield": """{
  "crop": string,
  "state": string,
  "hectares": number,
  "expected_yield_kg": number,
  "yield_per_hectare_kg": number,
  "price_per_kg_naira": number,
  "gross_revenue_naira": number,
  "yield_range_low_kg": number,
  "yield_range_high_kg": number,
  "confidence": "low"|"medium"|"high",
  "limiting_factors": [string],
  "boosters": [string],
  "notes": [string]
}""",

    "fertilizer": """{
  "crop": string,
  "hectares": number,
  "stages": [
    {
      "stage": string,
      "weeks_after_planting": number,
      "product": string,
      "npk_ratio": string,
      "quantity_kg": number,
      "bags_50kg": number,
      "cost_naira": number,
      "application_method": string
    }
  ],
  "total_cost_naira": number,
  "total_nutrients": {"N_kg": number, "P_kg": number, "K_kg": number},
  "organic_alternatives": [string],
  "notes": [string]
}""",

    "profit": """{
  "crop": string,
  "hectares": number,
  "revenue_naira": number,
  "expected_yield_kg": number,
  "price_per_kg_naira": number,
  "costs": [
    {"item": string, "amount_naira": number, "category": "seed"|"fertilizer"|"labor"|"chemicals"|"land"|"transport"|"other"}
  ],
  "total_cost_naira": number,
  "gross_margin_naira": number,
  "margin_percent": number,
  "break_even_kg": number,
  "break_even_price_naira": number,
  "roi_percent": number,
  "risk_factors": [string],
  "notes": [string]
}""",

    "calendar": """{
  "crop": string,
  "state": string,
  "planting_window": string,
  "harvest_window": string,
  "duration_days": number,
  "activities": [
    {"week": number, "phase": string, "task": string, "category": "land"|"planting"|"fertilizer"|"weed"|"pest"|"disease"|"water"|"harvest"|"postharvest"}
  ],
  "climate_notes": [string],
  "notes": [string]
}""",

    "seed": """{
  "crop": string,
  "state": string,
  "varieties": [
    {
      "name": string,
      "maturity_days": number,
      "yield_potential_kg_per_ha": number,
      "disease_resistance": [string],
      "drought_tolerance": "low"|"medium"|"high",
      "recommended_zone": string,
      "price_per_kg_naira": number,
      "seed_rate_kg_per_ha": number,
      "where_to_buy": [string]
    }
  ],
  "top_pick": string,
  "notes": [string]
}"""
}


@router.post("/agro-tools")
async def agro_tools(req: ToolRequest):
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured")

    tool = (req.tool or "").lower().strip()
    if tool not in SCHEMAS:
        raise HTTPException(400, "Unknown tool: " + tool)

    ctx = (
        "Tool: " + tool + "\n"
        "Crop: " + req.crop + "\n"
        "State: " + req.state + "\n"
        "Farm size: " + str(req.hectares or 1.0) + " hectares\n"
    )
    if req.budget_naira:
        ctx += "Budget: N" + str(int(req.budget_naira)) + "\n"
    if req.growth_stage:
        ctx += "Growth stage: " + req.growth_stage + "\n"
    if req.planting_month:
        ctx += "Planting month: " + req.planting_month + "\n"
    if req.notes:
        ctx += "Extra notes from farmer: " + req.notes + "\n"

    user_prompt = (
        "Generate the " + tool + " analysis for this Nigerian farm.\n\n"
        + ctx
        + "\nReturn ONLY a JSON object with EXACTLY this schema (no extra fields, no prose):\n"
        + SCHEMAS[tool]
    )

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            GROQ_URL,
            headers={
                "Authorization": "Bearer " + GROQ_KEY,
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": BASE_RULES},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 2000,
                "response_format": {"type": "json_object"},
            },
        )

    if r.status_code != 200:
        raise HTTPException(r.status_code, "Groq failed: " + r.text[:300])

    raw = r.json()["choices"][0]["message"]["content"]
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(502, "Groq returned non-JSON: " + raw[:200])

    return {"ok": True, "tool": tool, "data": data}
