import os
from datetime import datetime
import httpx
from fastapi import APIRouter, HTTPException, Header
from supabase import create_client

router = APIRouter()

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _load_env_sources():
    """Try every possible source of env vars. Cache on module level."""
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE

    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    source = "os.environ"

    if not url or not key:
        # try .env file
        try:
            from dotenv import load_dotenv
            from pathlib import Path as _P
            for p in [_P(".env"), _P("/app/.env"), _P(__file__).resolve().parents[2] / ".env"]:
                if p.exists():
                    load_dotenv(p)
                    break
            url = url or os.environ.get("SUPABASE_URL", "")
            key = key or os.environ.get("SUPABASE_SERVICE_KEY", "")
            if url and key:
                source = ".env"
        except ImportError:
            pass

    if not url or not key:
        # try pydantic settings
        for mod in ["app.config", "app.settings", "app.core.config"]:
            try:
                m = __import__(mod, fromlist=["settings"])
                s = getattr(m, "settings", None)
                if s:
                    url = url or getattr(s, "SUPABASE_URL", "") or getattr(s, "supabase_url", "")
                    key = key or getattr(s, "SUPABASE_SERVICE_KEY", "") or getattr(s, "supabase_service_key", "")
                    if url and key:
                        source = mod
                        break
            except (ImportError, AttributeError):
                continue

    _ENV_CACHE = {"url": url, "key": key, "source": source}
    return _ENV_CACHE


_ENV_CACHE = None


def _supa():
    cfg = _load_env_sources()
    if not cfg["url"] or not cfg["key"]:
        keys = sorted([k for k in os.environ.keys() if "SUPA" in k.upper() or "SERVICE" in k.upper()])
        raise RuntimeError(
            f"missing env: url={bool(cfg['url'])} key={bool(cfg['key'])} "
            f"source={cfg['source']} available_keys={keys}"
        )
    return create_client(cfg["url"], cfg["key"])



def _require_cron(secret: str | None):
    expected = os.environ.get("CRON_SECRET", "")
    if not expected:
        raise HTTPException(500, "CRON_SECRET not configured on server")
    if secret != expected:
        raise HTTPException(401, "Invalid cron secret")


def _in_quiet_hours(profile: dict) -> bool:
    if not profile.get("quiet_hours_enabled", True):
        return False
    start = profile.get("quiet_hours_start", 21)
    end = profile.get("quiet_hours_end", 6)
    hour = datetime.utcnow().hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


@router.post("/cron/weekly-digest")
async def weekly_digest(x_cron_secret: str = Header(None)):
    _require_cron(x_cron_secret)
    try:
        return await _weekly_digest_impl()
    except Exception as e:
        import traceback
        return {
            "error": str(e)[:500],
            "trace": traceback.format_exc()[-2000:],
        }


async def _weekly_digest_impl():
    sb = _supa()

    users = sb.table("user_profiles").select(
        "user_id, first_name, quiet_hours_enabled, quiet_hours_start, quiet_hours_end"
    ).execute()
    rows = users.data or []
    queued = 0
    skipped_quiet = 0
    skipped_empty = 0

    for u in rows:
        uid = u.get("user_id")
        if not uid:
            continue
        if _in_quiet_hours(u):
            skipped_quiet += 1
            continue

        try:
            dg = sb.rpc("get_weekly_digest", {"p_user_id": uid}).execute()
            d = dg.data or {}
        except Exception:
            d = {}

        scans = int(d.get("scans", 0) or 0)
        disease = int(d.get("disease_hits", 0) or 0)
        revenue = float(d.get("revenue_ngn", 0) or 0)

        if scans == 0 and disease == 0 and revenue == 0:
            skipped_empty += 1
            continue

        parts = [f"{scans} scan{'s' if scans != 1 else ''}"]
        if disease:
            parts.append(f"{disease} disease hit{'s' if disease != 1 else ''}")
        if revenue:
            parts.append(f"₦{int(revenue):,} revenue")

        name = (u.get("first_name") or "Farmer").split()[0]
        title = f"Your week, {name}"
        body = " · ".join(parts)

        try:
            sb.table("notification_queue").insert({
                "user_id": uid,
                "title": title,
                "body": body,
                "data": {"screen": "/notifications", "type": "digest"},
                "sent": False,
            }).execute()
            queued += 1
        except Exception:
            pass

    return {
        "ok": True,
        "queued": queued,
        "skipped_quiet_hours": skipped_quiet,
        "skipped_empty": skipped_empty,
        "total_users": len(rows),
    }


