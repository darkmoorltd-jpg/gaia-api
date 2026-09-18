import os
import requests

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "https://pxvtvuwlpzwlkdoxjrep.supabase.co",
)
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": "Bearer " + SERVICE_KEY,
    "Content-Type": "application/json",
}


def _check_configured():
    if not SERVICE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_KEY not configured")


def get_remaining(user_id):
    _check_configured()
    r = requests.get(
        SUPABASE_URL + "/rest/v1/user_scans",
        params={"user_id": "eq." + user_id, "select": "scans_remaining"},
        headers=HEADERS,
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        requests.post(
            SUPABASE_URL + "/rest/v1/user_scans",
            headers={**HEADERS, "Prefer": "return=representation"},
            json={"user_id": user_id, "scans_remaining": 30, "plan": "free"},
            timeout=10,
        )
        return 30
    return rows[0]["scans_remaining"]


def deduct_scan(user_id, cost=1):
    _check_configured()
    current = get_remaining(user_id)
    if current < cost:
        raise RuntimeError("Insufficient scans: have " + str(current))
    r = requests.post(
        SUPABASE_URL + "/rest/v1/rpc/decrement_scan",
        headers=HEADERS,
        json={"uid": user_id},
        timeout=10,
    )
    r.raise_for_status()
    return get_remaining(user_id)


def refund_scan(user_id, amount=1):
    _check_configured()
    current = get_remaining(user_id)
    r = requests.patch(
        SUPABASE_URL + "/rest/v1/user_scans",
        params={"user_id": "eq." + user_id},
        headers={**HEADERS, "Prefer": "return=representation"},
        json={"scans_remaining": current + amount},
        timeout=10,
    )
    r.raise_for_status()
    return current + amount
