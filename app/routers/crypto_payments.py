# app/routers/crypto_payments.py
# NOWPayments integration — accept crypto for scan packs
# Docs: https://documenter.getpostman.com/view/7907941/S1a32n38

import os
import json
import hmac
import hashlib
import requests as _requests
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

NOWPAY_KEY = os.environ.get("NOWPAY_API_KEY", "")
NOWPAY_IPN = os.environ.get("NOWPAY_IPN_SECRET", "")
NOWPAY_BASE = "https://api.nowpayments.io/v1"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# USD pricing for each scan pack
PLANS = {
    "starter":    {"scans": 150,  "usd": 2.00,  "label": "Starter – 150 scans"},
    "pro":        {"scans": 300,  "usd": 3.50,  "label": "Pro – 300 scans"},
    "business":   {"scans": 1000, "usd": 6.50,  "label": "Business – 1000 scans"},
    "enterprise": {"scans": 5000, "usd": 13.00, "label": "Enterprise – 5000 scans"},
}


def _supa_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


class InvoiceReq(BaseModel):
    user_id: str
    plan: str
    pay_currency: Optional[str] = "usdttrc20"


@router.post("/crypto/create-invoice")
async def create_invoice(req: InvoiceReq):
    if not NOWPAY_KEY:
        raise HTTPException(500, "NOWPAY_API_KEY not configured")
    if req.plan not in PLANS:
        raise HTTPException(400, "Unknown plan")

    plan = PLANS[req.plan]
    order_id = f"gaia_{req.user_id[:8]}_{int(datetime.now().timestamp())}"

    payload = {
        "price_amount": plan["usd"],
        "price_currency": "usd",
        "pay_currency": req.pay_currency or "usdttrc20",
        "order_id": order_id,
        "order_description": plan["label"],
        "ipn_callback_url": "https://gaia-api-xuly.onrender.com/webhook/nowpayments",
        "success_url": "https://gaia.app/payment-success",
        "cancel_url": "https://gaia.app/payment-cancelled",
    }

    try:
        r = _requests.post(
            NOWPAY_BASE + "/invoice",
            headers={"x-api-key": NOWPAY_KEY, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(500, "NOWPay: " + r.text[:200])
        data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "Network: " + str(e)[:100])

    invoice_id = data.get("id")
    if not invoice_id:
        raise HTTPException(500, "No invoice id in response")

    # Persist for reconciliation
    try:
        _requests.post(
            SUPABASE_URL + "/rest/v1/crypto_payments",
            headers=_supa_headers(),
            json={
                "user_id": req.user_id,
                "invoice_id": str(invoice_id),
                "order_id": order_id,
                "plan": req.plan,
                "scans_to_add": plan["scans"],
                "price_amount": plan["usd"],
                "price_currency": "usd",
                "pay_currency": payload["pay_currency"],
                "status": "waiting",
            },
            timeout=15,
        )
    except Exception:
        pass

    return {
        "invoice_id": str(invoice_id),
        "invoice_url": data.get("invoice_url"),
        "order_id": order_id,
        "amount_usd": plan["usd"],
        "scans": plan["scans"],
    }


@router.post("/webhook/nowpayments")
async def webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("x-nowpayments-sig", "")

    if not NOWPAY_IPN:
        # Accept without signature only if IPN secret not set (dev mode)
        try:
            data = json.loads(body)
        except Exception:
            raise HTTPException(400, "Bad JSON")
    else:
        # NOWPayments signs: HMAC-SHA512(secret, JSON.stringify(sorted_keys))
        try:
            data = json.loads(body)
            canonical = json.dumps(data, separators=(",", ":"), sort_keys=True)
            expected = hmac.new(NOWPAY_IPN.encode(), canonical.encode(), hashlib.sha512).hexdigest()
            if not hmac.compare_digest(expected, sig):
                raise HTTPException(401, "Bad signature")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "Bad signature format")

    status = data.get("payment_status", "")
    invoice_id = str(data.get("invoice_id") or "")
    order_id = data.get("order_id") or ""
    actually_paid = float(data.get("actually_paid") or 0)
    pay_address = data.get("pay_address")

    if not invoice_id:
        return {"ok": True, "ignored": "no invoice_id"}

    # Look up the invoice row
    try:
        r = _requests.get(
            SUPABASE_URL + "/rest/v1/crypto_payments?select=*&invoice_id=eq." + invoice_id,
            headers=_supa_headers(),
            timeout=15,
        )
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        return {"ok": True, "ignored": "db lookup failed"}

    if not rows:
        return {"ok": True, "ignored": "invoice not found"}

    row = rows[0]
    already_credited = row.get("status") in ("finished", "credited")

    # Update invoice status
    try:
        _requests.patch(
            SUPABASE_URL + "/rest/v1/crypto_payments?invoice_id=eq." + invoice_id,
            headers=_supa_headers(),
            json={
                "status": status,
                "actually_paid": actually_paid,
                "pay_address": pay_address,
                "payment_id": str(data.get("payment_id") or ""),
                "updated_at": datetime.now().isoformat(),
            },
            timeout=15,
        )
    except Exception:
        pass

    # Credit scans when payment is finished
    if status in ("finished", "confirmed") and not already_credited:
        user_id = row["user_id"]
        scans = int(row["scans_to_add"])
        plan_key = row["plan"]

        try:
            # Current balance
            cur = _requests.get(
                SUPABASE_URL + "/rest/v1/user_scans?select=scans_remaining&user_id=eq." + user_id,
                headers=_supa_headers(),
                timeout=15,
            )
            cur_rows = cur.json() if cur.status_code == 200 else []
            current = cur_rows[0]["scans_remaining"] if cur_rows else 0
            new_total = current + scans

            _requests.patch(
                SUPABASE_URL + "/rest/v1/user_scans?user_id=eq." + user_id,
                headers=_supa_headers(),
                json={"scans_remaining": new_total, "plan": plan_key},
                timeout=15,
            )

            _requests.post(
                SUPABASE_URL + "/rest/v1/payment_history",
                headers=_supa_headers(),
                json={
                    "user_id": user_id,
                    "amount": row["price_amount"],
                    "scans_added": scans,
                    "plan": plan_key,
                    "reference": "CRYPTO_" + invoice_id,
                },
                timeout=15,
            )

            _requests.patch(
                SUPABASE_URL + "/rest/v1/crypto_payments?invoice_id=eq." + invoice_id,
                headers=_supa_headers(),
                json={"status": "credited"},
                timeout=15,
            )
        except Exception as e:
            print("[webhook] credit failed:", e)

    return {"ok": True}
