import os
import requests
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from supabase import create_client
from typing import Optional

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
DOJAH_APP_ID = os.environ.get("DOJAH_APP_ID")
DOJAH_API_KEY = os.environ.get("DOJAH_API_KEY")
SENTINEL_CLIENT_ID = os.environ.get("SENTINEL_CLIENT_ID", "86ed44fa-793b-47da-973b-345a83ae18c0")
SENTINEL_CLIENT_SECRET = os.environ.get("SENTINEL_CLIENT_SECRET", "qYTQXnQFpgstJSrAulJ6NREflI2m2eCN")


def _supabase():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _log_usage(partner_key_id: str, endpoint: str, nin: Optional[str], status: int):
    try:
        _supabase().table("partner_api_usage").insert({
            "partner_key_id": partner_key_id,
            "endpoint": endpoint,
            "queried_nin": nin,
            "status_code": status,
        }).execute()
    except Exception:
        pass


class NINRequest(BaseModel):
    nin: str
    first_name: str = ""
    last_name: str = ""


@router.post("/verify-nin")
async def verify_nin(req: NINRequest):
    if not DOJAH_APP_ID:
        raise HTTPException(500, "Dojah API not configured")
    url = "https://api.dojah.io/api/v1/kyc/nin"
    headers = {
        "AppId": DOJAH_APP_ID,
        "Authorization": DOJAH_API_KEY,
        "Content-Type": "application/json",
    }
    try:
        res = requests.post(url, headers=headers, json={"nin": req.nin}, timeout=15)
        data = res.json()
        if res.status_code == 200 and data.get("entity"):
            e = data["entity"]
            return {"verified": True, "first_name": e.get("firstname"),
                    "last_name": e.get("surname"), "dob": e.get("birthdate")}
        return {"verified": False, "error": "NIN not found"}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/partner/usage")
async def partner_usage(x_partner_key: str = Header(None)):
    if not x_partner_key:
        raise HTTPException(401, "Missing X-Partner-Key header")
    try:
        r = _supabase().rpc("partner_usage_summary", {"p_api_key": x_partner_key}).execute()
        if not r.data:
            raise HTTPException(401, "Invalid partner key")
        return r.data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/partner/ndfr-lookup/{nin}")
async def partner_lookup(nin: str, x_partner_key: str = Header(None)):
    if not x_partner_key:
        raise HTTPException(401, "Missing X-Partner-Key header")

    sb = _supabase()
    key_row = sb.table("partner_api_keys").select("*").eq("api_key", x_partner_key).eq("status", "active").execute()
    if not key_row.data:
        raise HTTPException(401, "Invalid partner key")

    partner = key_row.data[0]
    key_id = partner["id"]
    month_start = __import__("datetime").datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    used = sb.table("partner_api_usage").select("id", count="exact").eq("partner_key_id", key_id).gte("created_at", month_start).execute()
    used_count = used.count if used.count is not None else 0
    if used_count >= partner["monthly_quota"]:
        _log_usage(key_id, "lookup", nin, 429)
        raise HTTPException(429, f"Monthly quota exceeded ({partner['monthly_quota']})")

    farmer_rows = sb.table("farmer_registry").select("*").eq("nin", nin).execute()
    if not farmer_rows.data:
        _log_usage(key_id, "lookup", nin, 404)
        raise HTTPException(404, "Farmer not found in NDFR")

    farmer = farmer_rows.data[0]
    _log_usage(key_id, "lookup", nin, 200)

    return {
        "gaia_farmer_id": farmer.get("unique_farmer_id"),
        "state": farmer.get("state"),
        "lga": farmer.get("lga"),
        "primary_crop": farmer.get("crop"),
        "farm_size_acres": farmer.get("farm_size_acres"),
        "has_insurance": farmer.get("has_insurance", False),
        "credit_access": farmer.get("has_credit_access", False),
        "kyc_verified": farmer.get("nin_verified", False),
        "ndvi_validated": farmer.get("ndvi_validated", False),
        "ndvi_value": farmer.get("ndvi_value"),
    }