@router.post("/cron/drain-queue")
async def drain_queue(x_cron_secret: str = Header(None), batch_size: int = 100):
    _require_cron(x_cron_secret)
    try:
        return await _drain_queue_impl(batch_size)
    except Exception as e:
        import traceback
        return {
            "error": str(e)[:500],
            "trace": traceback.format_exc()[-2000:],
        }


async def _drain_queue_impl(batch_size: int = 100):
    sb = _supa()

    q = sb.table("notification_queue").select("*").eq("sent", False).order("created_at").limit(batch_size).execute()
    rows = q.data or []
    if not rows:
        return {"ok": True, "sent": 0, "failed": 0, "remaining": 0}

    user_ids = list({r["user_id"] for r in rows})
    tokens_resp = sb.table("push_tokens").select("user_id, token").in_("user_id", user_ids).execute()
    tokens_by_user: dict[str, list[str]] = {}
    for t in tokens_resp.data or []:
        tokens_by_user.setdefault(t["user_id"], []).append(t["token"])

    messages = []
    for row in rows:
        for tok in tokens_by_user.get(row["user_id"], []):
            messages.append({
                "to": tok,
                "title": row["title"],
                "body": row["body"],
                "data": row.get("data") or {},
                "sound": "default",
                "priority": "high",
                "channelId": "default",
            })

    ids = [r["id"] for r in rows]

    if not messages:
        sb.table("notification_queue").update({"sent": True}).in_("id", ids).execute()
        return {"ok": True, "sent": 0, "failed": 0, "no_tokens": len(rows)}

    sent = 0
    failed = 0
    async with httpx.AsyncClient(timeout=25) as client:
        for i in range(0, len(messages), 100):
            batch = messages[i:i + 100]
            try:
                resp = await client.post(
                    EXPO_PUSH_URL,
                    json=batch,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    for d in data:
                        if d.get("status") == "ok":
                            sent += 1
                        else:
                            failed += 1
                else:
                    failed += len(batch)
            except Exception:
                failed += len(batch)

    sb.table("notification_queue").update({"sent": True}).in_("id", ids).execute()

    rem = sb.table("notification_queue").select("id", count="exact").eq("sent", False).execute()
    remaining = rem.count or 0

    return {"ok": True, "sent": sent, "failed": failed, "remaining": remaining}


@router.get("/cron/_env")
async def env_diag(x_cron_secret: str = Header(None)):
    """Dump which env vars are visible + which source works."""
    _require_cron(x_cron_secret)
    cfg = _load_env_sources()
    all_keys = sorted(os.environ.keys())
    supa_keys = [k for k in all_keys if "SUPA" in k.upper()]
    return {
_URL        "env_sources_result": {
            "url_present"": bool(cfg["url"]),
            "key_pesent": bool(cfg["key"]),
           ], "source": cfg["source"],
        },
        "os_environ_has\_SUPABASE_URL": bool(os.environ.get("SUPsABASE_URL")),
        "os_environ_has*\_SUPABASE_SERVICE_KEY": bool(os.environn.get("SUPABASE_SERVICE_KEY")),
        "os_environ_has\_CRON_SECRET": bool(os.environ.get("CRsON_SECRET")),
        "os_environ_has_SELF_URL": bool(os.environ.get("SELF_URL")),
        "all_supa_keys": supa_keys,
        "total_env_keys": len(all_keys),
        "sample_keys": all_keys[:40],
    }
