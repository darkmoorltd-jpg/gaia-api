# app/routers/tts.py
# YarnGPT — native Nigerian TTS for Yoruba / Igbo / Hausa / English
# Runs via yarngpt.ai API (not blocked from datacenter IPs)
import os
import logging
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

log = logging.getLogger('gaia.tts')
router = APIRouter()

YARN_API = "https://yarngpt.ai/api/v1/tts"
YARN_KEY = os.environ.get("YARNGPT_API_KEY", "")

VOICE_MAP = {
    "en":  "yoruba",
    "pcm": "yoruba",
    "yo":  "yoruba",
    "ig":  "igbo",
    "ha":  "hausa",
}


class TTSBody(BaseModel):
    text: str
    language: str = "en"


async def _synth_yarn(text: str, language: str) -> bytes | None:
    if not YARN_KEY:
        log.warning("YARNGPT_API_KEY not set")
        return None
    voice = VOICE_MAP.get((language or 'en').lower()[:2])
    if not voice:
        return None
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(
                YARN_API,
                headers={
                    "Authorization": "Bearer " + YARN_KEY,
                    "Content-Type": "application/json",
                },
                json={"text": text[:2000], "voice": voice, "language": voice},
            )
        if r.status_code == 200 and len(r.content) > 500:
            log.info("yarn ok: %d bytes voice=%s", len(r.content), voice)
            return r.content
        log.warning("yarn failed: status=%s body=%s", r.status_code, r.text[:180])
    except Exception as e:
        log.warning("yarn exception: %s", str(e)[:180])
    return None


async def _synth_gtts(text: str, language: str) -> bytes | None:
    try:
        from gtts import gTTS
        import asyncio, io
        lang_map = {'fr': 'fr', 'sw': 'sw', 'en': 'en'}
        lang = lang_map.get((language or 'en').lower()[:2], 'en')
        def _do():
            buf = io.BytesIO()
            gTTS(text=text[:2000], lang=lang, slow=False).write_to_fp(buf)
            return buf.getvalue()
        data = await asyncio.to_thread(_do)
        if data and len(data) > 500:
            log.info("gtts ok: %d bytes lang=%s", len(data), lang)
            return data
    except Exception as e:
        log.warning("gtts exception: %s", str(e)[:180])
    return None


async def _synth(text: str, language: str) -> bytes:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    code = (language or 'en').lower()[:2]

    audio = await _synth_yarn(text, code)
    if audio:
        return audio

    audio = await _synth_gtts(text, code)
    if audio:
        return audio

    raise HTTPException(status_code=502, detail="TTS unavailable for language: " + code)


@router.post("/tts")
async def tts_post(body: TTSBody):
    audio = await _synth(body.text, body.language)
    return Response(content=audio, media_type='audio/mpeg',
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/tts")
async def tts_get(text: str, language: str = "en"):
    audio = await _synth(text, language)
    return Response(content=audio, media_type='audio/mpeg',
                    headers={"Cache-Control": "public, max-age=3600"})
