import os
import requests
from fastapi import APIRouter, Header, HTTPException, Form
from app.services.auth import verify_supabase_token

router = APIRouter()

ADMIN_EMAIL = "darkmoorltd@gmail.com"


def _auth_admin(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    user = verify_supabase_token(authorization.split(" ", 1)[1])
    email = (user.get("email") or "").lower()
    if email != ADMIN_EMAIL:
        raise HTTPException(403, "Admin access required")
    return user


def _svc_headers():
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


@router.get("/admin/list-users")
async def list_users(authorization: str = Header(None)):
    """Return every auth user + their profile/scans/presence."""
    _auth_admin(authorization)
    url = os.environ["SUPABASE_URL"]
    h = _svc_headers()

    # 1. All auth users (service role can read this)
    r = requests.get(
        url + "/auth/v1/admin/users?per_page=1000",
        headers={"apikey": h["apikey"], "Authorization": h["Authorization"]},
        timeout=30,
    )
    if r.status_code != 200:
        raise HTTPException(500, "Auth list failed: " + r.text[:200])
    auth_users = r.json().get("users", [])

    # 2. Profiles
    r = requests.get(url + "/rest/v1/user_profiles?select=*", headers=h, timeout=30)
    profiles = {p["user_id"]: p for p in (r.json() if r.status_code == 200 else [])}

    # 3. Scans
    r = requests.get(url + "/rest/v1/user_scans?select=*", headers=h, timeout=30)
    scans = {s["user_id"]: s for s in (r.json() if r.status_code == 200 else [])}

    # 4. Presence
    r = requests.get(url + "/rest/v1/user_presence?select=*", headers=h, timeout=30)
    presence = {p["user_id"]: p for p in (r.json() if r.status_code == 200 else [])}

    # 5. Bans
    r = requests.get(url + "/rest/v1/user_bans?select=*", headers=h, timeout=30)
    bans = {b["user_id"]: b for b in (r.json() if r.status_code == 200 else [])}

    # Merge
    merged = []
    for u in auth_users:
        uid = u.get("id")
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
    """Platform KPIs for analytics page."""
    _auth_admin(authorization)
    url = os.environ["SUPABASE_URL"]
    h = _svc_headers()

    # Users count
    r = requests.get(
        url + "/auth/v1/admin/users?per_page=1000",
        headers={"apikey": h["apikey"], "Authorization": h["Authorization"]},
        timeout=30,
    )
    total_users = len(r.json().get("users", [])) if r.status_code == 200 else 0

    # Online now (last_seen < 2 min)
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    r = requests.get(
        url + "/rest/v1/user_presence?select=user_id&last_seen=gte." + cutoff,
        headers=h, timeout=30,
    )
    online_now = len(r.json()) if r.status_code == 200 else 0

    # Payments
    r = requests.get(url + "/rest/v1/payment_history?select=amount,paid_at", headers=h, timeout=30)
    payments = r.json() if r.status_code == 200 else []
    revenue_total = sum((p.get("amount") or 0) for p in payments)

    # Scans
    r = requests.get(url + "/rest/v1/user_scans?select=scans_remaining,plan", headers=h, timeout=30)
    scans = r.json() if r.status_code == 200 else []

    # Top crops (from scan_history if it exists)
    top_crops = []
    try:
        r = requests.get(
            url + "/rest/v1/scan_history?select=type&limit=5000",
            headers=h, timeout=30,
        )
        if r.status_code == 200:
            from collections import Counter
            counts = Counter(x.get("type", "unknown") for x in r.json())
            top_crops = [[k, v] for k, v in counts.most_common(5)]
    except Exception:
        pass

    # Top states (from profiles)
    r = requests.get(url + "/rest/v1/user_profiles?select=state", headers=h, timeout=30)
    top_states = []
    if r.status_code == 200:
        from collections import Counter
        counts = Counter(x.get("state", "Unknown") for x in r.json() if x.get("state"))
        top_states = [[k, v] for k, v in counts.most_common(5)]

    return {
        "total_users": total_users,
        "online_now": online_now,
        "dau": online_now,   # approximate
        "wau": online_now,
        "mau": online_now,
        "scans_total": sum((s.get("scans_remaining") or 0) for s in scans),
        "scans_24h": 0,
        "payments_count": len(payments),
        "revenue_total": revenue_total,
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
    url = os.environ["SUPABASE_URL"]
    h = _svc_headers()

    # Get current
    r = requests.get(
        url + "/rest/v1/user_scans?user_id=eq." + user_id + "&select=scans_remaining",
        headers=h, timeout=20,
    )
    rows = r.json() if r.status_code == 200 else []
    current = rows[0]["scans_remaining"] if rows else 30
    new_total = max(0, current + delta)

    if rows:
        requests.patch(
            url + "/rest/v1/user_scans?user_id=eq." + user_id,
            headers=h,
            json={"scans_remaining": new_total},
            timeout=20,
        )
    else:
        requests.post(
            url + "/rest/v1/user_scans",
            headers=h,
            json={"user_id": user_id, "scans_remaining": new_total, "plan": "free"},
            timeout=20,
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
    url = os.environ["SUPABASE_URL"]
    h = _svc_headers()

    r = requests.put(
        url + "/auth/v1/admin/users/" + user_id,
        headers={"apikey": h["apikey"], "Authorization": h["Authorization"],
                 "Content-Type": "application/json"},
        json={"password": new_password},
        timeout=30,
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
    from datetime import datetime, timedelta, timezone
    until = (datetime.now(timezone.utc) + timedelta(hours=ban_hours)).isoformat()

    url = os.environ["SUPABASE_URL"]
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
        timeout=20,
    )
    return {"ok": True, "banned_until": until}


@router.delete("/admin/delete-user/{user_id}")
async def admin_delete_user(user_id: str, authorization: str = Header(None)):
    _auth_admin(authorization)
    url = os.environ["SUPABASE_URL"]
    h = _svc_headers()

    r = requests.delete(
        url + "/auth/v1/admin/users/" + user_id,
        headers={"apikey": h["apikey"], "Authorization": h["Authorization"]},
        timeout=30,
    )
    if r.status_code not in (200, 204):
        raise HTTPException(r.status_code, r.text[:200])
    return {"ok": True}
