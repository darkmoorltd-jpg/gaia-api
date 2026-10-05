# app/routers/payment.py
import os, logging, httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

log = logging.getLogger('gaia.payment')
router = APIRouter()

PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_VERIFY = "https://api.paystack.co/transaction/verify/"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_KEY", "")

PLANS = {
    "starter":    {"scans": 150,  "kobo": 300000},
    "pro":        {"scans": 300,  "kobo": 500000},
    "business":   {"scans": 1000, "kobo": 1000000},
    "enterprise": {"scans": 5000, "kobo": 2000000},
}
REFERRAL_BONUS_SCANS = 50


class VerifyBody(BaseModel):
    reference: str
    plan: str


def _h() -> dict:
    return {
        "apikey": SUPABASE_SERVICE,
        "Authorization": "Bearer " + SUPABASE_SERVICE,
        "Content-Type": "application/json",
    }


async def _uid(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(SUPABASE_URL + "/auth/v1/user",
                        headers={"apikey": SUPABASE_SERVICE, "Authorization": "Bearer " + token})
    if r.status_code != 200:
        raise HTTPException(401, "Invalid token")
    return r.json()["id"]


async def _scan_row(uid):
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(SUPABASE_URL + "/rest/v1/user_scans", headers=_h(),
                        params={"user_id": "eq." + uid, "select": "*"})
    d = r.json() if r.status_code == 200 else []
    return d[0] if d else {}


async def _upsert_scans(uid, updates):
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(SUPABASE_URL + "/rest/v1/user_scans",
                         headers={**_h(), "Prefer": "resolution=merge-duplicates,return=representation"},
                         params={"on_conflict": "user_id"},
                         json={"user_id": uid, **updates})
    if r.status_code not in (200, 201):
        log.error("upsert failed: %s %s", r.status_code, r.text[:200])
        raise HTTPException(500, "Could not update scans")
    data = r.json()
    return data[0] if isinstance(data, list) and data else data


@router.post("/payment/verify-scan-purchase")
async def verify(body: VerifyBody, authorization: str = Header(None)):
    if not PAYSTACK_SECRET:
        raise HTTPException(500, "PAYSTACK_SECRET_KEY not configured")
    if body.plan not in PLANS:
        raise HTTPException(400, "Unknown plan")
    uid = await _uid(authorization)
    plan = PLANS[body.plan]

    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(PAYSTACK_VERIFY + body.reference,
                        headers={"Authorization": "Bearer " + PAYSTACK_SECRET})
    if r.status_code != 200:
        raise HTTPException(400, "Paystack verify failed HTTP " + str(r.status_code))
    txn = r.json().get("data", {})
    if txn.get("status") != "success":
        raise HTTPException(400, "Not successful: " + str(txn.get("status")))
    amt = int(txn.get("amount", 0))
    if amt < plan["kobo"]:
        raise HTTPException(400, "Amount too low")

    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(SUPABASE_URL + "/rest/v1/payment_history", headers=_h(),
                        params={"reference": "eq." + body.reference, "select": "id"})
        if r.status_code == 200 and r.json():
            raise HTTPException(409, "Reference already used")

    cur = await _scan_row(uid)
    new_total = int(cur.get("scans_remaining", 30) or 30) + plan["scans"]
    lt = int(cur.get("lifetime_scans_purchased", 0) or 0)

    await _upsert_scans(uid, {
        "scans_remaining": new_total,
        "plan": body.plan,
        "lifetime_scans_purchased": lt + plan["scans"],
    })

    async with httpx.AsyncClient(timeout=15.0) as c:
        await c.post(SUPABASE_URL + "/rest/v1/payment_history", headers=_h(),
                     json={"user_id": uid, "amount": amt / 100, "plan": body.plan,
                           "reference": body.reference, "scans_added": plan["scans"]})

    # Referral
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(SUPABASE_URL + "/rest/v1/user_scans", headers=_h(),
                            params={"user_id": "eq." + uid, "select": "referred_by,lifetime_scans_purchased"})
            row = (r.json() or [{}])[0]
        ref_id = row.get("referred_by")
        lt_after = int(row.get("lifetime_scans_purchased", 0) or 0)
        if ref_id and lt_after == plan["scans"]:
            rc = await _scan_row(ref_id)
            rs = int(rc.get("scans_remaining", 0) or 0)
            await _upsert_scans(ref_id, {"scans_remaining": rs + REFERRAL_BONUS_SCANS})
            async with httpx.AsyncClient(timeout=15.0) as c:
                await c.post(SUPABASE_URL + "/rest/v1/referral_bonuses", headers=_h(),
                             json={"referrer_id": ref_id, "referred_id": uid,
                                   "bonus_scans": REFERRAL_BONUS_SCANS,
                                   "reference": body.reference})
    except Exception as e:
        log.warning("referral failed: %s", str(e)[:150])

    return {"ok": True, "scans_added": plan["scans"], "new_balance": new_total,
            "plan": body.plan, "amount_naira": amt / 100}


@router.get("/payment/plans")
async def plans():
    return {"plans": [{"key": k, "scans": v["scans"], "naira": v["kobo"] / 100} for k, v in PLANS.items()]}
