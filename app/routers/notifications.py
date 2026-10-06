# app/routers/notifications.py
# Notification queue drain: SMS via Termii + push via Expo
import os
import httpx
from fastapi import APIRouter, Header, HTTPException
from app.services.auth import verify_supabase_token
from supabase import create_client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TERMII_KEY = os.environ.get("TERMII_API_KEY", "")
EXPO_PUSH = "https://exp.host/--/api/v2/push/send"


def svc():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def auth_admin(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    user = verify_supabase_token(token)
    if (user.get("email") or "").lower() != "darkmoorltd@gmail.com":
        raise HTTPException(403, "admin only")
    return user


async def send_sms(to, body):
    if not TERMII_KEY or not to:
        return False
    phone = to.replace("+", "").replace(" ", "").strip()
    if phone.startswith("0"):
        phone = "234" + phone[1:]
    elif not phone.startswith("234"):
        phone = "234" + phone
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://api.ng.termii.com/api/sms/send",
                json={
                    "api_key": TERMII_KEY,
                    "to": phone,
                    "from": "GAIA",
                    "sms": body[:160],
                    "type": "plain",
                    "channel": "generic",
                },
            )
        return r.status_code == 200
    except Exception:
        return False


async def send_push(tokens, title, body):
    if not tokens:
        return False
    messages = []
    for t in tokens:
        messages.append({
            "to": t,
            "sound": "default",
            "title": title,
            "body": body,
            "priority": "high",
        })
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(EXPO_PUSH, json=messages)
        return r.status_code == 200
    except Exception:
        return False


@router.post("/admin/drain-notifications")
async def drain_notifications(authorization: str = Header(None), limit: int = 100):
    auth_admin(authorization)
    s = svc()

    rows = s.table("notification_queue").select("*").eq("sent", False).order("created_at").limit(limit).execute()
    if not rows.data or len(rows.data) == 0:
        return {"ok": True, "processed": 0, "sent": 0, "failed": 0}

    sent = 0
    failed = 0
    for n in rows.data:
        uid = n["user_id"]
        title = n.get("title") or "GAIA"
        body = n.get("body") or ""

        phone = None
        try:
            p = s.table("user_profiles").select("phone").eq("user_id", uid).limit(1).execute()
            if p.data:
                phone = p.data[0].get("phone")
        except Exception:
            pass

        tokens = []
        try:
            t = s.table("push_tokens").select("token").eq("user_id", uid).execute()
            if t.data:
                tokens = [row["token"] for row in t.data]
        except Exception:
            pass

        delivered = False
        if phone:
            if await send_sms(phone, body):
                delivered = True
        if tokens:
            if await send_push(tokens, title, body):
                delivered = True

        if delivered:
            s.table("notification_queue").update({"sent": True}).eq("id", n["id"]).execute()
            sent += 1
        else:
            failed += 1

    return {"ok": True, "processed": len(rows.data), "sent": sent, "failed": failed}


@router.get("/admin/notification-stats")
async def notification_stats(authorization: str = Header(None)):
    auth_admin(authorization)
    s = svc()
    pending = s.table("notification_queue").select("id").eq("sent", False).execute()
    total = s.table("notification_queue").select("id").execute()
    pending_n = len(pending.data or [])
    total_n = len(total.data or [])
    return {
        "pending": pending_n,
        "total": total_n,
        "sent": max(0, total_n - pending_n),
    }
