import os
import requests
from fastapi import APIRouter, Header, HTTPException, Form
from datetime import datetime, timedelta, timezone
from collections import Counter

from app.services.auth import verify_supabase_token

router = APIRouter()

ADMIN_EMAIL = "darkmoorltd@gmail.com"

# Fallback hardcoded (rotate later once env vars confirmed working)
FALLBACK_URL = "https://pxvtvuwlpzwlkdoxjrep.supabase.co"
FALLBACK_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InB4dnR2dXdscHp3bGtkb3hqcmVwIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NDIxMDQ1NywiZXhwIjoyMDk5Nzg2NDU3fQ.WMzSa17VBFfqWAFt6zNv65AOzEuboWa39cEG35ZElDY"


def _url():
    return os.environ.get("SUPABASE_URL", FALLBACK_URL)


def _svc_headers():
    key = os.environ.get("SUPABASE_SERVICE_KEY", FALLBACK_SERVICE_KEY)
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _auth_admin(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    user = verify_supabase_token(authorization.split(" ", 1)[1])
    email = (user.get("email") or "").lower()
    if email != ADMIN_EMAIL:
        raise HTTPException(403, "Admin access required")
    return user


@router.get("/admin/list-users")
async def list_users(authorization: str = Header(None)):
    _auth_admin(authorization)
    url = _url()
    h = _svc_headers()
    svc_headers = {"apikey": h["apikey"], "Authorization": h["Authorization"]}

    # 1. Auth users
    try:
        r = requests.get(url + "/auth/v1/admin/users?per_page=1000",
                         headers=svc_headers, timeout=60)
    except Exception as e:
        raise HTTPException(500, "Auth list request failed: " + str(e))

    if r.status_code != 200:
        raise HTTPException(500, "Auth list " + str(r.status_code) + ": " + r.text[:200])
    auth_users = r.json().get("users", [])

    # 2. Profiles
    profiles = {}
    try:
        r = requests.get(url + "/rest/v1/user_profiles?select=*", headers=h, timeout=60)
        if r.status_code == 200:
            profiles = {p.get("user_id"): p for p in r.json() if p.get("user_id")}
    except Exception:
        pass

    # 3. Scans
    scans = {}
    try:
        r = requests.get(url + "/rest/v1/user_scans?select=*", headers=h, timeout=60)
        if r.status_code == 200:
            scans = {s.get("user_id"): s for s in r.json() if s.get("user_id")}
    except Exception:
        pass

    # 4. Presence
    presence = {}
    try:
        r = requests.get(url + "/rest/v1/user_presence?select=*", headers=h, timeout=60)
        if r.status_code == 200:
            presence = {p.get("user_id"): p for p in r.json() if p.get("user_id")}
    except Exception:
        pass

    # 5. Bans
    bans = {}
    try:
        r = requests.get(url + "/rest/v1/user_bans?select=*", headers=h, timeout=60)
        if r.status_code == 200:
            bans = {b.get("user_id"): b for b in r.json() if b.get("user_id")}
    except Exception:
        pass

    merged = []
    for u in auth_users:
        uid = u.get("id")
        if not uid:
            continue
        merged.append({
            "user_id": uid,
            "email": u.get("email"),
            "created_at": u.get("created_at"),
            "last_sign_in_at": u.get("last_sign_in_at"),
            "confirmed": bool(u.get("email_confirmed_at")),
            "profile": profiles.get(uid, {}),
            "scans": scans.get(uid, {}),
            "presence": presence.get(uid, {}),
            "ban": bans.get(uid, {}),
        })

    return {"total": len(merged), "users": merged}


@router.get("/admin/stats")
async def admin_stats(authorization: str = Header(None)):
    _auth_admin(authorization)
    url = _url()
    h = _svc_headers()
    svc_headers = {"apikey": h["apikey"], "Authorization": h["Authorization"]}

    total_users = 0
    try:
        r = requests.get(url + "/auth/v1/admin/users?per_page=1000",
                         headers=svc_headers, timeout=60)
        if r.status_code == 200:
            total_users = len(r.json().get("users", []))
    except Exception:
        pass

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    online_now = 0
    try:
        r = requests.get(
            url + "/rest/v1/user_presence?select=user_id&last_seen=gte." + cutoff,
            headers=h, timeout=60,
        )
        if r.status_code == 200:
            online_now = len(r.json())
    except Exception:
        pass

    payments = []
    try:
        r = requests.get(url + "/rest/v1/payment_history?select=amount,paid_at",
                         headers=h, timeout=60)
        if r.status_code == 200:
            payments = r.json()
    except Exception:
        pass

    scans = []
    try:
        r = requests.get(url + "/rest/v1/user_scans?select=scans_remaining,plan",
                         headers=h, timeout=60)
        if r.status_code == 200:
            scans = r.json()
    except Exception:
        pass

    top_crops = []
    try:
        r = requests.get(url + "/rest/v1/scan_history?select=type&limit=5000",
                         headers=h, timeout=60)
        if r.status_code == 200:
            counts = Counter(x.get("type", "unknown") for x in r.json())
            top_crops = [[k, v] for k, v in counts.most_common(5)]
    except Exception:
        pass

    top_states = []
    try:
        r = requests.get(url + "/rest/v1/user_profiles?select=state",
                         headers=h, timeout=60)
        if r.status_code == 200:
            counts = Counter(x.get("state", "Unknown") for x in r.json() if x.get("state"))
            top_states = [[k, v] for k, v in counts.most_common(5)]
    except Exception:
        pass

    return {
        "total_users": total_users,
        "online_now": online_now,
        "dau": online_now,
        "wau": online_now,
        "mau": online_now,
        "scans_total": sum((s.get("scans_remaining") or 0) for s in scans),
        "scans_24h": 0,
        "payments_count": len(payments),
        "revenue_total": sum((p.get("amount") or 0) for p in payments),
        "revenue_30d": 0,
        "top_crops": top_crops,
        "top_states": top_states,
    }


@router.post("/admin/add-scans")
async def admin_add_scans(
    user_id: str = Form(...),
    delta: int = Form(...),
    reason: str = Form("admin adjustment"),
    authorization: str = Header(None),
):
    _auth_admin(authorization)
    url = _url()
    h = _svc_headers()

    r = requests.get(
        url + "/rest/v1/user_scans?user_id=eq." + user_id + "&select=scans_remaining",
        headers=h, timeout=60,
    )
    rows = r.json() if r.status_code == 200 else []
    current = rows[0]["scans_remaining"] if rows else 30
    new_total = max(0, current + delta)

    if rows:
        requests.patch(
            url + "/rest/v1/user_scans?user_id=eq." + user_id,
            headers=h, json={"scans_remaining": new_total}, timeout=60,
        )
    else:
        requests.post(
            url + "/rest/v1/user_scans",
            headers=h,
            json={"user_id": user_id, "scans_remaining": new_total, "plan": "free"},
            timeout=60,
        )

    return {"ok": True, "new_total": new_total}


@router.post("/admin/reset-password")
async def admin_reset_password(
    user_id: str = Form(...),
    new_password: str = Form(...),
    authorization: str = Header(None),
):
    _auth_admin(authorization)
    if len(new_password) < 6:
        raise HTTPException(400, "Password too short")
    url = _url()
    h = _svc_headers()
    r = requests.put(
        url + "/auth/v1/admin/users/" + user_id,
        headers={"apikey": h["apikey"], "Authorization": h["Authorization"],
                 "Content-Type": "application/json"},
        json={"password": new_password},
        timeout=60,
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:200])
    return {"ok": True}


@router.post("/admin/ban-user")
async def admin_ban_user(
    user_id: str = Form(...),
    ban_hours: int = Form(24),
    authorization: str = Header(None),
):
    admin = _auth_admin(authorization)
    until = (datetime.now(timezone.utc) + timedelta(hours=ban_hours)).isoformat()
    url = _url()
    h = _svc_headers()

    requests.post(
        url + "/rest/v1/user_bans",
        headers=h,
        json={
            "user_id": user_id,
            "banned_until": until,
            "reason": "admin ban",
            "banned_by": admin.get("sub"),
        },
        timeout=60,
    )
    return {"ok": True, "banned_until": until}


@router.delete("/admin/delete-user/{user_id}")
async def admin_delete_user(user_id: str, authorization: str = Header(None)):
    _auth_admin(authorization)
    url = _url()
    h = _svc_headers()
    r = requests.delete(
        url + "/auth/v1/admin/users/" + user_id,
        headers={"apikey": h["apikey"], "Authorization": h["Authorization"]},
        timeout=60,
    )
    if r.status_code not in (200, 204):
        raise HTTPException(r.status_code, r.text[:200])
    return {"ok": True}
