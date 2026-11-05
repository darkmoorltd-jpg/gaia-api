import os
import hashlib
import secrets
import requests
from datetime import datetime
from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel
from supabase import create_client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
TERMII_API_KEY = os.environ.get("TERMII_API_KEY", "tlv_X4pCCI6rCP7B8ovuHztPQhtVXo91Y0VQTF-pB8jF9xw")
CONSENT_VERSION = "2026-01-v1"


def _sb():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _hash(otp, salt):
    return hashlib.sha256((otp + salt).encode()).hexdigest()


def _send_sms(phone, code):
    if not phone:
        return False
    if phone.startswith("0"):
        phone = "234" + phone[1:]
    elif not phone.startswith("234"):
        phone = "234" + phone
    payload = {
        "api_key": TERMII_API_KEY, "to": phone, "from": "GAIA",
        "sms": "Your GAIA claim code is " + code + ". Expires in 10 minutes.",
        "type": "plain", "channel": "generic",
    }
    try:
        r = requests.post("https://api.ng.termii.com/api/sms/send", json=payload, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def _ip(request):
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _auth(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    tok = authorization.split(" ", 1)[1]
    try:
        r = _sb().auth.get_user(tok)
        if not r.user:
            raise HTTPException(401, "Invalid token")
        return r.user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid token")


def _mask(phone):
    if not phone or len(phone) < 6:
        return "****"
    return phone[:4] + "****" + phone[-3:]


def _size_label(acres):
    if not acres:
        return "unknown"
    ha = float(acres) * 0.4047
    if ha < 1: return "~subsistence (<1 ha)"
    if ha < 2: return "~small (<2 ha)"
    if ha < 10: return "~medium (2-10 ha)"
    if ha < 100: return "~large (10-100 ha)"
    return "~commercial (>100 ha)"


class PreviewReq(BaseModel):
    nin: str


class SendOTPReq(BaseModel):
    claim_token: str


class ConfirmReq(BaseModel):
    claim_token: str
    otp: str
    consent: bool
    consent_text: str = ""


@router.post("/ndfr/claim/preview")
async def preview(req: PreviewReq, request: Request, authorization: str = Header(None)):
    user = _auth(authorization)
    sb = _sb()
    ip = _ip(request)

    if not req.nin or len(req.nin) != 11 or not req.nin.isdigit():
        try:
            sb.table("claim_attempts").insert({"user_id": user.id, "nin_tried": req.nin, "result": "invalid_nin", "ip": ip}).execute()
        except Exception:
            pass
        raise HTTPException(400, "NIN must be 11 digits")

    rate = sb.rpc("claim_rate_ok", {"p_user_id": user.id}).execute()
    if not rate.data:
        raise HTTPException(429, "Too many attempts. Try again in an hour.")

    r = sb.table("state_import_staging").select("*").eq("nin", req.nin).is_("claimed_by", "null").limit(1).execute()
    if not r.data:
        try:
            sb.table("claim_attempts").insert({"user_id": user.id, "nin_tried": req.nin, "result": "preview_not_found", "ip": ip}).execute()
        except Exception:
            pass
        raise HTTPException(404, "No record found for this NIN")

    stg = r.data[0]
    phone = stg.get("phone", "") or ""

    sess = sb.table("claim_sessions").insert({
        "user_id": user.id,
        "staged_id": stg["id"],
        "phone_last4": phone[-4:] if phone else "",
    }).execute()

    try:
        sb.table("claim_attempts").insert({"user_id": user.id, "nin_tried": req.nin, "result": "preview_found", "ip": ip}).execute()
    except Exception:
        pass

    return {
        "found": True,
        "claim_token": sess.data[0]["token"],
        "preview": {
            "name": stg.get("full_name") or "Unknown",
            "state": stg.get("state_name") or "—",
            "lga": stg.get("lga") or "—",
            "crop": stg.get("crop") or "—",
            "farm_size_label": _size_label(stg.get("farm_size_acres")),
        },
        "otp_target": _mask(phone),
    }


@router.post("/ndfr/claim/send-otp")
async def send_otp(req: SendOTPReq, authorization: str = Header(None)):
    user = _auth(authorization)
    sb = _sb()

    sess = sb.table("claim_sessions").select("*").eq("token", req.claim_token).eq("user_id", user.id).execute()
    if not sess.data:
        raise HTTPException(404, "Session not found")

    s = sess.data[0]
    if s["expires_at"] and s["expires_at"] < datetime.utcnow().isoformat():
        raise HTTPException(410, "Session expired")

    stg = sb.table("state_import_staging").select("phone").eq("id", s["staged_id"]).execute()
    phone = (stg.data[0].get("phone") or "") if stg.data else ""
    if not phone:
        raise HTTPException(400, "No phone number on record")

    code = str(secrets.randbelow(900000) + 100000)
    salt = secrets.token_hex(8)
    combined = salt + ":" + _hash(code, salt)

    sb.table("claim_otp").insert({"session_token": req.claim_token, "otp_hash": combined}).execute()
    sb.table("claim_sessions").update({"otp_sent_at": datetime.utcnow().isoformat()}).eq("token", req.claim_token).execute()

    if not _send_sms(phone, code):
        raise HTTPException(500, "SMS send failed")

    return {"sent": True, "expires_in_seconds": 600}


@router.post("/ndfr/claim/confirm")
async def confirm(req: ConfirmReq, request: Request, authorization: str = Header(None)):
    user = _auth(authorization)
    sb = _sb()
    ip = _ip(request)

    if not req.consent:
        raise HTTPException(400, "Consent required")
    if not req.otp or len(req.otp) != 6 or not req.otp.isdigit():
        raise HTTPException(400, "OTP must be 6 digits")

    otp_rec = sb.table("claim_otp").select("*").eq("session_token", req.claim_token).is_("verified_at", "null").order("created_at", desc=True).limit(1).execute()
    if not otp_rec.data:
        raise HTTPException(404, "No active OTP")

    r = otp_rec.data[0]
    if r["attempts"] >= 5:
        raise HTTPException(429, "Too many OTP attempts")

    if r["expires_at"] and r["expires_at"] < datetime.utcnow().isoformat():
        raise HTTPException(410, "OTP expired")

    try:
        salt, stored = r["otp_hash"].split(":", 1)
    except Exception:
        raise HTTPException(500, "Malformed OTP record")

    if _hash(req.otp, salt) != stored:
        sb.table("claim_otp").update({"attempts": r["attempts"] + 1}).eq("id", r["id"]).execute()
        raise HTTPException(401, "Incorrect OTP")

    sb.table("claim_otp").update({"verified_at": datetime.utcnow().isoformat(), "attempts": r["attempts"] + 1}).eq("id", r["id"]).execute()
    sb.table("claim_sessions").update({"otp_verified_at": datetime.utcnow().isoformat()}).eq("token", req.claim_token).execute()

    consent_text = (req.consent_text or "Consent given")[:500] + " [v:" + CONSENT_VERSION + "]"

    res = sb.rpc("execute_claim", {
        "p_user_id": user.id,
        "p_session_token": req.claim_token,
        "p_consent_text": consent_text,
        "p_ip": ip,
    }).execute()

    if not res.data or not res.data.get("success"):
        raise HTTPException(400, (res.data or {}).get("error", "Claim failed"))

    return res.data
