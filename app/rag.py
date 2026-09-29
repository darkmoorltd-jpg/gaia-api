import os
import io
import requests
import httpx
from fastapi import APIRouter, File, UploadFile, Form, HTTPException, Header
from app.services.auth import verify_supabase_token

router = APIRouter()

EMBED_URL = "https://api-inference.huggingface.co/pipeline/feature-extraction/sentence-transformers/all-MiniLM-L6-v2"


def _headers():
    return {
        "apikey": os.environ["SUPABASE_SERVICE_KEY"],
        "Authorization": "Bearer " + os.environ["SUPABASE_SERVICE_KEY"],
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _embed(text, hf_token):
    r = requests.post(
        EMBED_URL,
        headers={"Authorization": "Bearer " + hf_token},
        json={"inputs": text, "options": {"wait_for_model": True}},
        timeout=60,
    )
    r.raise_for_status()
    vec = r.json()
    if isinstance(vec[0], list):
        vec = vec[0]
    return vec


def _auth(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    return verify_supabase_token(authorization.split(" ", 1)[1])


@router.post("/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...), authorization: str = Header(None)):
    _auth(authorization)
    contents = await audio.read()
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        raise HTTPException(500, "GROQ_API_KEY not set")

    files = {"file": ("audio.wav", contents, audio.content_type or "audio/wav")}
    data = {"model": "whisper-large-v3-turbo"}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": "Bearer " + groq_key},
            files=files,
            data=data,
        )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return {"text": r.json().get("text", "")}


@router.post("/rag/upload")
async def rag_upload(
    file: UploadFile = File(...),
    title: str = Form(...),
    authorization: str = Header(None),
):
    user = _auth(authorization)
    contents = await file.read()

    if file.content_type == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise HTTPException(500, "pypdf not installed")
        reader = PdfReader(io.BytesIO(contents))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
    else:
        text = contents.decode("utf-8", errors="ignore")

    if not text.strip():
        raise HTTPException(400, "Empty document")

    size, overlap = 800, 100
    chunks = []
    for i in range(0, len(text), size - overlap):
        c = text[i:i + size]
        if c.strip():
            chunks.append(c)

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        raise HTTPException(500, "HF_TOKEN not set")

    supabase_url = os.environ["SUPABASE_URL"]
    h = _headers()

    doc_resp = requests.post(
        supabase_url + "/rest/v1/documents",
        headers=h,
        json={"user_id": user["sub"], "title": title, "content": text[:5000]},
        timeout=20,
    )
    if doc_resp.status_code not in (200, 201):
        raise HTTPException(500, "Insert doc failed: " + doc_resp.text)
    doc_id = doc_resp.json()[0]["id"]

    inserted = 0
    for c in chunks:
        try:
            vec = _embed(c, hf_token)
        except Exception as e:
            print("embed fail:", e)
            continue
        r = requests.post(
            supabase_url + "/rest/v1/document_chunks",
            headers=h,
            json={"document_id": doc_id, "content": c, "embedding": vec},
            timeout=30,
        )
        if r.status_code in (200, 201):
            inserted += 1

    return {"document_id": doc_id, "chunks": inserted}


@router.post("/rag/query")
async def rag_query(
    question: str = Form(...),
    authorization: str = Header(None),
):
    user = _auth(authorization)
    hf_token = os.environ.get("HF_TOKEN", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not hf_token or not groq_key:
        raise HTTPException(500, "HF_TOKEN or GROQ_API_KEY not set")

    q_vec = _embed(question, hf_token)

    supabase_url = os.environ["SUPABASE_URL"]
    r = requests.post(
        supabase_url + "/rest/v1/rpc/match_chunks",
        headers=_headers(),
        json={"query_embedding": q_vec, "match_count": 5, "user_uuid": user["sub"]},
        timeout=30,
    )
    chunks = r.json() if r.status_code == 200 else []
    context = "\n\n".join(c.get("content", "") for c in chunks)

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": "Bearer " + groq_key, "Content-Type": "application/json"},
        json={
            "model": "openai/gpt-oss-120b",
            "messages": [
                {"role": "system", "content": "You are GAIA, an African agricultural AI. Answer ONLY using the context. If unsure, say so.\n\nCONTEXT:\n" + context},
                {"role": "user", "content": question},
            ],
            "max_tokens": 800,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise HTTPException(500, "Groq error: " + resp.text)

    answer = resp.json()["choices"][0]["message"]["content"]
    return {
        "answer": answer,
        "sources": [c.get("content", "")[:200] for c in chunks],
        "num_sources": len(chunks),
    }
