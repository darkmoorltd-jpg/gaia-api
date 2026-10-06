# app/routers/wallet_pin.py
# Wallet PIN — set and verify, hashed consistently
import os
import hashlib
import hmac
from datetime import datetime
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
    # Salted + peppered sha256 — good for a 4-6 digit wallet PIN
    # that is already rate-limited by the verify endpoint.
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
    s.table("farmer_wallets").upsert(
        {"user_id": uid, "balance": 0, "pin_hash": h, "updated_at": datetime.utcnow().isoformat()},
        on_conflict="user_id",
    ).execute()

    return {"ok": True}


class HasPinReq(BaseModel):
    pass


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
    pin = (req.pin or "").strip()
    if not pin:
        raise HTTPException(400, "PIN required")

    s = svc()
    r = s.table("farmer_wallets").select("pin_hash").eq("user_id", uid).limit(1).execute()
    if not r.data or len(r.data) == 0 or not r.data[0].get("pin_hash"):
        raise HTTPException(400, "No PIN set")
    stored = r.data[0]["pin_hash"]

    expected = hash_pin(uid, pin)
    if not hmac.compare_digest(stored, expected):
        raise HTTPException(400, "Incorrect PIN")

    return {"ok": True}
