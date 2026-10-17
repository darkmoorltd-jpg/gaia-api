import os
from fastapi import APIRouter, HTTPException, Header, Form
from supabase import create_client

router = APIRouter()
ADMIN_EMAIL = "darkmoorltd@gmail.com"


def _supa():
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )


def _require_admin(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    try:
        from app.services.auth import verify_supabase_token
        payload = verify_supabase_token(authorization.split(" ", 1)[1])
    except Exception as e:
        raise HTTPException(401, f"Bad token: {e}")
    if payload.get("email") != ADMIN_EMAIL:
        raise HTTPException(403, "Admin only")


@router.post("/admin/send-push")
async def admin_send_push(
    title: str = Form(...),
    body: str = Form(...),
    segment: str = Form("all"),
    screen: str = Form("/"),
    drain_now: bool = Form(False),
    authorization: str = Header(None),
):
    _require_admin(authorization)
    sb = _supa()

    try:
        r = sb.rpc("admin_broadcast", {
            "p_title": title,
            "p_body": body,
            "p_segment": segment,
        }).execute()
        count = r.data if isinstance(r.data, int) else 0
    except Exception as e:
        raise HTTPException(500, f"Queue failed: {e}")

    drained = None
    if drain_now:
        try:
            import httpx
            url = os.environ.get("SELF_URL", "http://localhost:8000") + "/cron/drain-queue"
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.post(url, headers={"X-Cron-Secret": os.environ.get("CRON_SECRET", "")})
                if resp.status_code == 200:
                    drained = resp.json()
        except Exception:
            pass

    return {"ok": True, "queued": count, "drained": drained}
