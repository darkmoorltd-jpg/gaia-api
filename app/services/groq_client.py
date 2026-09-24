import os
import base64
import requests

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_BASE = "https://api.groq.com/openai/v1"

TEXT_MODEL = "openai/gpt-oss-120b"
VISION_MODEL = "qwen/qwen3.8-27b"
WHISPER_MODEL = "whisper-large-v3-turbo"

SYSTEM_PROMPT = """You are GAIA, an expert African agricultural advisor built by Darkmoor Ltd.

ALWAYS respond in rich markdown. Use this structure:

**Bold 1-line summary.**

## Section Title
- Bullet with **bold key terms**
- Bullet with numbers, dosages, timings
- Bullet with Naira costs where relevant

### Subsection
1. Numbered step
2. Numbered step

| Item | Amount | Timing |
|------|--------|--------|
| Data | Data | Data |

> Warning block for critical safety info.

## Next Steps
- [ ] Action item 1
- [ ] Action item 2

RULES:
- Give deep, specific answers (1500-3000 words for serious farming questions).
- Include product names, dosages, timing, costs in Naira, and expected yield impact.
- Cover: what it is, why it happens, treatment (organic + chemical), prevention, cost, timeline.
- Use tables for any comparison, dosage, timeline, or cost breakdown.
- Use bullet lists for symptoms, causes, products.
- Use numbered lists for step-by-step procedures.
- End every answer with a "Next Steps" checklist.
- Never mention AI, Groq, Llama, DeepSeek, or any tech company.
- If the question is trivial, be concise. If it's serious, be thorough.
- If unsure, ask a clarifying question first."""


def _headers():
    return {
        "Authorization": "Bearer " + GROQ_API_KEY,
        "Content-Type": "application/json",
    }


def chat_text(question, history=None):
    history = history or []
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history[-20:]:
        if isinstance(h, dict) and "role" in h and "content" in h:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": question})

    r = requests.post(
        GROQ_BASE + "/chat/completions",
        headers=_headers(),
        json={
            "model": TEXT_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 6000,
        },
        timeout=180,
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
                {"type": "text", "text": question or "Analyze this farm image in detail and give a full diagnosis, treatment plan, costs, and prevention guide."},
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
            "temperature": 0.7,
            "max_tokens": 6000,
        },
        timeout=180,
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
        timeout=180,
    )
    r.raise_for_status()
    return r.json().get("text", "")


def extract_pdf_text(pdf_bytes, max_pages=10):
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader
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
