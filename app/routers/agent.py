import os
import secrets
import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


class RegisterFarmerBody(BaseModel):
    full_name: str
    phone: str
    email: Optional[str] = None
    password: Optional[str] = None
    state: str
    lga: Optional[str] = None
    crop: Optional[str] = None
    farm_size_acres: Optional[float] = 1.0
    gps_lat: Optional[float] = None
    gps_lng: Optional[float] = None
    notes: Optional[str] = None


def normalize_phone(p: str) -> str:
    p = (p or "").strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("0"):
        return "234" + p[1:]
    if p.startswith("234"):
        return p
    return ("234" + p) if p else ""


async def _get_agent(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            SUPABASE_URL + "/auth/v1/user",
            headers={"apikey": SERVICE_KEY, "Authorization": "Bearer " + token},
        )
        if r.status_code != 200:
            raise HTTPException(401, "Invalid token")
        user = r.json()
        user_id = user.get("id")
        if not user_id:
            raise HTTPException(401, "No user in token")

        r2 = await client.get(
            SUPABASE_URL + "/rest/v1/user_profiles",
            params={"select": "role,agent_code,agent_zone", "user_id": "eq." + user_id},
            headers={"apikey": SERVICE_KEY, "Authorization": "Bearer " + SERVICE_KEY},
        )
        if r2.status_code != 200 or not r2.json():
            raise HTTPException(403, "Profile not found")
        prof = r2.json()[0]
        if prof.get("role") != "agent":
            raise HTTPException(403, "Not an agent account")
        return user_id, prof


