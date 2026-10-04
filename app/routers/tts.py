# app/routers/tts.py
# Edge Neural TTS — native-sounding Yoruba / Igbo / Hausa / French / Swahili / English
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


async def _synth(text: str, language: str) -> bytes:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    voice = VOICES.get((language or "en").lower()[:2], VOICES["en"])
    log.info("tts request: lang=%s voice=%s chars=%d", language, voice, len(text))
    try:
        communicate = edge_tts.Communicate(text[:2000], voice)
        audio = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
    except Exception as e:
        log.error("edge-tts failed: %s", e)
        log.error(traceback.format_exc())
        raise HTTPException(status_code=502, detail="TTS failed: " + str(e)[:200])
    if not audio:
        log.error("edge-tts returned no audio for voice=%s", voice)
        raise HTTPException(status_code=502, detail="tts produced no audio")
    return audio


@router.post("/tts")
async def tts_post(body: TTSBody):
    audio = await _synth(body.text, body.language)
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/tts")
async def tts_get(text: str, language: str = "en"):
    audio = await _synth(text, language)
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )
