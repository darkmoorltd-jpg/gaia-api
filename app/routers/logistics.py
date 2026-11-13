from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from supabase import create_client
import os

router = APIRouter()
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

class BatchCreate(BaseModel):
    crop_name: str
    quantity_kg: float
    quality_grade: str
    location: str

@router.post("/logistics/batches")
async def create_batch(batch: BatchCreate, authorization: str = Header(None)):
    """Create a new harvest batch (Farmer)"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization.split(" ")[1]
    
    # Verify user
    try:
        user = supabase.auth.get_user(token)
        farmer_id = user.user.id
    except:
        raise HTTPException(401, "Invalid token")

    import random, string
    code = "GAI-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    
    res = supabase.table("batches").insert({
        "farmer_id": farmer_id,
        "batch_code": code,
        "crop_name": batch.crop_name,
        "quantity_kg": batch.quantity_kg,
        "quality_grade": batch.quality_grade,
        "location": batch.location,
        "status": "active"
    }).execute()
    
    return {"batch": res.data[0]}

@router.post("/logistics/{batch_code}/dispatch")
async def dispatch_batch(batch_code: str, driver_id: str, authorization: str = Header(None)):
    """Driver accepts cargo"""
    res = supabase.table("batches").update({"status": "in_transit"}).eq("batch_code", batch_code).execute()
    if not res.data:
        raise HTTPException(404, "Batch not found")
    
    supabase.table("batch_movements").insert({
        "batch_id": res.data[0]["id"],
        "driver_id": driver_id,
        "from_location": res.data[0]["location"],
        "to_location": "In Transit",
        "status": "in_transit"
    }).execute()
    
    return {"status": "dispatched", "batch": res.data[0]}

@router.post("/logistics/{batch_code}/receive")
async def receive_batch(batch_code: str, received_kg: float, authorization: str = Header(None)):
    """Warehouse receives cargo"""
    res = supabase.table("batches").select("*").eq("batch_code", batch_code).execute()
    if not res.data:
        raise HTTPException(404, "Batch not found")
    
    batch = res.data[0]
    loss = batch["quantity_kg"] - received_kg
    
    supabase.table("batches").update({"status": "in_storage"}).eq("id", batch["id"]).execute()
    supabase.table("batch_movements").insert({
        "batch_id": batch["id"],
        "from_location": "In Transit",
        "to_location": "Warehouse",
        "quantity_kg": received_kg,
        "loss_kg": max(0, loss),
        "status": "received"
    }).execute()
    
    return {"status": "received", "loss_kg": loss}
