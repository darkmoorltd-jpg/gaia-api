import os
import base64
import requests

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_BASE = "https://api.groq.com/openai/v1"

TEXT_MODEL = "openai/gpt-oss-120b"
VISION_MODEL = "qwen/qwen3.8-27b"
WHISPER_MODEL = "whisper-large-v3-turbo"

SYSTEM_PROMPT = """You are GAIA, an expert African agricultural advisor built by Darkmoor Ltd. You help smallholder farmers with crop disease, pests, soil, livestock, weather, and general farming questions.

Rules:
- Give practical, specific answers tailored to African farming.
- Use simple language a farmer can understand.
- Include specific product names, dosages, and timing.
- Keep answers 2-4 short paragraphs.
- Never mention you are an AI, never mention Groq, Llama, DeepSeek, or any tech company.
- If unsure, ask a clarifying question."""


def _headers():
    return {
        "Authorization": "Bearer " + GROQ_API_KEY,
        "Content-Type": "application/json",
    }


def chat_text(question, history=None):
    history = history or []
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history[-6:]:
        if isinstance(h, dict) and "role" in h and "content" in h:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": question})

    r = requests.post(
        GROQ_BASE + "/chat/completions",
        headers=_headers(),
        json={
            "model": TEXT_MODEL,
            "messages": messages,
            "temperature": 0.6,
            "max_tokens": 800,
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def chat_vision(image_bytes, question, mime="image/jpeg"):
    b64 = base64.b64encode(image_bytes).decode("ascii")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question or "Analyze this image and advise the farmer."},
                {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}},
            ],
        },
    ]
    r = requests.post(
        GROQ_BASE + "/chat/completions",
        headers=_headers(),
        json={
            "model": VISION_MODEL,
            "messages": messages,
            "temperature": 0.6,
            "max_tokens": 800,
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def transcribe_audio(audio_bytes, filename="audio.m4a"):
    files = {"file": (filename, audio_bytes)}
    data = {"model": WHISPER_MODEL, "language": "en", "response_format": "json"}
    r = requests.post(
        GROQ_BASE + "/audio/transcriptions",
        headers={"Authorization": "Bearer " + GROQ_API_KEY},
        files=files,
        data=data,
        timeout=120,
    )
    r.raise_for_status()
    return r.json().get("text", "")


def extract_pdf_text(pdf_bytes, max_pages=10):
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise RuntimeError("pypdf not installed")
    import io
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = ""
    for i, page in enumerate(reader.pages):
        if i >= max_pages:
            break
        try:
            text += page.extract_text() or ""
        except Exception:
            pass
    return text[:8000]