class PolygonValidateRequest(BaseModel):
    farmer_id: str
    polygon_coords: list  # list of [lon, lat] pairs


def _sentinel_token():
    import base64
    creds = base64.b64encode(f"{SENTINEL_CLIENT_ID}:{SENTINEL_CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    if r.status_code != 200:
        return None
    return r.json().get("access_token")


@router.post("/validate-polygon")
async def validate_polygon(req: PolygonValidateRequest):
    if not req.polygon_coords or len(req.polygon_coords) < 3:
        raise HTTPException(400, "Polygon must have at least 3 points")

    lons = [p[0] for p in req.polygon_coords]
    lats = [p[1] for p in req.polygon_coords]
    bbox = [min(lons), min(lats), max(lons), max(lats)]

    token = _sentinel_token()
    if not token:
        raise HTTPException(500, "Sentinel auth failed")

    evalscript = """//VERSION=3
function setup() { return { input: ["B04","B08"], output: { bands: 1, sampleType: "FLOAT32" } }; }
function evaluatePixel(s) {
  let ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 0.0001);
  return [ndvi];
}"""

    payload = {
        "input": {
            "bounds": {"bbox": bbox, "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}},
            "data": [{"type": "sentinel-2-l1c",
                      "dataFilter": {"maxCloudCoverage": 40,
                                     "timeRange": {"from": "2026-01-01T00:00:00Z", "to": "2026-12-31T23:59:59Z"}}}]
        },
        "output": {"width": 128, "height": 128,
                   "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]},
        "evalscript": evalscript,
    }

    try:
        r = requests.post(
            "https://sh.dataspace.copernicus.eu/api/v1/process",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=45,
        )
    except Exception as e:
        return {"validated": False,
                "reason": "Sentinel network error: " + str(e)[:120],
                "farmer_id": req.farmer_id, "retry": True}

    if r.status_code != 200:
        body = r.text[:200] if r.text else ""
        return {"validated": False,
                "reason": f"Sentinel HTTP {r.status_code}: {body}",
                "farmer_id": req.farmer_id,
                "retry": True,
                "hint": "Verify Sentinel Hub account has access to sentinel-2-l1c"}

    # ---- Parse the TIFF ----
    try:
        import numpy as np
        import io
        import math
        from PIL import Image
        img = Image.open(io.BytesIO(r.content))
        arr = np.array(img, dtype=np.float32)
        # Sanity: shape and value range
        shape_str = str(arr.shape)
        finite_count = int(np.isfinite(arr).sum())
        total_pixels = int(arr.size)
        if finite_count == 0:
            return {"validated": False,
                    "reason": f"TIFF has no finite pixels (shape={shape_str}, total={total_pixels})",
                    "farmer_id": req.farmer_id, "retry": True}
        mean_ndvi = float(np.nanmean(arr))
        if not math.isfinite(mean_ndvi):
            return {"validated": False,
                    "reason": f"NDVI is NaN/Inf (shape={shape_str}, finite={finite_count}/{total_pixels})",
                    "farmer_id": req.farmer_id, "retry": True}
        mean_ndvi = round(mean_ndvi, 4)
        is_valid = mean_ndvi > 0.2
    except Exception as e:
        return {"validated": False,
                "reason": "TIFF parse failed: " + str(e)[:150],
                "farmer_id": req.farmer_id, "retry": True}

    # ---- Write to DB (isolated so we know if write fails) ----
    try:
        _supabase().table("farmer_registry").update({
            "ndvi_validated": is_valid,
            "ndvi_value": mean_ndvi,
            "polygon_validated_at": __import__("datetime").datetime.utcnow().isoformat(),
        }).eq("user_id", req.farmer_id).execute()
    except Exception as e:
        return {"validated": False,
                "reason": "DB write failed: " + str(e)[:150],
                "mean_ndvi": mean_ndvi,
                "farmer_id": req.farmer_id, "retry": True}

    return {"validated": True, "is_vegetation": is_valid,
            "mean_ndvi": mean_ndvi,
            "farmer_id": req.farmer_id}
