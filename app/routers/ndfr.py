import os
import requests
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from supabase import create_client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
DOJAH_APP_ID = os.environ.get("DOJAH_APP_ID")
DOJAH_API_KEY = os.environ.get("DOJAH_API_KEY")

# Partner API Key (Give this to BOA/Banks)
PARTNER_API_KEY = "GAIA_BOA_PARTNER_2026" 

class NINRequest(BaseModel):
    nin: str
    first_name: str
    last_name: str

@router.post("/verify-nin")
async def verify_nin(req: NINRequest):
    """Called by the Agent App to verify a NIN via Dojah"""
    if not DOJAH_APP_ID:
        raise HTTPException(500, "Dojah API not configured")

    url = "https://api.dojah.io/api/v1/kyc/nin"
    headers = {
        "AppId": DOJAH_APP_ID,
        "Authorization": DOJAH_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {"nin": req.nin}

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        data = res.json()

        if res.status_code == 200 and data.get("entity"):
            entity = data["entity"]
            return {
                "verified": True,
                "first_name": entity.get("firstname"),
                "last_name": entity.get("surname"),
                "dob": entity.get("birthdate")
            }
        return {"verified": False, "error": "NIN not found"}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/partner/ndfr-lookup/{nin}")
async def partner_lookup(nin: str, x_partner_key: str = Header(None)):
    """Secure API for Banks/BOA to query a farmer's registry data."""
    if x_partner_key != PARTNER_API_KEY:
        raise HTTPException(401, "Invalid Partner API Key")

    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    res = supabase.table("farmer_registry").select("*").eq("nin", nin).execute()

    if not res.data:
        raise HTTPException(404, "Farmer not found in NDFR")

    farmer = res.data[0]

    # Return only fields the bank needs (Privacy by Design)
    return {
        "gaia_farmer_id": farmer.get("unique_farmer_id"),
        "state": farmer.get("state"),
        "lga": farmer.get("lga"),
        "primary_crop": farmer.get("crop"),
        "farm_size_acres": farmer.get("farm_size_acres"),
        "has_insurance": farmer.get("has_insurance", False),
        "credit_access": farmer.get("has_credit_access", False),
        "kyc_verified": farmer.get("nin_verified", False)
    }