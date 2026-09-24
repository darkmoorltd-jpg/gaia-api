import os
import requests
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List

from app.services.auth import verify_supabase_token

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SVC = os.environ.get("SUPABASE_SERVICE_KEY", "")

MAX_EDITS = 5


def _headers():
    return {
        "apikey": SUPABASE_SVC,
        "Authorization": "Bearer " + SUPABASE_SVC,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _auth(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        return verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))


class SaveMessage(BaseModel):
    session_id: str
    role: str
    content: str
    parent_id: Optional[int] = None


class EditMessage(BaseModel):
    new_content: str


class SessionID(BaseModel):
    session_id: str


@router.post("/history/save")
async def save_message(body: SaveMessage, authorization: str = Header(None)):
    user = _auth(authorization)
    user_id = user["sub"]

    payload = {
        "user_id": user_id,
        "session_id": body.session_id,
        "role": body.role,
        "content": body.content,
        "parent_id": body.parent_id,
        "edit_count": 0,
    }

    r = requests.post(
        SUPABASE_URL + "/rest/v1/chat_history",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])

    rows = r.json() if r.content else []
    return {"ok": True, "message": rows[0] if rows else None}


@router.get("/history/{session_id}")
async def get_session(session_id: str, authorization: str = Header(None)):
    user = _auth(authorization)
    user_id = user["sub"]

    r = requests.get(
        SUPABASE_URL + "/rest/v1/chat_history"
        + "?user_id=eq." + user_id
        + "&session_id=eq." + session_id
        + "&order=created_at.asc",
        headers=_headers(),
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return {"messages": r.json()}


@router.get("/history")
async def list_sessions(authorization: str = Header(None)):
    """List all distinct sessions for the user with preview + count."""
    user = _auth(authorization)
    user_id = user["sub"]

    r = requests.get(
        SUPABASE_URL + "/rest/v1/chat_history"
        + "?user_id=eq." + user_id
        + "&order=created_at.desc&limit=2000",
        headers=_headers(),
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    rows = r.json()

    # Group by session_id
    sessions = {}
    for row in rows:
        sid = row["session_id"]
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "created_at": row["created_at"],
                "updated_at": row["created_at"],
                "preview": "",
                "message_count": 0,
            }
        sessions[sid]["message_count"] += 1
        if row["created_at"] > sessions[sid]["updated_at"]:
            sessions[sid]["updated_at"] = row["created_at"]
        if row["role"] == "user" and not sessions[sid]["preview"]:
            sessions[sid]["preview"] = row["content"][:80]

    return {"sessions": list(sessions.values())}


@router.delete("/history/message/{message_id}")
async def delete_message(message_id: int, authorization: str = Header(None)):
    user = _auth(authorization)
    user_id = user["sub"]

    r = requests.delete(
        SUPABASE_URL + "/rest/v1/chat_history"
        + "?id=eq." + str(message_id)
        + "&user_id=eq." + user_id,
        headers=_headers(),
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return {"ok": True}


@router.delete("/history/session/{session_id}")
async def delete_session(session_id: str, authorization: str = Header(None)):
    user = _auth(authorization)
    user_id = user["sub"]

    r = requests.delete(
        SUPABASE_URL + "/rest/v1/chat_history"
        + "?session_id=eq." + session_id
        + "&user_id=eq." + user_id,
        headers=_headers(),
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return {"ok": True}


@router.post("/history/edit/{message_id}")
async def edit_message(message_id: int, body: EditMessage, authorization: str = Header(None)):
    user = _auth(authorization)
    user_id = user["sub"]

    # Fetch current
    r = requests.get(
        SUPABASE_URL + "/rest/v1/chat_history"
        + "?id=eq." + str(message_id)
        + "&user_id=eq." + user_id
        + "&select=edit_count,role",
        headers=_headers(),
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    rows = r.json()
    if not rows:
        raise HTTPException(404, "Message not found")

    current = rows[0].get("edit_count", 0) or 0
    if rows[0]["role"] != "user":
        raise HTTPException(403, "Only user messages can be edited")
    if current >= MAX_EDITS:
        raise HTTPException(429, "Edit limit reached (5 edits max)")

    patch = {
        "content": body.new_content,
        "edit_count": current + 1,
        "edited_at": "now()",
    }
    r = requests.patch(
        SUPABASE_URL + "/rest/v1/chat_history"
        + "?id=eq." + str(message_id)
        + "&user_id=eq." + user_id,
        headers=_headers(),
        json=patch,
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])

    return {
        "ok": True,
        "edit_count": current + 1,
        "edits_remaining": MAX_EDITS - (current + 1),
    }


# ══════════════════════════════════════════════════════════
# PERSISTENT MEMORY
# ══════════════════════════════════════════════════════════

class MemoryUpsert(BaseModel):
    key: str
    value: str


@router.get("/memory")
async def get_memory(authorization: str = Header(None)):
    user = _auth(authorization)
    user_id = user["sub"]
    r = requests.get(
        SUPABASE_URL + "/rest/v1/farmer_memory"
        + "?user_id=eq." + user_id
        + "&select=key,value",
        headers=_headers(),
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return {"memory": {row["key"]: row["value"] for row in r.json()}}


@router.post("/memory")
async def upsert_memory(body: MemoryUpsert, authorization: str = Header(None)):
    user = _auth(authorization)
    user_id = user["sub"]
    r = requests.post(
        SUPABASE_URL + "/rest/v1/farmer_memory",
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        json={
            "user_id": user_id,
            "key": body.key,
            "value": body.value,
            "updated_at": "now()",
        },
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return {"ok": True}


@router.delete("/memory/{key}")
async def delete_memory(key: str, authorization: str = Header(None)):
    user = _auth(authorization)
    user_id = user["sub"]
    r = requests.delete(
        SUPABASE_URL + "/rest/v1/farmer_memory"
        + "?user_id=eq." + user_id
        + "&key=eq." + key,
        headers=_headers(),
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return {"ok": True}
