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

    # Idempotency: if any order in this batch is already paid, return the previous result
    already_paid = [o for o in orders if o.get("status") == "paid"]
    if already_paid:
        return {
            "ok": True,
            "idempotent": True,
            "paid_orders": len(already_paid),
            "message": "This payment was already processed",
        }

    for o in orders:
        if o.get("status") not in ("pending",):
            raise HTTPException(400, "Order not payable")

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


# ============================================================
# Disputes
# ============================================================
class RaiseDisputeReq(BaseModel):
    order_id: str
    reason: str
    description: str = ""
    evidence_urls: list = []


@router.post("/marketplace/dispute")
async def marketplace_raise_dispute(req: RaiseDisputeReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    s = svc()

    o = s.table("marketplace_orders").select("*").eq("id", req.order_id).limit(1).execute()
    if not o.data or len(o.data) == 0:
        raise HTTPException(404, "Order not found")
    order = o.data[0]

    # Only buyer or seller can dispute
    if uid not in (order.get("buyer_id"), order.get("seller_id")):
        raise HTTPException(403, "Not your order")

    # Can't dispute a cancelled or confirmed order
    if order.get("status") in ("cancelled", "confirmed"):
        raise HTTPException(400, "Cannot dispute this order")

    against_id = order["seller_id"] if uid == order["buyer_id"] else order["buyer_id"]

    existing = s.table("marketplace_disputes").select("id").eq("order_id", req.order_id).eq("status", "open").execute()
    if existing.data and len(existing.data) > 0:
        raise HTTPException(400, "Dispute already open for this order")

    disp = s.table("marketplace_disputes").insert({
        "order_id": order["id"],
        "raised_by": uid,
        "against_id": against_id,
        "reason": req.reason,
        "description": req.description,
        "evidence_urls": req.evidence_urls or [],
        "status": "open",
    }).execute()
    disp_id = disp.data[0]["id"] if disp.data else None

    s.table("marketplace_orders").update({
        "status": "disputed",
        "dispute_id": disp_id,
    }).eq("id", req.order_id).execute()

    # Notify admin
    try:
        s.table("notification_queue").insert({
            "user_id": "1dacbab1-d366-4711-b86b-5d5b83d824fe",
            "title": "New dispute raised",
            "body": "Order #" + str(order.get("order_ref")) + " — " + req.reason,
        }).execute()
    except Exception:
        pass

    return {"ok": True, "dispute_id": disp_id}


class ResolveDisputeReq(BaseModel):
    dispute_id: str
    resolution: str
    action: str = "release"  # 'release' to seller, 'refund' to buyer, 'split'
    refund_pct: float = 100.0  # for split


@router.post("/marketplace/resolve-dispute")
async def marketplace_resolve_dispute(req: ResolveDisputeReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    admin_email = (user.get("email") or "").lower() == "darkmoorltd@gmail.com"
    if not admin_email:
        raise HTTPException(403, "admin only")

    s = svc()

    d = s.table("marketplace_disputes").select("*").eq("id", req.dispute_id).limit(1).execute()
    if not d.data or len(d.data) == 0:
        raise HTTPException(404, "Dispute not found")
    dispute = d.data[0]

    if dispute.get("status") != "open":
        raise HTTPException(400, "Dispute already resolved")

    esc = s.table("marketplace_escrow").select("*").eq("order_id", dispute["order_id"]).limit(1).execute()
    escrow = esc.data[0] if esc.data else None

    ts = now_iso()
    buyer_id = dispute["raised_by"] if dispute["raised_by"] != dispute["against_id"] else dispute["against_id"]
    # Determine actual buyer/seller from the order
    order_row = s.table("marketplace_orders").select("buyer_id,seller_id").eq("id", dispute["order_id"]).limit(1).execute()
    order_data = order_row.data[0] if order_row.data else {}
    actual_buyer = order_data.get("buyer_id")
    actual_seller = order_data.get("seller_id")

    released_to_seller = 0.0
    refunded_to_buyer = 0.0

    if escrow and escrow.get("status") == "held":
        amt = float(escrow.get("amount") or 0)
        seller_net = float(escrow.get("seller_net") or 0)

        if req.action == "release":
            # Full release to seller
            s.table("farmer_wallets").upsert({"user_id": actual_seller, "balance": 0}, on_conflict="user_id").execute()
            sw = s.table("farmer_wallets").select("balance").eq("user_id", actual_seller).limit(1).execute()
            sbal = float((sw.data[0] if sw.data else {}).get("balance") or 0)
            new_sbal = sbal + seller_net
            s.table("farmer_wallets").update({"balance": new_sbal}).eq("user_id", actual_seller).execute()
            s.table("wallet_transactions").insert({
                "user_id": actual_seller,
                "type": "marketplace_payout",
                "direction": "in",
                "amount": seller_net,
                "balance_after": new_sbal,
                "status": "success",
                "reference": "GAIA_DISP_REL_" + uuid.uuid4().hex[:10],
                "meta": {"dispute_id": req.dispute_id, "action": "release"},
            }).execute()
            released_to_seller = seller_net

        elif req.action == "refund":
            # Full refund to buyer
            s.table("farmer_wallets").upsert({"user_id": actual_buyer, "balance": 0}, on_conflict="user_id").execute()
            bw = s.table("farmer_wallets").select("balance").eq("user_id", actual_buyer).limit(1).execute()
            bbal = float((bw.data[0] if bw.data else {}).get("balance") or 0)
            new_bbal = bbal + amt
            s.table("farmer_wallets").update({"balance": new_bbal}).eq("user_id", actual_buyer).execute()
            s.table("wallet_transactions").insert({
                "user_id": actual_buyer,
                "type": "marketplace_refund",
                "direction": "in",
                "amount": amt,
                "balance_after": new_bbal,
                "status": "success",
                "reference": "GAIA_DISP_REF_" + uuid.uuid4().hex[:10],
                "meta": {"dispute_id": req.dispute_id, "action": "refund"},
            }).execute()
            refunded_to_buyer = amt

        elif req.action == "split":
            # Split between buyer and seller
            pct = max(0.0, min(100.0, req.refund_pct)) / 100.0
            buyer_share = round(amt * pct, 2)
            seller_share = round(amt - buyer_share, 2)
            seller_net_share = round(seller_share * (seller_net / amt) if amt > 0 else 0, 2)

            if buyer_share > 0:
                s.table("farmer_wallets").upsert({"user_id": actual_buyer, "balance": 0}, on_conflict="user_id").execute()
                bw = s.table("farmer_wallets").select("balance").eq("user_id", actual_buyer).limit(1).execute()
                bbal = float((bw.data[0] if bw.data else {}).get("balance") or 0)
                new_bbal = bbal + buyer_share
                s.table("farmer_wallets").update({"balance": new_bbal}).eq("user_id", actual_buyer).execute()
                s.table("wallet_transactions").insert({
                    "user_id": actual_buyer,
                    "type": "marketplace_refund",
                    "direction": "in",
                    "amount": buyer_share,
                    "balance_after": new_bbal,
                    "status": "success",
                    "reference": "GAIA_DISP_SPL_" + uuid.uuid4().hex[:10],
                    "meta": {"dispute_id": req.dispute_id, "action": "split"},
                }).execute()
                refunded_to_buyer = buyer_share

            if seller_net_share > 0:
                s.table("farmer_wallets").upsert({"user_id": actual_seller, "balance": 0}, on_conflict="user_id").execute()
                sw = s.table("farmer_wallets").select("balance").eq("user_id", actual_seller).limit(1).execute()
                sbal = float((sw.data[0] if sw.data else {}).get("balance") or 0)
                new_sbal = sbal + seller_net_share
                s.table("farmer_wallets").update({"balance": new_sbal}).eq("user_id", actual_seller).execute()
                s.table("wallet_transactions").insert({
                    "user_id": actual_seller,
                    "type": "marketplace_payout",
                    "direction": "in",
                    "amount": seller_net_share,
                    "balance_after": new_sbal,
                    "status": "success",
                    "reference": "GAIA_DISP_SPL_" + uuid.uuid4().hex[:10],
                    "meta": {"dispute_id": req.dispute_id, "action": "split"},
                }).execute()
                released_to_seller = seller_net_share

        final_status = "released" if req.action in ("release", "split") else "refunded"
        update_payload = {
            "status": final_status,
            "release_ref": "GAIA_DISP_" + req.dispute_id[:8],
        }
        if final_status == "released":
            update_payload["released_at"] = ts
        else:
            update_payload["refunded_at"] = ts
        s.table("marketplace_escrow").update(update_payload).eq("id", escrow["id"]).execute()

    s.table("marketplace_disputes").update({
        "status": "resolved",
        "resolution": req.resolution,
        "refund_amount": refunded_to_buyer,
        "resolved_by": user["sub"],
        "resolved_at": ts,
    }).eq("id", req.dispute_id).execute()

    s.table("marketplace_orders").update({"status": "confirmed" if req.action == "release" else "cancelled"}).eq("id", dispute["order_id"]).execute()

    return {
        "ok": True,
        "action": req.action,
        "released_to_seller": released_to_seller,
        "refunded_to_buyer": refunded_to_buyer,
    }


# ============================================================
# Reviews
# ============================================================
class ReviewReq(BaseModel):
    order_id: str
    rating: int
    comment: str = ""


@router.post("/marketplace/review")
async def marketplace_review(req: ReviewReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]

    if req.rating < 1 or req.rating > 5:
        raise HTTPException(400, "Rating must be 1-5")

    s = svc()
    o = s.table("marketplace_orders").select("*").eq("id", req.order_id).limit(1).execute()
    if not o.data or len(o.data) == 0:
        raise HTTPException(404, "Order not found")
    order = o.data[0]

    if order.get("buyer_id") != uid:
        raise HTTPException(403, "Only the buyer can review")
    if order.get("status") != "confirmed":
        raise HTTPException(400, "Confirm delivery before reviewing")

    existing = s.table("marketplace_reviews").select("id").eq("order_id", req.order_id).limit(1).execute()
    if existing.data and len(existing.data) > 0:
        raise HTTPException(400, "Already reviewed")

    s.table("marketplace_reviews").insert({
        "order_id": order["id"],
        "listing_id": order.get("listing_id"),
        "seller_id": order.get("seller_id"),
        "reviewer_id": uid,
        "rating": req.rating,
        "comment": req.comment or "",
    }).execute()

    s.table("marketplace_orders").update({"reviewed_at": now_iso()}).eq("id", req.order_id).execute()

    return {"ok": True}


@router.get("/marketplace/reviews/{seller_id}")
async def marketplace_get_reviews(seller_id: str):
    s = svc()
    rows = s.table("marketplace_reviews").select("*").eq("seller_id", seller_id).order("created_at", desc=True).limit(20).execute()
    return {"reviews": rows.data or []}
