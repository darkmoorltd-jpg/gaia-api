from fastapi import APIRouter, UploadFile, File, Header, HTTPException
from pydantic import BaseModel
from typing import List
import os

from app.services.auth import verify_supabase_token
from app.services import rag

router = APIRouter()


def _get_user_id(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        u = verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))
    return u["sub"]


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    authorization: str = Header(None),
):
    user_id = _get_user_id(authorization)
    contents = await file.read()
    if len(contents) > 20 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 20 MB)")

    try:
        doc = rag.ingest_document(user_id, file.filename, contents)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, "Ingest failed: " + str(e)[:200])

    return doc


@router.get("/documents")
async def list_documents(authorization: str = Header(None)):
    user_id = _get_user_id(authorization)
    try:
        return {"documents": rag.list_documents(user_id)}
    except Exception as e:
        raise HTTPException(500, "List failed: " + str(e)[:200])


class DeleteDoc(BaseModel):
    document_id: int


@router.post("/documents/delete")
async def delete_document(body: DeleteDoc, authorization: str = Header(None)):
    user_id = _get_user_id(authorization)
    try:
        rag.delete_document(user_id, body.document_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, "Delete failed: " + str(e)[:200])
