# app/routers/tts.py
# Edge Neural TTS — native voices for yo/ig/ha/fr/sw/en
# Requires: edge-tts>=7.2.7
import asyncio
import logging
import traceback
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
import edge_tts

log = logging.getLogger('gaia.tts')
router = APIRouter()

VOICES = {
    "en":  "en-NG-SoniaNeural",
    "ha":  "ha-NG-MuhammedNeural",
    "yo":  "yo-NG-AbimbolaNeural",
    "ig":  "ig-NG-ChidinmaNeural",
    "pcm": "en-NG-AbeoNeural",
    "fr":  "fr-FR-DeniseNeural",
    "sw":  "sw-KE-ZuriNeural",
}


class TTSBody(BaseModel):
    text: str
    language: str = "en"


async def _synth_once(text: str, voice: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice)
    audio = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return audio


async def _synth(text: str, language: str) -> bytes:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    voice = VOICES.get((language or "en").lower()[:2], VOICES["en"])
    log.info("tts request: lang=%s voice=%s chars=%d", language, voice, len(text))

    delays = [0, 1.5, 3.0, 6.0]
    last_err = None
    for i, delay in enumerate(delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            audio = await _synth_once(text[:2000], voice)
            if audio and len(audio) > 500:
                log.info("tts ok: %d bytes (attempt %d)", len(audio), i + 1)
                return audio
            last_err = "empty audio"
        except Exception as e:
            last_err = str(e)[:180]
            log.warning("tts attempt %d failed: %s", i + 1, last_err)

    log.error("tts exhausted retries: %s", last_err)
    raise HTTPException(status_code=502, detail=f"TTS failed after retries: {last_err}")


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
