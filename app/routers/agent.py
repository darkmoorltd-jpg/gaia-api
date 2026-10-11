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

    farmer_email = phone + "@farmer.gaia"
    temp_password = secrets.token_urlsafe(12)

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            SUPABASE_URL + "/rest/v1/user_profiles",
            params={"select": "user_id", "phone": "eq." + phone},
            headers={"apikey": SERVICE_KEY, "Authorization": "Bearer " + SERVICE_KEY},
        )
        if r.status_code == 200 and r.json():
            raise HTTPException(409, "A farmer with this phone already exists")

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
                "Prefer": "return=minimal",
            },
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

    return {
        "ok": True,
        "farmer_id": farmer_id,
        "email": farmer_email,
        "phone": phone,
        "temp_password": temp_password,
    }
