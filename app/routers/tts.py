# app/routers/tts.py
# YarnGPT — native Nigerian voices
# Flow: POST -> poll /status/{job_id} -> download audio_url
import os
import uuid
import asyncio
import logging
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

log = logging.getLogger('gaia.tts')
router = APIRouter()

YARN_BASE = "https://api.yarngpt.ai/api/v1"
YARN_KEY = os.environ.get("YARNGPT_API_KEY", "")

# Native voice per language
VOICE_MAP = {
    "yo":  "idera",       # Yoruba
    "ig":  "chinenye",    # Igbo
    "ha":  "umar",        # Hausa
    "en":  "mary",        # Nigerian English
    "pcm": "mary",
    "fr":  "mary",        # French via polyglot voice
    "sw":  "mary",        # Swahili via polyglot voice
}


class TTSBody(BaseModel):
    text: str
    language: str = "en"


async def _synth_yarn(text: str, code: str) -> bytes | None:
    if not YARN_KEY:
        log.warning("YARNGPT_API_KEY not set")
        return None
    voice = VOICE_MAP.get(code)
    if not voice:
        voice = VOICE_MAP["en"]

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            # 1. Queue the job
            r = await client.post(
                YARN_BASE + "/tts",
                headers={
                    "Authorization": "Bearer " + YARN_KEY,
                    "Content-Type": "application/json",
                    "Idempotency-Key": str(uuid.uuid4()),
                },
                json={"text": text[:2000], "voice": voice},
            )

            # Sync response (some accounts skip queueing)
            if r.status_code == 200 and len(r.content) > 1000:
                log.info("yarn sync ok: %d bytes voice=%s", len(r.content), voice)
                return r.content

            if r.status_code not in (200, 202):
                log.warning("yarn queue failed: %s %s", r.status_code, r.text[:180])
                return None

            job_id = r.json().get("job_id")
            if not job_id:
                log.warning("yarn no job_id in %s", r.text[:180])
                return None

            log.info("yarn queued: job=%s voice=%s chars=%d", job_id, voice, len(text))

            # 2. Poll for completion
            for _ in range(40):
                await asyncio.sleep(1.5)
                s = await client.get(
                    YARN_BASE + "/status/" + job_id,
                    headers={"Authorization": "Bearer " + YARN_KEY},
                )
                if s.status_code != 200:
                    continue
                data = s.json()
                status = data.get("status")
                if status == "completed" and data.get("audio_url"):
                    # 3. Download the MP3
                    a = await client.get(data["audio_url"])
                    if a.status_code == 200 and len(a.content) > 1000:
                        log.info("yarn completed: %d bytes voice=%s", len(a.content), voice)
                        return a.content
                    log.warning("yarn download failed: %s", a.status_code)
                    return None
                if status == "failed":
                    log.warning("yarn job failed: %s", data.get("error_message"))
                    return None

            log.warning("yarn poll timeout for job %s", job_id)
            return None
    except Exception as e:
        log.warning("yarn exception: %s", str(e)[:200])
        return None


async def _synth_gtts(text: str, code: str) -> bytes | None:
    # Fallback only — used if YarnGPT fails
    try:
        from gtts import gTTS
        import io
        lang_map = {"fr": "fr", "sw": "sw", "en": "en"}
        lang = lang_map.get(code, "en")
        def _do():
            buf = io.BytesIO()
            gTTS(text=text[:2000], lang=lang, slow=False).write_to_fp(buf)
            return buf.getvalue()
        data = await asyncio.to_thread(_do)
        if data and len(data) > 1000:
            log.info("gtts ok: %d bytes lang=%s", len(data), lang)
            return data
    except Exception as e:
        log.warning("gtts exception: %s", str(e)[:200])
    return None


async def _synth(text: str, language: str) -> bytes:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    code = (language or "en").lower()[:2]

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
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Cache-Control": ".ai endpointpublic, max-age=3600"})


@router.get("/tts")
async def tts_get(text: str, language: str = "en"):
    audio = await _synth(text, language)
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=3600"})