@router.post("/agent/register-farmer")
async def register_farmer(body: RegisterFarmerBody, authorization: str = Header(None)):
    if not SERVICE_KEY:
        raise HTTPException(500, "Service key not configured")

    agent_id, _agent_prof = await _get_agent(authorization)

    phone = normalize_phone(body.phone)
    if not phone or len(phone) < 10:
        raise HTTPException(400, "Invalid phone number")

    # Use provided email if present, else phone-based synthetic email
    farmer_email = (body.email or '').strip().lower() if body.email else (phone + "@farmer.gaia")
    if farmer_email and '@' not in farmer_email:
        raise HTTPException(400, "Invalid email format")
    # Agent-supplied password (agent tells the farmer). Fallback to random 8-char.
    if body.password and len(body.password.strip()) >= 6:
        temp_password = body.password.strip()
    else:
        temp_password = secrets.token_urlsafe(6)  # ~8 chars

    async with httpx.AsyncClient(timeout=30) as client:
        # Check by phone
        r = await client.get(
            SUPABASE_URL + "/rest/v1/user_profiles",
            params={"select": "user_id", "phone": "eq." + phone},
            headers={"apikey": SERVICE_KEY, "Authorization": "Bearer " + SERVICE_KEY},
        )
        if r.status_code == 200 and r.json():
            raise HTTPException(409, "A farmer with this phone already exists")

        # Check by email if provided
        if body.email:
            check_email = body.email.strip().lower()
            r = await client.get(
                SUPABASE_URL + "/rest/v1/user_profiles",
                params={"select": "user_id", "email": "eq." + check_email},
                headers={"apikey": SERVICE_KEY, "Authorization": "Bearer " + SERVICE_KEY},
            )
            if r.status_code == 200 and r.json():
                raise HTTPException(409, "A farmer with this email already exists")

        r = await client.post(
            SUPABASE_URL + "/auth/v1/admin/users",
            headers={
                "apikey": SERVICE_KEY,
                "Authorization": "Bearer " + SERVICE_KEY,
                "Content-Type": "application/json",
            },
            json={
                "email": farmer_email,
                "password": temp_password,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": body.full_name,
                    "phone": phone,
                    "registered_by_agent": agent_id,
                },
            },
        )
        if r.status_code not in (200, 201):
            raise HTTPException(500, "Failed to create user: " + r.text[:200])
        new_user = r.json()
        farmer_id = new_user["id"]

        parts = (body.full_name or "").strip().split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        r = await client.post(
            SUPABASE_URL + "/rest/v1/user_profiles",
            headers={
                "apikey": SERVICE_KEY,
                "Authorization": "Bearer " + SERVICE_KEY,
                "Content-Type": "application/json",
                "Prefer": "return=minimal,resolution=merge-duplicates",
            },
            params={"on_conflict": "user_id"},
            json={
                "user_id": farmer_id,
                "email": farmer_email,
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "state": body.state,
                "lga": body.lga,
                "farm_state": body.state,
                "farm_lga": body.lga,
                "primary_crops": body.crop,
                "farm_size_acres": body.farm_size_acres or 1.0,
                "farm_gps_lat": body.gps_lat,
                "farm_gps_lon": body.gps_lng,
                "registered_by_agent_id": agent_id,
                "role": "farmer",
                "verification_status": "pending",
                "account_status": "active",
            },
        )
        if r.status_code not in (200, 201):
            await client.delete(
                SUPABASE_URL + "/auth/v1/admin/users/" + farmer_id,
                headers={"apikey": SERVICE_KEY, "Authorization": "Bearer " + SERVICE_KEY},
            )
            raise HTTPException(500, "Failed to create profile: " + r.text[:200])

        await client.post(
            SUPABASE_URL + "/rest/v1/user_scans",
            headers={
                "apikey": SERVICE_KEY,
                "Authorization": "Bearer " + SERVICE_KEY,
                "Content-Type": "application/json",
            },
            json={"user_id": farmer_id, "scans_remaining": 30, "plan": "free"},
        )

        r = await client.post(
            SUPABASE_URL + "/rest/v1/agent_registrations",
            headers={
                "apikey": SERVICE_KEY,
                "Authorization": "Bearer " + SERVICE_KEY,
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json={
                "agent_id": agent_id,
                "farmer_id": farmer_id,
                "source": "agent_app",
                "gps_lat": body.gps_lat,
                "gps_lng": body.gps_lng,
                "notes": body.notes,
            },
        )
        if r.status_code not in (200, 201):
            print("agent_registrations insert failed:", r.status_code, r.text[:200])

    # Send credentials to farmer via SMS
    try:
        termii_key = os.environ.get("TERMII_API_KEY", "")
        if termii_key:
            sms_body = (
                "GAIA Account Created\n"
                + "Login: " + (body.email if body.email else phone) + "\n"
                + "Password: " + temp_password + "\n"
                + "Change password after first login."
            )
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    "https://api.ng.termii.com/api/sms/send",
                    json={
                        "api_key": termii_key,
                        "to": phone,
                        "from": "GAIA",
                        "sms": sms_body,
                        "type": "plain",
                        "channel": "generic",
                    },
                )
    except Exception as e:
        print("credential SMS failed:", str(e)[:200])

    return {
        "ok": True,
        "farmer_id": farmer_id,
        "email": farmer_email,
        "phone": phone,
        "temp_password": temp_password,
    }



class NotifyVerificationBody(BaseModel):
    verification_id: int


