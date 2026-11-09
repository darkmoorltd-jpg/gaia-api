import os
import base64
import requests
from fastapi import APIRouter, File, UploadFile, Form, HTTPException
from pydantic import BaseModel
from supabase import create_client
from app.utils import robase

router = APIRouter()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _supabase():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ============================================================
# 1. PRECISION MEAL VISION (Qwen 3.8 via Groq)
# ============================================================
@router.post("/clinical/analyze-meal")
async def analyze_meal(
    file: UploadFile = File(...),
    condition: str = Form(...)
):
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="Groq API key missing")

    contents = await file.read()
    base64_image = base64.b64encode(contents).decode("utf-8")

    prompt = f"""You are Dr. Vinc, a mathematically precise clinical nutritionist in Nigeria.
The patient has: {condition}.
Analyze this food image carefully.

You MUST follow these steps strictly:
1. FOOD ITEMS & WEIGHTS: List every item you see. You MUST estimate the weight of each item in grams. Use visual cues (plate size, utensils, standard Nigerian portions). Example: "White rice - 180g", "Fried chicken - 120g", "Palm oil - 15g", "Beans - 50g".
2. MACRO CALCULATIONS: Based on your estimated weights, calculate the exact Carbs, Protein, Fat, and Calories.
3. PORTION ADVICE: Is this too large/small for a {condition} patient?
4. CLINICAL WARNING: How will these specific weights affect their {condition}?
5. RECOMMENDED SUBSTITUTE: What should they eat instead in Nigerian context?

Format your response with clear headings. Do not guess blindly, think critically about the volume and density of the food."""

    payload = {
        "model": "qwen/qwen3.8-27b",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
        ]}],
        "temperature": 0.3, # Lower temp for more logical/mathematical responses
        "max_tokens": 800
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                 headers=headers, json=payload, timeout=40)
        response.raise_for_status()
        return {"analysis": response.json()["choices"][0]["message"]["content"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 2. DR. VINC CHAT (Text & Voice conversation)
# ============================================================
class ChatRequest(BaseModel):
    message: str
    history: list = []
    language: str = "en"

@router.post("/clinical/chat")
async def clinical_chat(req: ChatRequest):
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="DeepSeek API key missing")

    system_prompt = f"""You are Dr. Vinc, a caring clinical nutritionist in Nigeria. 
The patient is speaking to you. Reply in their language ({req.language}).
Keep your answers concise, empathetic, and highly practical for a Nigerian context.
If they mention a food, estimate its weight in grams before giving macro advice.
NEVER mention you are an AI model or DeepSeek. You ARE Dr. Vinc."""

    messages = [{"role": "system", "content": system_prompt}]
    for h in req.history[-5:]: # Keep last 5 messages for context
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": req.message})

    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 800
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    
    try:
        response = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        return {"reply": response.json()["choices"][0]["message"]["content"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 3. LOG VITALS + SMS ALERTS (Robase)
# ============================================================
class VitalsRequest(BaseModel):
    user_id: str
    blood_sugar_mgdl: float | None = None
    systolic_bp: int | None = None
    diastolic_bp: int | None = None

@router.post("/clinical/log-vitals")
def log_vitals(req: VitalsRequest):
    sb = _supabase()
    sb.table("daily_checkins").insert({
        "user_id": req.user_id,
        "blood_sugar_mgdl": req.blood_sugar_mgdl,
        "systolic_bp": req.systolic_bp,
        "diastolic_bp": req.diastolic_bp
    }).execute()

    is_critical = (
        (req.systolic_bp and req.systolic_bp >= 180) or
        (req.diastolic_bp and req.diastolic_bp >= 120) or
        (req.blood_sugar_mgdl and req.blood_sugar_mgdl >= 300)
    )

    if is_critical:
        links = sb.table("family_links").select("sponsor_id").eq("patient_id", req.user_id).execute()
        for link in links.data or []:
            sponsor_id = link["sponsor_id"]
            profile = sb.table("user_profiles").select("phone, first_name").eq("user_id", sponsor_id).execute()
            if profile.data and profile.data[0].get("phone"):
                phone = profile.data[0]["phone"]
                msg = f"GAIA ALERT: Critical reading. BP: {req.systolic_bp or '?'}/{req.diastolic_bp or '?'}, Sugar: {req.blood_sugar_mgdl or '?'} mg/dL."
                try: robase.send_sms(phone, msg)
                except: pass
    return {"ok": True}
