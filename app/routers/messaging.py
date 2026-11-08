from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.utils import robase


router = APIRouter(prefix="/messaging", tags=["messaging"])


class SMSRequest(BaseModel):
    phone_number: str
    message: str


@router.get("/health")
def messaging_health():
    return {
        "ok": True,
        "robase_key_set": bool(robase.ROBASE_API_KEY),
        "endpoint": robase.ROBASE_ENDPOINT,
        "base_url": robase.ROBASE_BASE_URL,
    }


@router.post("/sms/send")
def send_sms(req: SMSRequest):
    result = robase.send_sms(req.phone_number, req.message)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "SMS failed"))
    return {"ok": True, "message_id": result.get("message_id")}
