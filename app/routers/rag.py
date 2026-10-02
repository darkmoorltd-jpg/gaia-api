import os
import httpx
from fastapi import APIRouter, UploadFile, File, Form, Header, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.services.auth import verify_supabase_token
from app.services.rag_service import (
    extract_text, chunk_text, insert_document, insert_chunks,
    finalize_document, retrieve,
)

router = APIRouter()
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


class RagQuery(BaseModel):
    question: str
    language: Optional[str] = "en"


@router.post("/rag/upload")
async def upload_doc(
    file: UploadFile = File(...),
    file_url: str = Form(""),
    authorization: str = Header(None),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization.split(" ", 1)[1]
    try:
        user = verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))
    uid = user["sub"]

    contents = await file.read()
    text = extract_text(contents, file.filename or "file", file.content_type or "")

    if not text or text.startswith("["):
        raise HTTPException(400, "Could not extract text from this file")

    chunks = chunk_text(text)
    if not chunks:
        raise HTTPException(400, "Document too short to index")

    doc_id = insert_document(
        uid, file.filename or "document",
        file_url, file.content_type or "application/octet-stream",
        len(contents),
    )
    n = insert_chunks(uid, doc_id, chunks)
    finalize_document(doc_id, n)

    return {
        "id": doc_id,
        "name": file.filename,
        "mime_type": file.content_type,
        "size_bytes": len(contents),
        "status": "ready",
        "chunk_count": n,
        "created_at": None,
    }


@router.post("/rag/query")
async def rag_query(req: RagQuery, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization.split(" ", 1)[1]
    try:
        user = verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))
    uid = user["sub"]

    if not DEEPSEEK_KEY:
        raise HTTPException(500, "DEEPSEEK_API_KEY not configured")

    chunks = retrieve(req.question, uid, top_k=5)
    if not chunks:
        return {"answer": "I don't have any documents to reference yet. Upload a file first."}

    context = "\n\n---\n\n".join(
        "Source: " + str(c.get("document_name", "unknown")) + "\n" + c.get("content", "")
        for c in chunks
    )

    system = (
        "You are GAIA, an expert African agricultural advisor. "
        "Answer the user's question using ONLY the provided document excerpts. "
        "If the answer is not in the excerpts, say so plainly and suggest uploading the right document. "
        "Be concise, practical, and specific. Reply in English."
    )

    user_msg = (
        "DOCUMENT EXCERPTS:\n\n" + context +
        "\n\n---\n\nUSER QUESTION: " + req.question
    )

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": "Bearer " + DEEPSEEK_KEY,
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.3,
                "max_tokens": 600,
            },
        )

    if r.status_code != 200:
        raise HTTPException(r.status_code, "Chat failed: " + r.text[:200])

    answer = r.json()["choices"][0]["message"]["content"].strip()
    return {"answer": answer}
