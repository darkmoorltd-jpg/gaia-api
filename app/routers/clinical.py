import os
import base64
import requests
from fastapi import APIRouter, File, UploadFile, Form, HTTPException

router = APIRouter()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

@router.post("/clinical/analyze-meal")
async def analyze_meal(
    file: UploadFile = File(...),
    condition: str = Form(...)
):
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="Groq API key missing")

    # Read and encode image
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
        "model": "qwen/qwen3.8-27b",  # Working Groq Vision model
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

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", 
                                 headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        return {"analysis": data["choices"][0]["message"]["content"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
