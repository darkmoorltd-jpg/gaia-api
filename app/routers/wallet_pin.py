# app/routers/wallet_pin.py
# Wallet PIN — set and verify. NEVER touches balance.
import os
import hashlib
import hmac
from datetime import datetime, timedelta
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from app.services.auth import verify_supabase_token
from supabase import create_client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
PIN_PEPPER = os.environ.get("PIN_PEPPER", "gaia-wallet-pepper-v1")


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


def hash_pin(user_id, pin):
    raw = (user_id + ":" + pin + ":" + PIN_PEPPER).encode()
    return hashlib.sha256(raw).hexdigest()


class SetPinReq(BaseModel):
    pin: str


@router.post("/wallet/set-pin")
async def wallet_set_pin(req: SetPinReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]

    pin = (req.pin or "").strip()
    if len(pin) < 4 or len(pin) > 8:
        raise HTTPException(400, "PIN must be 4-8 digits")
    if not pin.isdigit():
        raise HTTPException(400, "PIN must contain only digits")

    h = hash_pin(uid, pin)
    s = svc()

    # Insert-only if missing. NEVER overwrite balance.
    existing = s.table("farmer_wallets").select("user_id").eq("user_id", uid).limit(1).execute()
    if not existing.data:
        s.table("farmer_wallets").insert({
            "user_id": uid,
            "balance": 0,
            "pin_hash": h,
            "updated_at": datetime.utcnow().isoformat(),
        }).execute()
    else:
        s.table("farmer_wallets").update({
            "pin_hash": h,
            "failed_pin_attempts": 0,
            "pin_locked_until": None,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("user_id", uid).execute()

    return {"ok": True}


@router.get("/wallet/has-pin")
async def wallet_has_pin(authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    s = svc()
    r = s.table("farmer_wallets").select("pin_hash").eq("user_id", uid).limit(1).execute()
    has = bool(r.data and len(r.data) > 0 and r.data[0].get("pin_hash"))
    return {"has_pin": has}


class VerifyPinReq(BaseModel):
    pin: str


@router.post("/wallet/verify-pin")
async def wallet_verify_pin(req: VerifyPinReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    _check_pin(uid, (req.pin or "").strip())
    return {"ok": True}


class ResetPinReq(BaseModel):
    password: str
    new_pin: str


@router.post("/wallet/reset-pin")
async def wallet_reset_pin(req: ResetPinReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    email = user.get("email") or ""

    if not email:
        raise HTTPException(400, "Account email missing")

    import httpx
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            SUPABASE_URL + "/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_SERVICE_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": req.password},
        )
    if r.status_code != 200:
        raise HTTPException(400, "Incorrect password")

    pin = (req.new_pin or "").strip()
    if len(pin) < 4 or len(pin) > 8 or not pin.isdigit():
        raise HTTPException(400, "PIN must be 4-8 digits")

    h = hash_pin(uid, pin)
    s = svc()
    s.table("farmer_wallets").update({
        "pin_hash": h,
        "failed_pin_attempts": 0,
        "pin_locked_until": None,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("user_id", uid).execute()

    return {"ok": True, "reset": True}


def check_pin(uid: str, pin: str) -> bool:
    """Verify the PIN with lockout. Raises HTTPException on failure."""
    if not pin:
        raise HTTPException(400, "PIN required")

    s = svc()
    r = s.table("farmer_wallets").select(
        "pin_hash,failed_pin_attempts,pin_locked_until"
    ).eq("user_id", uid).limit(1).execute()

    if not r.data or len(r.data) == 0 or not r.data[0].get("pin_hash"):
        raise HTTPException(400, "Set your transfer PIN first in Wallet")

    row = r.data[0]

    # Locked?
    locked = row.get("pin_locked_until")
    if locked:
        try:
            locked_dt = datetime.fromisoformat(str(locked).replace("Z", "+00:00"))
            if locked_dt.tzinfo is not None:
                now = datetime.now(locked_dt.tzinfo)
            else:
                now = datetime.utcnow()
            if locked_dt > now:
                remaining = int((locked_dt - now).total_seconds() // 60) + 1
                raise HTTPException(429, "PIN locked. Try again in " + str(remaining) + " minutes.")
        except HTTPException:
            raise
        except Exception:
            pass

    stored = row["pin_hash"]
    expected = hash_pin(uid, pin)
    if not hmac.compare_digest(stored, expected):
        # Record failure atomically
        try:
            s.rpc("wallet_record_pin_failure", {"p_user_id": uid}).execute()
        except Exception:
            pass
        raise HTTPException(403, "Incorrect PIN")

    # Success — clear failures
    try:
        s.rpc("wallet_clear_pin_failures", {"p_user_id": uid}).execute()
    except Exception:
        pass

    return True
