# app/routers/marketplace.py
# Marketplace escrow: buyer wallet -> escrow -> seller wallet
import os
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from app.services.auth import verify_supabase_token
from app.routers.wallet_pin import check_pin
from supabase import create_client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
PLATFORM_FEE_PCT = 0.05


def svc():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def auth_user(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        return verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))


class PayReq(BaseModel):
    order_ref: str
    pin: str


@router.post("/marketplace/pay")
async def marketplace_pay(req: PayReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    check_pin(uid, req.pin)

    s = svc()
    rows = s.table("marketplace_orders").select("*").eq("payment_ref", req.order_ref).execute()
    if not rows.data or len(rows.data) == 0:
        raise HTTPException(404, "No orders found for this reference")
    orders = rows.data

    for o in orders:
        if o.get("buyer_id") != uid:
            raise HTTPException(403, "Not your order")
        if o.get("status") != "pending":
            raise HTTPException(400, "Order already paid or cancelled")

    total = 0.0
    for o in orders:
        total += float(o.get("total") or 0)
    if total <= 0:
        raise HTTPException(400, "Invalid total")

    ww = s.table("farmer_wallets").select("balance").eq("user_id", uid).limit(1).execute()
    bal = float((ww.data[0] if ww.data else {}).get("balance") or 0)
    if bal < total:
        raise HTTPException(400, "Insufficient wallet balance")

    new_bal = bal - total
    s.table("farmer_wallets").update({"balance": new_bal}).eq("user_id", uid).execute()

    buyer_ref = "GAIA_MKT_PAY_" + uuid.uuid4().hex[:12]
    s.table("wallet_transactions").insert({
        "user_id": uid,
        "type": "marketplace_purchase",
        "direction": "out",
        "amount": total,
        "balance_after": new_bal,
        "status": "success",
        "reference": buyer_ref,
        "counterparty_name": req.order_ref,
        "meta": {"order_ref": req.order_ref, "order_count": len(orders)},
    }).execute()

    for o in orders:
        amt = float(o.get("total") or 0)
        fee = round(amt * PLATFORM_FEE_PCT, 2)
        net = round(amt - fee, 2)

        esc = s.table("marketplace_escrow").insert({
            "order_id": o["id"],
            "buyer_id": uid,
            "seller_id": o["seller_id"],
            "amount": amt,
            "platform_fee": fee,
            "seller_net": net,
            "status": "held",
        }).execute()
        escrow_id = esc.data[0]["id"] if esc.data else None

        s.table("marketplace_orders").update({
            "status": "paid",
            "paid_at": now_iso(),
            "escrow_id": escrow_id,
            "platform_fee": fee,
            "seller_net": net,
        }).eq("id", o["id"]).execute()

        try:
            s.table("notification_queue").insert({
                "user_id": o["seller_id"],
                "title": "New paid order",
                "body": "Order " + str(o.get("order_ref")) + " is paid. Ship to release funds.",
            }).execute()
        except Exception:
            pass

    return {
        "ok": True,
        "paid_orders": len(orders),
        "total_charged": total,
        "new_balance": new_bal,
        "reference": buyer_ref,
    }


class ReleaseReq(BaseModel):
    order_id: str


@router.post("/marketplace/release")
async def marketplace_release(req: ReleaseReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    s = svc()

    o = s.table("marketplace_orders").select("*").eq("id", req.order_id).limit(1).execute()
    if not o.data or len(o.data) == 0:
        raise HTTPException(404, "Order not found")
    order = o.data[0]

    if order.get("buyer_id") != uid:
        raise HTTPException(403, "Not your order")
    if order.get("status") not in ("paid", "shipped", "delivered"):
        raise HTTPException(400, "Order not in a releasable state")

    esc = s.table("marketplace_escrow").select("*").eq("order_id", req.order_id).limit(1).execute()
    if not esc.data or len(esc.data) == 0:
        raise HTTPException(404, "No escrow for this order")
    escrow = esc.data[0]

    if escrow.get("status") != "held":
        raise HTTPException(400, "Escrow already settled")

    seller_id = escrow["seller_id"]
    net = float(escrow["seller_net"] or 0)

    s.table("farmer_wallets").upsert({"user_id": seller_id, "balance": 0}, on_conflict="user_id").execute()
    sw = s.table("farmer_wallets").select("balance").eq("user_id", seller_id).limit(1).execute()
    sbal = float((sw.data[0] if sw.data else {}).get("balance") or 0)
    new_sbal = sbal + net
    s.table("farmer_wallets").update({"balance": new_sbal}).eq("user_id", seller_id).execute()

    ref = "GAIA_MKT_REL_" + uuid.uuid4().hex[:12]
    s.table("wallet_transactions").insert({
        "user_id": seller_id,
        "type": "marketplace_payout",
        "direction": "in",
        "amount": net,
        "balance_after": new_sbal,
        "status": "success",
        "reference": ref,
        "counterparty_name": "GAIA Marketplace",
        "meta": {"order_id": req.order_id, "order_ref": order.get("order_ref")},
    }).execute()

    ts = now_iso()
    s.table("marketplace_escrow").update({
        "status": "released",
        "released_at": ts,
        "release_ref": ref,
    }).eq("id", escrow["id"]).execute()

    s.table("marketplace_orders").update({
        "status": "confirmed",
        "confirmed_at": ts,
        "release_ref": ref,
    }).eq("id", req.order_id).execute()

    try:
        s.table("notification_queue").insert({
            "user_id": seller_id,
            "title": "Funds released",
            "body": "Wallet credited. Order payout complete.",
        }).execute()
    except Exception:
        pass

    return {
        "ok": True,
        "seller_credited": net,
        "seller_balance": new_sbal,
        "reference": ref,
    }


class CancelReq(BaseModel):
    order_id: str
    reason: str = ""


@router.post("/marketplace/cancel")
async def marketplace_cancel(req: CancelReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    admin_email = (user.get("email") or "").lower() == "darkmoorltd@gmail.com"
    s = svc()

    o = s.table("marketplace_orders").select("*").eq("id", req.order_id).limit(1).execute()
    if not o.data or len(o.data) == 0:
        raise HTTPException(404, "Order not found")
    order = o.data[0]

    if order.get("buyer_id") != uid and not admin_email:
        raise HTTPException(403, "Not your order")

    esc = s.table("marketplace_escrow").select("*").eq("order_id", req.order_id).limit(1).execute()
    escrow = esc.data[0] if esc.data else None

    ts = now_iso()
    refunded = False

    if escrow and escrow.get("status") == "held" and admin_email:
        buyer_id = escrow["buyer_id"]
        amt = float(escrow["amount"] or 0)
        s.table("farmer_wallets").upsert({"user_id": buyer_id, "balance": 0}, on_conflict="user_id").execute()
        bw = s.table("farmer_wallets").select("balance").eq("user_id", buyer_id).limit(1).execute()
        bbal = float((bw.data[0] if bw.data else {}).get("balance") or 0)
        newb = bbal + amt
        s.table("farmer_wallets").update({"balance": newb}).eq("user_id", buyer_id).execute()

        ref = "GAIA_MKT_REF_" + uuid.uuid4().hex[:12]
        s.table("wallet_transactions").insert({
            "user_id": buyer_id,
            "type": "marketplace_refund",
            "direction": "in",
            "amount": amt,
            "balance_after": newb,
            "status": "success",
            "reference": ref,
            "meta": {"order_id": req.order_id},
        }).execute()

        s.table("marketplace_escrow").update({
            "status": "refunded",
            "refunded_at": ts,
            "refund_reason": req.reason or "refunded",
        }).eq("id", escrow["id"]).execute()

        refunded = True

    s.table("marketplace_orders").update({
        "status": "cancelled",
        "cancelled_at": ts,
        "cancel_reason": req.reason or "cancelled",
    }).eq("id", req.order_id).execute()

    return {"ok": True, "refunded": refunded}
