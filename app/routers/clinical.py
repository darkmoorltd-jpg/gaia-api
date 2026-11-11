import os
import base64
import requests
from fastapi import APIRouter, File, UploadFile, Form, HTTPException
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# ============================================================
# 1. MEAL VISION SCANNER
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

    prompt = f"""
    You are Dr. Vinc, a strict clinical nutritionist in Nigeria.
    The patient has: {condition}.
    Analyze this food image.
    
    Provide your response in this exact format:
    1. MEAL DETECTED: (What food is this?)
    2. COMPOSITION: (Estimate the macros - Carbs, Protein, Fat, Calories)
    3. PORTION ADVICE: (Is the portion too large/small? e.g., "Reduce rice to 1 cup")
    4. CLINICAL WARNING: (How does this affect their {condition}? e.g., "High glycemic index")
    5. RECOMMENDED SUBSTITUTE: (What should they eat instead?)
    
    Keep it concise, direct, and in Nigerian context.
    """

    payload = {
        "model": "qwen/qwen3.8-27b",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }
        ],
        "temperature": 0.4,
        "max_tokens": 600
    }

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        return {"analysis": data["choices"][0]["message"]["content"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# 2. LOG VITALS & GET AI FEEDBACK
# ============================================================
class VitalsLog(BaseModel):
    user_id: str
    blood_sugar_mgdl: Optional[float] = None
    systolic_bp: Optional[float] = None
    diastolic_bp: Optional[float] = None

@router.post("/clinical/log-vitals")
async def log_vitals(req: VitalsLog):
    prompt = f"You are Dr. Vinc. The patient has a blood pressure of {req.systolic_bp}/{req.diastolic_bp} and blood sugar of {req.blood_sugar_mgdl}. Give a 2-sentence clinical warning if these are high, or encouragement if normal."
    
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "max_tokens": 150}
    
    try:
        d_res = requests.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=20)
        ai_feedback = d_res.json()["choices"][0]["message"]["content"] if d_res.status_code == 200 else "Vitals logged."
    except:
        ai_feedback = "Vitals logged."

    return {"ok": True, "ai_feedback": ai_feedback}

# ============================================================
# 3. CHAT WITH DR. VINC
# ============================================================
class ChatMsg(BaseModel):
    role: str
    content: str

class ChatReq(BaseModel):
    message: str
    history: List[ChatMsg]
    language: str

@router.post("/clinical/chat")
async def chat(req: ChatReq):
    system_prompt = "You are Dr. Vinc, a strict but caring Nigerian clinical nutritionist. Keep answers under 100 words. Focus on diet, macros, and Nigerian foods. Never say you are an AI language model."
    
    messages = [{"role": "system", "content": system_prompt}]
    for m in req.history:
        messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": req.message})

    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": messages, "temperature": 0.7, "max_tokens": 300}
    
    try:
        res = requests.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=20)
        if res.status_code == 200:
            return {"reply": res.json()["choices"][0]["message"]["content"]}
    except:
        pass
    raise HTTPException(status_code=500, detail="AI unavailable")