@router.post("/agent/notify-verification")
async def notify_verification(body: NotifyVerificationBody, authorization: str = Header(None)):
    """Called by mobile after agent submits a verification.
    Emails darkmoorltd@gmail.com with farmer details + payment link.
    Falls back to a Termii SMS if RESEND_API_KEY is not set.
    """
    if not SERVICE_KEY:
        raise HTTPException(500, "Service key not configured")

    agent_id, _prof = await _get_agent(authorization)

    async with httpx.AsyncClient(timeout=20) as client:
        # Fetch farmer + verification details
        r = await client.get(
            SUPABASE_URL + "/rest/v1/farmer_verifications",
            params={
                "select": "id,user_id,full_name,phone,state,lga,bvn,nin,created_at",
                "id": "eq." + str(body.verification_id),
            },
            headers={"apikey": SERVICE_KEY, "Authorization": "Bearer " + SERVICE_KEY},
        )
        if r.status_code != 200 or not r.json():
            raise HTTPException(404, "Verification not found")
        v = r.json()[0]

        # Fetch agent email
        r2 = await client.get(
            SUPABASE_URL + "/auth/v1/admin/users/" + agent_id,
            headers={"apikey": SERVICE_KEY, "Authorization": "Bearer " + SERVICE_KEY},
        )
        agent_email = ""
        if r2.status_code == 200:
            agent_email = r2.json().get("email", "")

        # Payment link — one-click farmer-facing page
        payment_url = "https://gaia-api-xuly.onrender.com/verify/pay/" + str(body.verification_id)

        # Log the notification in Supabase so admin sees it in-app
        try:
            await client.post(
                SUPABASE_URL + "/rest/v1/admin_notifications",
                headers={
                    "apikey": SERVICE_KEY,
                    "Authorization": "Bearer " + SERVICE_KEY,
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={
                    "kind": "verification_payment",
                    "farmer_id": v.get("user_id"),
                    "verification_id": body.verification_id,
                    "agent_id": agent_id,
                    "title": "Verification submitted: " + (v.get("full_name") or "Farmer"),
                    "body": "Fee N2,000 pending. Forward link to farmer: " + payment_url,
                    "meta": {"phone": v.get("phone"), "state": v.get("state"), "payment_url": payment_url},
                },
            )
        except Exception as e:
            print("admin_notifications insert failed:", str(e)[:200])

        # Send email via Resend if configured
        resend_key = os.environ.get("RESEND_API_KEY", "")
        email_sent = False
        if resend_key:
            try:
                html = (
                    "<h2>New Verification — Payment Pending</h2>"
                    "<p><b>Farmer:</b> " + str(v.get("full_name") or "Unknown") + "</p>"
                    "<p><b>Phone:</b> " + str(v.get("phone") or "-") + "</p>"
                    "<p><b>State:</b> " + str(v.get("state") or "-") + " / " + str(v.get("lga") or "-") + "</p>"
                    "<p><b>BVN:</b> " + str(v.get("bvn") or "-") + "</p>"
                    "<p><b>NIN:</b> " + str(v.get("nin") or "-") + "</p>"
                    "<p><b>Agent:</b> " + agent_email + "</p>"
                    "<p><b>Payment link (forward to farmer):</b><br>"
                    "<a href='" + payment_url + "'>" + payment_url + "</a></p>"
                    "<p><b>Fee:</b> N2,000</p>"
                )
                rr = await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": "Bearer " + resend_key, "Content-Type": "application/json"},
                    json={
                        "from": "GAIA <noreply@darkmoor.ng>",
                        "to": ["darkmoorltd@gmail.com"],
                        "subject": "GAIA Verification: " + str(v.get("full_name") or "New farmer"),
                        "html": html,
                    },
                )
                email_sent = rr.status_code in (200, 201)
            except Exception as e:
                print("resend failed:", str(e)[:200])

        # Fallback SMS to admin
        if not email_sent:
            try:
                termii_key = os.environ.get("TERMII_API_KEY", "")
                admin_phone = os.environ.get("ADMIN_PHONE", "")
                if termii_key and admin_phone:
                    await client.post(
                        "https://api.ng.termii.com/api/sms/send",
                        json={
                            "api_key": termii_key,
                            "to": admin_phone,
                            "from": "GAIA",
                            "sms": "New verify: " + str(v.get("full_name") or "?") + " / " + str(v.get("phone") or "?") + " / pay: " + payment_url,
                            "type": "plain",
                            "channel": "generic",
                        },
                    )
            except Exception as e:
                print("admin SMS failed:", str(e)[:200])

        return {
            "ok": True,
            "email_sent": email_sent,
            "payment_url": payment_url,
            "verification_id": body.verification_id,
        }



