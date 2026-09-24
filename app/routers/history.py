from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import os
import requests

from app.services.auth import verify_supabase_token

router = APIRouter(prefix="/history", tags=["history"])

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

H = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def _auth(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    return verify_supabase_token(token)


class SaveReq(BaseModel):
    session_id: str
    role: str
    content: str


class EditReq(BaseModel):
    new_content: str


@router.get("/sessions")
async def list_sessions(authorization: str = Header(None)):
    user = _auth(authorization)
    uid = user["sub"]
    r = requests.get(
        SUPABASE_URL + "/rest/v1/chat_history?user_id=eq." + uid +
        "&select=session_id,content,role,created_at&order=created_at.desc&limit=500",
        headers=H, timeout=15,
    )
    r.raise_for_status()
    rows = r.json()

    sessions = {}
    for row in rows:
        sid = row["session_id"]
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "preview": "",
                "count": 0,
                "last_at": row["created_at"],
            }
        sessions[sid]["count"] += 1
        if not sessions[sid]["preview"] and row["role"] == "user":
            sessions[sid]["preview"] = row["content"][:80]

    return {"sessions": list(sessions.values())}


@router.get("/{session_id}")
async def get_session(session_id: str, authorization: str = Header(None)):
    user = _auth(authorization)
    uid = user["sub"]
    r = requests.get(
        SUPABASE_URL + "/rest/v1/chat_history?user_id=eq." + uid +
        "&session_id=eq." + session_id + "&order=created_at.asc",
        headers=H, timeout=15,
    )
    r.raise_for_status()
    return {"messages": r.json()}


@router.post("/save")
async def save_message(req: SaveReq, authorization: str = Header(None)):
    user = _auth(authorization)
    uid = user["sub"]
    r = requests.post(
        SUPABASE_URL + "/rest/v1/chat_history",
        headers=H,
        json={
            "user_id": uid,
            "session_id": req.session_id,
            "role": req.role,
            "content": req.content,
        },
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    return {"message": rows[0] if rows else None}


@router.post("/edit/{message_id}")
async def edit_message(message_id: int, req: EditReq, authorization: str = Header(None)):
    user = _auth(authorization)
    uid = user["sub"]
    cur = requests.get(
        SUPABASE_URL + "/rest/v1/chat_history?id=eq." + str(message_id) + "&select=edit_count",
        headers=H, timeout=15,
    ).json()
    if not cur:
        raise HTTPException(404, "Message not found")
    count = cur[0].get("edit_count", 0)
    if count >= 5:
        raise HTTPException(400, "Edit limit reached (5)")
    r = requests.patch(
        SUPABASE_URL + "/rest/v1/chat_history?id=eq." + str(message_id),
        headers=H,
        json={"content": req.new_content, "edit_count": count + 1},
        timeout=15,
    )
    r.raise_for_status()
    return {"ok": True, "edit_count": count + 1}


@router.delete("/message/{message_id}")
async def delete_message(message_id: int, authorization: str = Header(None)):
    _auth(authorization)
    requests.delete(
        SUPABASE_URL + "/rest/v1/chat_history?id=eq." + str(message_id),
        headers=H, timeout=15,
    )
    return {"ok": True}


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, authorization: str = Header(None)):
    user = _auth(authorization)
    uid = user["sub"]
    requests.delete(
        SUPABASE_URL + "/rest/v1/chat_history?user_id=eq." + uid +
        "&session_id=eq." + session_id,
        headers=H, timeout=15,
    )
    return {"ok": True}
