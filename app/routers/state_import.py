import os
import io
import uuid
import datetime
from fastapi import APIRouter, HTTPException, Header, UploadFile, File, Form
from supabase import create_client
import pandas as pd

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

ADMIN_EMAIL = "darkmoorltd@gmail.com"


def _sb():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _require_admin(authorization: str):
    """Verify the caller is the admin by checking their JWT against Supabase auth."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization.split(" ", 1)[1]
    try:
        r = _sb().auth.get_user(token)
        if not r.user or r.user.email != ADMIN_EMAIL:
            raise HTTPException(403, "Admin only")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid token")


# Column aliases: what a state CSV might call things → what we expect
ALIASES = {
    "fullname": "full_name", "name": "full_name", "farmer name": "full_name",
    "farmer_name": "full_name", "phone number": "phone", "phone_number": "phone",
    "mobile": "phone", "nin": "nin", "national id": "nin", "national_id": "nin",
    "state": "state", "state_name": "state", "lga": "lga", "ward": "ward",
    "crop": "crop", "primary crop": "crop", "primary_crop": "crop",
    "farm size": "farm_size_acres", "farm_size": "farm_size_acres",
    "farm_size_acres": "farm_size_acres", "acres": "farm_size_acres",
    "gps_lat": "gps_lat", "gps_lon": "gps_lon", "latitude": "gps_lat", "longitude": "gps_lon",
    "email": "email", "gender": "gender",
}


@router.post("/admin/state-import")
async def state_import(
    state_name: str = Form(...),
    source_name: str = Form(...),
    file: UploadFile = File(...),
    authorization: str = Header(None),
):
    """Ingest a CSV of farmer records from a state registry. Dedups by NIN or phone."""
    _require_admin(authorization)
    sb = _sb()

    # Create import record
    imp_id = str(uuid.uuid4())
    sb.table("state_imports").insert({
        "id": imp_id, "state_name": state_name, "source_name": source_name,
        "file_name": file.filename, "status": "running",
    }).execute()

    try:
        raw = await file.read()
        if file.filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw))
        elif file.filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw))
        else:
            raise HTTPException(400, "Only CSV or Excel supported")
    except HTTPException:
        raise
    except Exception as e:
        sb.table("state_imports").update({"status": "failed", "notes": str(e)[:200]}).eq("id", imp_id).execute()
        raise HTTPException(400, f"Could not parse file: {e}")

    # Normalize columns
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={k: v for k, v in ALIASES.items() if k in df.columns})

    total = len(df)
    inserted = skipped = failed = 0

    for _, row in df.iterrows():
        nin = str(row.get("nin", "")).strip() if pd.notna(row.get("nin", "")) else ""
        phone = str(row.get("phone", "")).strip() if pd.notna(row.get("phone", "")) else ""

        if not nin and not phone:
            failed += 1
            continue

        # Dedup
        existing = None
        if nin:
            existing = sb.table("farmer_registry").select("user_id").eq("nin", nin).execute()
        if not existing or not existing.data:
            if phone:
                existing = sb.table("farmer_registry").select("user_id").eq("phone", phone).execute()

        if existing and existing.data:
            skipped += 1
            continue

        # Not found: create a "pending claim" row (no auth user yet)
        # We store the NIN/phone/state so the farmer can later verify + claim
        try:
            sb.table("state_import_staging").insert({
                "import_id": imp_id,
                "state_name": state_name,
                "source_name": source_name,
                "full_name": str(row.get("full_name", "")) if pd.notna(row.get("full_name", "")) else None,
                "nin": nin or None,
                "phone": phone or None,
                "lga": str(row.get("lga", "")) if pd.notna(row.get("lga", "")) else None,
                "ward": str(row.get("ward", "")) if pd.notna(row.get("ward", "")) else None,
                "crop": str(row.get("crop", "")) if pd.notna(row.get("crop", "")) else None,
                "farm_size_acres": float(row.get("farm_size_acres", 0)) if pd.notna(row.get("farm_size_acres", 0)) else None,
                "gps_lat": float(row.get("gps_lat", 0)) if pd.notna(row.get("gps_lat", 0)) else None,
                "gps_lon": float(row.get("gps_lon", 0)) if pd.notna(row.get("gps_lon", 0)) else None,
            }).execute()
            inserted += 1
        except Exception:
            failed += 1

    sb.table("state_imports").update({
        "total_rows": total, "inserted": inserted,
        "skipped_duplicates": skipped, "failed": failed,
        "status": "completed",
        "finished_at": datetime.datetime.utcnow().isoformat(),
    }).eq("id", imp_id).execute()

    return {"import_id": imp_id, "total": total, "inserted": inserted,
            "skipped_duplicates": skipped, "failed": failed}


@router.get("/admin/state-imports")
async def list_imports(authorization: str = Header(None)):
    _require_admin(authorization)
    sb = _sb()
    r = sb.table("state_imports").select("*").order("started_at", desc=True).limit(50).execute()
    return {"imports": r.data or []}
