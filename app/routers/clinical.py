import os
import base64
import requests
from fastapi import APIRouter, File, UploadFile, Form, HTTPException
from pydantic import BaseModel
from supabase import create_client
from app.utils import robase

router = APIRouter()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ============================================================
# SUPABASE CLIENT (SERVICE ROLE)
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY env vars must be set")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ============================================================
# 1. MEAL VISION (Qwen 3.8 via Groq)
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

    prompt = f"""You are Dr. Vinc, a strict clinical nutritionist in Nigeria.
The patient has: {condition}.
Analyze this food image.

Provide your response in this exact format:
1. MEAL DETECTED: (What food is this?)
2. COMPOSITION: (Estimate the macros - Carbs, Protein, Fat, Calories)
3. PORTION ADVICE: (Is the portion too large/small?)
4. CLINICAL WARNING: (How does this affect their {condition}?)
5. RECOMMENDED SUBSTITUTE: (What should they eat instead?)

Keep it concise, direct, and in Nigerian context."""

    payload = {
        "model": "qwen/qwen3.8-27b",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
        ]}],
        "temperature": 0.4,
        "max_tokens": 600
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                 headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        return {"analysis": data["choices"][0]["message"]["content"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 2. LOG VITALS + SMS ALERTS (Robase)
# ============================================================
class VitalsRequest(BaseModel):
    user_id: str
    blood_sugar_mgdl: int | None = None
    systolic_bp: int | None = None
    diastolic_bp: int | None = None

@router.post("/clinical/log-vitals")
def log_vitals(req: VitalsRequest):
    import traceback
    try:
        sb = _supabase()

        # Cast to int to prevent DB error
        sugar = int(req.blood_sugar_mgdl) if req.blood_sugar_mgdl is not None else None
        sys = int(req.systolic_bp) if req.systolic_bp is not None else None
        dia = int(req.diastolic_bp) if req.diastolic_bp is not None else None

        # 1. Save to DB
        sb.table("daily_checkins").insert({
            "user_id": req.user_id,
            "blood_sugar_mgdl": sugar,
            "systolic_bp": sys,
            "diastolic_bp": dia
        }).execute()

        # 2. Detect critical reading
        is_critical = (
            (sys and sys >= 180) or
            (dia and dia >= 120) or
            (sugar and sugar >= 300)
        )

        sms_result = {"sent": False, "sponsors_notified": 0}

        if is_critical:
            # 3. Find linked family members (Sponsors)
            links = sb.table("family_links").select("sponsor_id").eq("patient_id", req.user_id).execute()

            for link in (links.data or []):
                sponsor_id = link["sponsor_id"]
                profile = sb.table("user_profiles").select("phone, first_name").eq("user_id", sponsor_id).execute()

                if profile.data and profile.data[0].get("phone"):
                    phone = profile.data[0]["phone"]
                    name = profile.data[0].get("first_name") or "Family Member"
                    msg = (
                        f"GAIA ALERT: {name}, your loved one has a critical reading. "
                        f"BP: {sys or '?'}/{dia or '?'}, Sugar: {sugar or '?'} mg/dL. "
                        f"Open GAIA or call them immediately."
                    )
                    try:
                        robase.send_sms(phone, msg)
                        sms_result["sponsors_notified"] += 1
                    except Exception as e:
                        print(f"[ROBASE] SMS failed: {e}")

            sms_result["sent"] = sms_result["sponsors_notified"] > 0

        return {
            "ok": True,
            "sms_sent": sms_result["sent"],
            "sponsors_notified": sms_result["sponsors_notified"]
        }

    except Exception as e:
        return {
            "ok": False,
            "error_type": type(e).__name__,
            "error": str(e),
            "trace": traceback.format_exc()
        }