class SubmitVerificationBody(BaseModel):
    farmer_id: str
    full_name: str
    phone: str
    state: str
    lga: Optional[str] = None
    address: Optional[str] = None
    bvn: Optional[str] = None
    nin: Optional[str] = None
    id_type: str = "National ID Card"
    id_number: Optional[str] = None
    crop: Optional[str] = None
    farm_state: Optional[str] = None
    farm_lga: Optional[str] = None
    farm_size: Optional[str] = None
    id_image_url: Optional[str] = None
    selfie_url: Optional[str] = None
    gps_lat: Optional[float] = None
    gps_lng: Optional[float] = None
    payment_reference: str


@router.post("/agent/submit-verification")
async def submit_verification(body: SubmitVerificationBody, authorization: str = Header(None)):
    """End-to-end: verify Paystack ref -> create verification row (paid) -> notify admin."""
    if not SERVICE_KEY:
        raise HTTPException(500, "Service key not configured")

    agent_id, _prof = await _get_agent(authorization)

    # 1) Verify Paystack reference
    paystack_secret = os.environ.get("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret:
        raise HTTPException(500, "Paystack secret not configured")

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://api.paystack.co/transaction/verify/" + body.payment_reference,
            headers={"Authorization": "Bearer " + paystack_secret},
        )
        if r.status_code != 200:
            raise HTTPException(400, "Could not verify payment: " + r.text[:200])
        pdata = r.json().get("data", {})
        if pdata.get("status") != "success":
            raise HTTPException(400, "Payment not successful")
        amount_naira = pdata.get("amount", 0) / 100
        if amount_naira < 2000:
            raise HTTPException(400, "Insufficient payment amount (need N2000, got N" + str(amount_naira) + ")")

        # 2) Create the verification row via SQL RPC
        r2 = await client.post(
            SUPABASE_URL + "/rest/v1/rpc/agent_submit_verification",
            headers={
                "apikey": SERVICE_KEY,
                "Authorization": "Bearer " + SERVICE_KEY,
                "Content-Type": "application/json",
            },
            json={
                "p_farmer_id": body.farmer_id,
                "p_full_name": body.full_name,
                "p_phone": body.phone,
                "p_state": body.state,
                "p_lga": body.lga,
                "p_address": body.address,
                "p_bvn": body.bvn,
                "p_nin": body.nin,
                "p_id_type": body.id_type,
                "p_id_number": body.id_number,
                "p_crop": body.crop,
                "p_farm_state": body.farm_state or body.state,
                "p_farm_lga": body.farm_lga or body.lga,
                "p_farm_size": body.farm_size,
                "p_id_image_url": body.id_image_url,
                "p_selfie_url": body.selfie_url,
                "p_gps_lat": body.gps_lat,
                "p_gps_lng": body.gps_lng,
                "p_payment_reference": body.payment_reference,
                "p_payment_status": "paid",
            },
        )
        if r2.status_code != 200:
            raise HTTPException(500, "Verification RPC failed: " + r2.text[:200])
        verif = r2.json()

        # 3) Create admin notification
        payment_url = "https://gaia-api-xuly.onrender.com/verify/pay/" + str(verif.get("verification_id"))
        try:
            await client.post(
                SUPABASE_URL + "/rest/v1/admin_notifications",
                headers={
                    "apikey": SERVICE_KEY,
                    "Authorization": "Bearer " + SERVICE_KEY,
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={
                    "kind": "verification_paid",
                    "farmer_id": body.farmer_id,
                    "verification_id": verif.get("verification_id"),
                    "agent_id": agent_id,
                    "title": "PAID verification: " + body.full_name,
                    "body": "N2,000 received. Ready for admin review.",
                    "meta": {"phone": body.phone, "state": body.state, "amount": amount_naira},
                },
            )
        except Exception as e:
            print("admin_notifications insert failed:", str(e)[:200])

        return {
            "ok": True,
            "verification_id": verif.get("verification_id"),
            "agent_verification_id": verif.get("agent_verification_id"),
            "payment_amount": amount_naira,
            "status": verif.get("status"),
        }
