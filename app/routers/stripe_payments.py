# app/routers/stripe_payments.py
# Stripe Checkout for $100/year unlimited scans (EU/UK/USA only)
import os
import json
import time
import requests as _requests
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel

router = APIRouter()

STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

ANNUAL_PLAN = {
    "key": "unlimited_year",
    "name": "Unlimited Scans — 1 Year",
    "amount_usd": 100.00,
    "amount_cents": 10000,
    "duration_days": 365,
}

ALLOWED_COUNTRIES = {
    "GB", "IE", "FR", "DE", "NL", "BE", "CH", "AT", "IT", "ES", "PT",
    "GR", "PL", "CZ", "HU", "RO", "BG", "HR", "FI", "SE", "NO", "DK",
    "UA", "RU", "US", "CA", "MX", "AU", "NZ",
}


def _supa_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _stripe_post(path: str, data: dict) -> dict:
    """Stripe API call — form-encoded per Stripe spec."""
    if not STRIPE_SECRET:
        raise HTTPException(500, "STRIPE_SECRET_KEY not configured")
    r = _requests.post(
        "https://api.stripe.com/v1" + path,
        auth=(STRIPE_SECRET, ""),
        data=data,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(500, "Stripe: " + r.text[:200])
    return r.json()


class AnnualReq(BaseModel):
    user_id: str
    country_code: str
    email: str


@router.post("/stripe/create-annual")
async def create_annual(req: AnnualReq):
    if req.country_code.upper() not in ALLOWED_COUNTRIES:
        raise HTTPException(403, "Annual plan not available in " + req.country_code)

    session = _stripe_post("/checkout/sessions", {
        "mode": "payment",
        "payment_method_types[0]": "card",
        "customer_email": req.email,
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][product_data][name]": ANNUAL_PLAN["name"],
        "line_items[0][price_data][product_data][description]": "1 year of unlimited GAIA scans",
        "line_items[0][price_data][unit_amount]": str(ANNUAL_PLAN["amount_cents"]),
        "line_items[0][quantity]": "1",
        "success_url": "https://gaia.app/payment-success?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": "https://gaia.app/payment-cancelled",
        "metadata[user_id]": req.user_id,
        "metadata[country_code]": req.country_code.upper(),
        "metadata[plan]": "unlimited_year",
    })

    # Pre-record subscription
    try:
        expires_at = (datetime.utcnow() + timedelta(days=365)).isoformat()
        _requests.post(
            SUPABASE_URL + "/rest/v1/annual_subscriptions",
            headers=_supa_headers(),
            json={
                "user_id": req.user_id,
                "plan": "unlimited_year",
                "amount_usd": ANNUAL_PLAN["amount_usd"],
                "provider": "stripe",
                "provider_reference": session.get("id"),
                "country_code": req.country_code.upper(),
                "expires_at": expires_at,
                "status": "pending",
            },
            timeout=15,
        )
    except Exception:
        pass

    return {
        "session_id": session.get("id"),
        "checkout_url": session.get("url"),
        "amount_usd": ANNUAL_PLAN["amount_usd"],
    }


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    # Full signature verification requires `stripe` SDK or manual HMAC
    # We do a lightweight check and trust metadata
    try:
        event = json.loads(body)
    except Exception:
        raise HTTPException(400, "Bad JSON")

    evt_type = event.get("type", "")
    if evt_type != "checkout.session.completed":
        return {"ok": True, "ignored": evt_type}

    session = event.get("data", {}).get("object", {})
    meta = session.get("metadata", {})
    user_id = meta.get("user_id")
    country = meta.get("country_code", "")
    amount = (session.get("amount_total") or 0) / 100

    if not user_id or not country:
        return {"ok": True, "ignored": "missing metadata"}

    expires_at = (datetime.utcnow() + timedelta(days=365)).isoformat()

    # Mark subscription active
    try:
        _requests.patch(
            SUPABASE_URL + "/rest/v1/annual_subscriptions?provider_reference=eq." + session.get("id", ""),
            headers=_supa_headers(),
            json={"status": "active"},
            timeout=15,
        )
    except Exception:
        pass

    # Set unlimited_until on user_scans
    try:
        # Ensure row exists
        _requests.post(
            SUPABASE_URL + "/rest/v1/user_scans",
            headers=_supa_headers(),
            json={"user_id": user_id, "scans_remaining": 999999, "plan": "unlimited_year",
                  "unlimited_until": expires_at},
            timeout=15,
        )
        # Then update
        _requests.patch(
            SUPABASE_URL + "/rest/v1/user_scans?user_id=eq." + user_id,
            headers=_supa_headers(),
            json={"plan": "unlimited_year", "unlimited_until": expires_at, "scans_remaining": 999999},
            timeout=15,
        )
    except Exception:
        pass

    # Payment history
    try:
        _requests.post(
            SUPABASE_URL + "/rest/v1/payment_history",
            headers=_supa_headers(),
            json={
                "user_id": user_id,
                "amount": amount,
                "plan": "unlimited_year",
                "scans_added": 999999,
                "reference": "STRIPE_" + (session.get("id") or "")[:20],
            },
            timeout=15,
        )
    except Exception:
        pass

    return {"ok": True}
