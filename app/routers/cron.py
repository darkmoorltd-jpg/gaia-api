import json
import base64
import os
import traceback
from datetime import datetime
import httpx
from fastapi import APIRouter, Header
from supabase import create_client

router = APIRouter()

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

_ENV_CACHE = None

def _derive_url_from_key(key):
    """Supabase service keys are JWTs whose payload has 'ref' (project id).
    URL is always https://{ref}.supabase.co . Return None if we can't parse."""
    try:
        parts = key.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        # pad base64
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        ref = payload.get("ref")
        if ref:
            return "https://" + ref + ".supabase.co"
    except Exception:
        pass
    return None




def _load_env():
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE

    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    source = "os.environ"

    if not url or not key:
        try:
            from dotenv import load_dotenv
            for path in (".env", "/app/.env"):
                if os.path.exists(path):
                    load_dotenv(path)
            url = url or os.environ.get("SUPABASE_URL", "")
            key = key or os.environ.get("SUPABASE_SERVICE_KEY", "")
            if url and key:
                source = ".env"
        except Exception:
            pass

    if not url or not key:
        for mod_name in ("app.config", "app.settings", "app.core.config"):
            try:
                mod = __import__(mod_name, fromlist=["settings"])
                settings = getattr(mod, "settings", None)
                if settings is not None:
                    u = getattr(settings, "SUPABASE_URL", "") or getattr(settings, "supabase_url", "")
                    k = getattr(settings, "SUPABASE_SERVICE_KEY", "") or getattr(settings, "supabase_service_key", "")
                    if u and k:
                        url, key = u, k
                        source = mod_name
                        break
            except Exception:
                continue

    # Fallback: derive URL from service key JWT
    if not url and key:
        derived = _derive_url_from_key(key)
        if derived:
            url = derived
            source = source + " + derived_url_from_jwt"

    _ENV_CACHE = {"url": url, "key": key, "source": source}
    return _ENV_CACHE


def _supa():
    cfg = _load_env()
    if not cfg["url"] or not cfg["key"]:
        raise RuntimeError(
            "missing env: url_present={} key_present={} source={} supa_keys={}".format(
                bool(cfg["url"]),
                bool(cfg["key"]),
                cfg["source"],
                [k for k in os.environ.keys() if "SUPA" in k.upper()],
            )
        )
    return create_client(cfg["url"], cfg["key"])


def _require_cron(secret):
    expected = os.environ.get("CRON_SECRET", "")
    if not expected:
        raise RuntimeError("CRON_SECRET not configured on server")
    if secret != expected:
        raise RuntimeError("unauthorized")


def _in_quiet_hours(profile):
    if not profile.get("quiet_hours_enabled", True):
        return False
    start = profile.get("quiet_hours_start", 21)
    end = profile.get("quiet_hours_end", 6)
    hour = datetime.utcnow().hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# ============================================================
# Diagnostic
# ============================================================
@router.get("/cron/_env")
async def env_diag(x_cron_secret: str = Header(None)):
    try:
        _require_cron(x_cron_secret)
    except Exception as e:
        return {"error": str(e)}
    cfg = _load_env()
    all_keys = sorted(os.environ.keys())
    return {
        "env_sources_result": {
            "url_present": bool(cfg["url"]),
            "key_present": bool(cfg["key"]),
            "source": cfg["source"],
        },
        "os_environ_has_SUPABASE_URL": bool(os.environ.get("SUPABASE_URL")),
        "os_environ_has_SUPABASE_SERVICE_KEY": bool(os.environ.get("SUPABASE_SERVICE_KEY")),
        "os_environ_has_CRON_SECRET": bool(os.environ.get("CRON_SECRET")),
        "os_environ_has_SELF_URL": bool(os.environ.get("SELF_URL")),
        "all_supa_keys": [k for k in all_keys if "SUPA" in k.upper()],
        "total_env_keys": len(all_keys),
        "sample_keys": all_keys[:40],
    }


