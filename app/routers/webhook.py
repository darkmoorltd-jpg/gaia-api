import os
import json
import hmac
import hashlib
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, HTTPException, Header
import requests

router = APIRouter()

PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET_KEY", "")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# Amount (in kobo) -> (plan_key, scans_to_add)
BUY_SCAN_AMOUNTS = {
    300000:  ("starter", 150),
    500000:  ("pro", 300),
    1000000: ("business", 1000),
    2000000: ("enterprise", 5000),
}

# Amount (in kobo) -> badge tier
BADGE_AMOUNTS = {
    50000:   "bronze",
    150000:  "silver",
    300000:  "gold",
    500000:  "platinum",
}

VERIFICATION_AMOUNTS = {200000}  # N2,000


def _find_user_by_email(email: str):
    """Return user_id (uuid) for an email, or None."""
    r = requests.get(
        SUPABASE_URL + "/rest/v1/user_profiles?email=eq." + email.lower() + "&select=user_id",
        headers=HEADERS, timeout=10,
    )
    rows = r.json()
    if rows:
        return rows[0]["user_id"]
    # Fallback: hit auth.users via admin API
    r = requests.get(
        SUPABASE_URL + "/auth/v1/admin/users?email=" + email.lower(),
        headers=HEADERS, timeout=10,
    )
    try:
        data = r.json()
        if isinstance(data, dict) and data.get("users"):
            return data["users"][0]["id"]
    except Exception:
        pass
    return None


def _ensure_scans_row(user_id: str):
    """Create user_scans row with 0 scans if it doesn't exist."""
    r = requests.get(
        SUPABASE_URL + "/rest/v1/user_scans?user_id=eq." + user_id + "&select=user_id",
        headers=HEADERS, timeout=10,
    )
    if not r.json():
        requests.post(
            SUPABASE_URL + "/rest/v1/user_scans",
            headers=HEADERS,
            json={"user_id": user_id, "scans_remaining": 0, "plan": "free"},
            timeout=10,
        )


def _add_scans(user_id: str, plan: str, scans: int):
    """Add scans to the user's balance (adds to existing balance)."""
    _ensure_scans_row(user_id)
    r = requests.get(
        SUPABASE_URL + "/rest/v1/user_scans?user_id=eq." + user_id + "&select=scans_remaining",
        headers=HEADERS, timeout=10,
    )
    rows = r.json()
    current = rows[0]["scans_remaining"] if rows else 0
    new_total = current + scans

    requests.patch(
        SUPABASE_URL + "/rest/v1/user_scans?user_id=eq." + user_id,
        headers=HEADERS,
        json={"scans_remaining": new_total, "plan": plan},
        timeout=10,
    )
    return new_total


def _activate_badge(user_id: str, tier: str):
    """Create or extend a badge subscription for 30 days."""
    expiry = (datetime.utcnow() + timedelta(days=30)).isoformat()

    # Delete previous active row (simplest)
    requests.delete(
        SUPABASE_URL + "/rest/v1/badge_subscriptions?user_id=eq." + user_id,
        headers=HEADERS, timeout=10,
    )
    requests.post(
        SUPABASE_URL + "/rest/v1/badge_subscriptions",
        headers=HEADERS,
        json={
            "user_id": user_id,
            "plan": tier,
            "status": "active",
            "start_date": datetime.utcnow().isoformat(),
            "expiry": expiry,
        },
        timeout=10,
    )


def _mark_verification_paid(user_id: str, reference: str):
    requests.patch(
        SUPABASE_URL + "/rest/v1/farmer_verifications?user_id=eq." + user_id,
        headers=HEADERS,
        json={
            "status": "pending",           # moves from pending_payment to pending admin review
            "payment_status": "paid",
            "payment_reference": reference,
        },
        timeout=10,
    )


def _log_payment(user_id: str, amount_kobo: int, plan: str, scans: int, reference: str):
    requests.post(
        SUPABASE_URL + "/rest/v1/payment_history",
        headers=HEADERS,
        json={
            "user_id": user_id,
            "amount": amount_kobo,          # stored in kobo (matches web app)
            "scans_added": scans,
            "plan": plan,
            "reference": reference,
            "paid_at": datetime.utcnow().isoformat(),
        },
        timeout=10,
    )


@router.post("/webhook/paystack")
async def paystack_webhook(
    request: Request,
    x_paystack_signature: str = Header(None),
):
    raw = await request.body()

    # Verify signature (SHA512)
    if not PAYSTACK_SECRET:
        raise HTTPException(500, "PAYSTACK_SECRET_KEY not configured")

    expected = hmac.new(
        PAYSTACK_SECRET.encode(), raw, hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(expected, x_paystack_signature or ""):
        raise HTTPException(401, "Invalid signature")

    event = json.loads(raw)

    if event.get("event") != "charge.success":
        return {"ok": True, "ignored": True}

    data = event["data"]
    reference = data["reference"]
    email = data["customer"]["email"]
    amount = int(data["amount"])            # kobo
    metadata = data.get("metadata") or {}

    user_id = _find_user_by_email(email)
    if not user_id:
        return {"ok": True, "user_not_found": True, "email": email}

    plan = metadata.get("plan")
    if isinstance(plan, dict):
        plan = plan.get("plan")

    # 1. Buy scans
    if amount in BUY_SCAN_AMOUNTS:
        plan_key, scans = BUY_SCAN_AMOUNTS[amount]
        _add_scans(user_id, plan_key, scans)
        _log_payment(user_id, amount, plan_key, scans, reference)
        return {"ok": True, "credited": scans, "plan": plan_key}

    # 2. Badge purchase
    if amount in BADGE_AMOUNTS:
        tier = BADGE_AMOUNTS[amount]
        _activate_badge(user_id, tier)
        _log_payment(user_id, amount, "badge_" + tier, 0, reference)
        return {"ok": True, "badge": tier}

    # 3. Verification
    if amount in VERIFICATION_AMOUNTS or plan == "verification":
        _mark_verification_paid(user_id, reference)
        _log_payment(user_id, amount, "verification", 0, reference)
        return {"ok": True, "verification": "paid"}

    # 4. Unknown amount — still log it
    _log_payment(user_id, amount, plan or "unknown", 0, reference)
    return {"ok": True, "logged_unknown": amount}
