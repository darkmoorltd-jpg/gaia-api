import os
import base64
import requests
from typing import List

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-1.5-flash-latest"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL + ":generateContent?key=" + GEMINI_API_KEY
)


class VisionUnavailable(Exception):
    pass


def vision_chat(
    prompt: str,
    images: List[dict],       # [{ "data": base64, "mime": "image/jpeg" }, ...]
    system_prompt: str = "",
) -> str:
    """Send a prompt + images to Gemini. Returns the assistant's text."""
    if not GEMINI_API_KEY:
        raise VisionUnavailable("GEMINI_API_KEY not configured")

    # Build parts: text first, then images
    parts = []
    if system_prompt:
        # Gemini supports a system_instruction field, but for simplicity
        # we prepend the system prompt as text.
        parts.append({"text": system_prompt})
    if prompt:
        parts.append({"text": prompt})

    for img in images:
        parts.append({
            "inline_data": {
                "mime_type": img.get("mime", "image/jpeg"),
                "data": img["data"],     # already base64
            }
        })

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1500,
        },
    }

    r = requests.post(GEMINI_URL, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(
            "Gemini " + str(r.status_code) + ": " + r.text[:200]
        )

    data = r.json()
    try:
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("No candidates returned: " + str(data)[:200])
        content = candidates[0].get("content", {})
        parts_out = content.get("parts", [])
        # Gemini can return multiple parts; concatenate text parts
        reply = "".join(p.get("text", "") for p in parts_out).strip()
        return reply or "No reply from GAIA."
    except Exception as e:
        raise RuntimeError("Bad Gemini response: " + str(e)[:200])
