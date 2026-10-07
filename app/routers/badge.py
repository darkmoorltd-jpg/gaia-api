# app/routers/badge.py
# Badge subscriptions with server-side Paystack verification
import os
import uuid
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from app.services.auth import verify_supabase_token
from supabase import create_client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET_KEY", "")

TIER_PRICES = {
    "bronze":   500_00,
    "silver":   1500_00,
    "gold":     3000_00,
    "platinum": 5000_00,
}


def svc():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def auth_user(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        return verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))


class SubscribeReq(BaseModel):
    tier: str
    user_email: str


@router.post("/badge/subscribe")
async def badge_subscribe(req: SubscribeReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    tier = (req.tier or "").lower().strip()
    if tier not in TIER_PRICES:
        raise HTTPException(400, "Invalid tier: " + tier)

    amount_kobo = TIER_PRICES[tier]
    ref = "GAIA_BADGE_" + tier + "_" + uid[:8] + "_" + uuid.uuid4().hex[:8]

    s = svc()
    try:
        s.table("badge_events").insert({
            "user_id": uid,
            "tier": tier,
            "event": "subscribe_initiated",
            "amount": amount_kobo / 100,
            "reference": ref,
        }).execute()
    except Exception:
        pass

    return {
        "ok": True,
        "reference": ref,
        "tier": tier,
        "amount_kobo": amount_kobo,
        "amount_naira": amount_kobo / 100,
        "email": req.user_email or user.get("email", ""),
    }


class VerifyReq(BaseModel):
    reference: str


@router.post("/badge/verify")
async def badge_verify(req: VerifyReq, authorization: str = Header(None)):
    if not PAYSTACK_SECRET:
        raise HTTPException(500, "PAYSTACK_SECRET_KEY not configured")
    user = auth_user(authorization)
    uid = user["sub"]

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://api.paystack.co/transaction/verify/" + req.reference,
            headers={"Authorization": "Bearer " + PAYSTACK_SECRET},
        )
    if r.status_code != 200:
        raise HTTPException(400, "Paystack verify failed")

    data = r.json()
    if not data.get("status") or data["data"]["status"] != "success":
        raise HTTPException(400, "Payment not successful")

    txn = data["data"]
    amount_naira = txn["amount"] / 100

    parts = req.reference.split("_")
    if len(parts) < 3 or parts[0] != "GAIA" or parts[1] != "BADGE":
        raise HTTPException(400, "Invalid badge reference")

    tier = parts[2]
    if tier not in TIER_PRICES:
        raise HTTPException(400, "Unknown tier in reference")

    expected = TIER_PRICES[tier] / 100
    if abs(amount_naira - expected) > 1:
        raise HTTPException(400, f"Amount mismatch: paid N{amount_naira}, expected N{expected}")

    ref_user = parts[3] if len(parts) > 3 else ""
    if ref_user and not uid.startswith(ref_user):
        raise HTTPException(403, "Reference does not belong to this user")

    s = svc()
    existing = s.table("badge_subscriptions").select("id").eq("paystack_ref", req.reference).execute()
    if existing.data:
        return {"ok": True, "idempotent": True, "badge": s.rpc("badge_current", {}).execute().data}

    s.rpc("badge_activate", {
        "p_user_id": uid,
        "p_tier": tier,
        "p_reference": req.reference,
        "p_amount": amount_naira,
        "p_days": 30,
    }).execute()

    try:
        s.table("notification_queue").insert({
            "user_id": uid,
            "title": "Badge activated",
            "body": tier.upper() + " badge is now active. Enjoy your benefits.",
        }).execute()
    except Exception:
        pass

    badge = s.rpc("badge_current", {}).execute().data

    return {"ok": True, "tier": tier, "amount_naira": amount_naira, "badge": badge}


@router.get("/badge/current")
async def badge_current_endpoint(authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    s = svc()
    result = s.table("badge_subscriptions") \
        .select("*") \
        .eq("user_id", uid) \
        .eq("status", "active") \
        .gt("expires_at", datetime.now(timezone.utc).isoformat()) \
        .order("expires_at", desc=True) \
        .limit(1).execute()

    row = result.data[0] if result.data else None
    if not row:
        return {"tier": None, "is_active": False}

    exp = row.get("expires_at")
    days_left = None
    if exp:
        try:
            dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            days_left = max(0, (dt - datetime.now(timezone.utc)).days)
        except Exception:
            pass

    return {
        "tier": row.get("badge_tier"),
        "status": row.get("status"),
        "expires_at": exp,
        "subscribed_at": row.get("subscribed_at"),
        "auto_renew": row.get("auto_renew", True),
        "price_paid": row.get("price_paid", 0),
        "renewal_count": row.get("renewal_count", 0),
        "days_left": days_left,
        "is_active": True,
    }


@router.post("/badge/cancel-renewal")
async def badge_cancel_renewal(authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    s = svc()
    s.table("badge_subscriptions").update({
        "auto_renew": False,
        "cancelled_at": datetime.now(timezone.utc).isoformat(),
    }).eq("user_id", uid).eq("status", "active").execute()
    return {"ok": True}