# ============================================================
# Weekly digest
# ============================================================
@router.post("/cron/weekly-digest")
async def weekly_digest(x_cron_secret: str = Header(None)):
    try:
        _require_cron(x_cron_secret)
    except Exception as e:
        return {"error": str(e)}
    try:
        return await _weekly_digest_impl()
    except Exception as e:
        return {"error": str(e)[:500], "trace": traceback.format_exc()[-2000:]}


async def _weekly_digest_impl():
    sb = _supa()
    users = (
        sb.table("user_profiles")
        .select("user_id, first_name, quiet_hours_enabled, quiet_hours_start, quiet_hours_end")
        .execute()
    )
    rows = users.data or []
    queued = 0
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

        parts = ["{} scan{}".format(scans, "" if scans == 1 else "s")]
        if disease:
            parts.append("{} disease hit{}".format(disease, "" if disease == 1 else "s"))
        if revenue:
            parts.append("N{} revenue".format(int(revenue)))

        name = (u.get("first_name") or "Farmer").split()[0]
        title = "Your week, " + name
        body = " - ".join(parts)

        try:
            sb.table("notification_queue").insert(
                {
                    "user_id": uid,
                    "title": title,
                    "body": body,
                    "data": {"screen": "/notifications", "type": "digest"},
                    "sent": False,
                }
            ).execute()
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


# ============================================================
# Drain queue
# ============================================================
@router.post("/cron/drain-queue")
async def drain_queue(x_cron_secret: str = Header(None), batch_size: int = 100):
    try:
        _require_cron(x_cron_secret)
    except Exception as e:
        return {"error": str(e)}
    try:
        return await _drain_queue_impl(batch_size)
    except Exception as e:
        return {"error": str(e)[:500], "trace": traceback.format_exc()[-2000:]}


async def _drain_queue_impl(batch_size):
    sb = _supa()
    q = (
        sb.table("notification_queue")
        .select("*")
        .eq("sent", False)
        .order("created_at")
        .limit(batch_size)
        .execute()
    )
    rows = q.data or []
    if not rows:
        return {"ok": True, "sent": 0, "failed": 0, "remaining": 0}

    user_ids = list({r["user_id"] for r in rows})
    tokens_resp = (
        sb.table("push_tokens").select("user_id, token").in_("user_id", user_ids).execute()
    )
    tokens_by_user = {}
    for t in tokens_resp.data or []:
        tokens_by_user.setdefault(t["user_id"], []).append(t["token"])

    messages = []
    for row in rows:
        for tok in tokens_by_user.get(row["user_id"], []):
            messages.append(
                {
                    "to": tok,
                    "title": row["title"],
                    "body": row["body"],
                    "data": row.get("data") or {},
                    "sound": "default",
                    "priority": "high",
                    "channelId": "default",
                }
            )

    ids = [r["id"] for r in rows]

    if not messages:
        sb.table("notification_queue").update({"sent": True}).in_("id", ids).execute()
        return {"ok": True, "sent": 0, "failed": 0, "no_tokens": len(rows)}

    sent = 0
    failed = 0
    async with httpx.AsyncClient(timeout=25) as client:
        for i in range(0, len(messages), 100):
            batch = messages[i : i + 100]
            try:
                resp = await client.post(
                    EXPO_PUSH_URL, json=batch, headers={"Content-Type": "application/json"}
                )
                if resp.status_code == 200:
                    for d in resp.json().get("data", []):
                        if d.get("status") == "ok":
                            sent += 1
                        else:
                            failed += 1
                else:
                    failed += len(batch)
            except Exception:
                failed += len(batch)

    sb.table("notification_queue").update({"sent": True}).in_("id", ids).execute()

    rem = (
        sb.table("notification_queue")
        .select("id", count="exact")
        .eq("sent", False)
        .execute()
    )
    remaining = rem.count or 0

    return {"ok": True, "sent": sent, "failed": failed, "remaining": remaining}
