import os
import requests
from fastapi import APIRouter, HTTPException, Header, Form
from app.services.auth import verify_supabase_token

router = APIRouter()

ADMIN_EMAIL = "darkmoorltd@gmail.com"


def _admin_headers():
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _require_admin(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    payload = verify_supabase_token(authorization.split(" ", 1)[1])
    email = payload.get("email", "")
    if email != ADMIN_EMAIL:
        raise HTTPException(403, "Admin only")
    return payload


@router.post("/admin/delete-user")
async def admin_delete_user(
    user_id: str = Form(...),
    authorization: str = Header(None),
):
    _require_admin(authorization)

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    h = _admin_headers()

    tables = [
        "user_scans", "user_profiles", "payment_history", "scan_history",
        "user_presence", "farmer_wallets", "farmer_verifications",
        "documents", "document_chunks", "badge_subscriptions",
        "marketplace_listings", "marketplace_orders", "marketplace_cart",
        "marketplace_favorites", "marketplace_reviews",
        "chat_members", "chat_messages", "friendships",
        "gaia_chat_memory", "farmer_memory",
        "loan_applications", "loan_repayments",
        "field_reports", "extension_assignments",
        "insurance_policies", "support_tickets",
        "user_feedback", "user_status",
    ]

    deleted_counts = {}
    for t in tables:
        try:
            r = requests.delete(
                url + "/rest/v1/" + t + "?user_id=eq." + user_id,
                headers=h, timeout=20,
            )
            deleted_counts[t] = r.status_code
        except Exception as e:
            deleted_counts[t] = "err:" + str(e)[:40]

    r = requests.delete(
        url + "/auth/v1/admin/users/" + user_id,
        headers={"apikey": key, "Authorization": "Bearer " + key},
        timeout=30,
    )

    if r.status_code not in (200, 204):
        raise HTTPException(500, "Auth delete failed: " + r.text)

    return {"ok": True, "user_id": user_id, "tables_cleared": deleted_counts}
