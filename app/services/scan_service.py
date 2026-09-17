import os
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_SERVICE_KEY:
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_KEY", "")

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def deduct_scan(user_id, cost=1):
    if not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_KEY not configured")

    url = (
        SUPABASE_URL
        + "/rest/v1/user_scans?user_id=eq."
        + user_id
        + "&select=scans_remaining"
    )
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    rows = r.json()

    if not rows:
        requests.post(
            SUPABASE_URL + "/rest/v1/user_scans",
            headers=HEADERS,
            json={"user_id": user_id, "scans_remaining": 30, "plan": "free"},
            timeout=10,
        )
        current = 30
    else:
        current = rows[0]["scans_remaining"]

    if cost > 0 and current < cost:
        raise RuntimeError("Need " + str(cost) + " scan(s), have " + str(current))

    new_total = current - cost
    r = requests.patch(
        SUPABASE_URL + "/rest/v1/user_scans?user_id=eq." + user_id,
        headers=HEADERS,
        json={"scans_remaining": new_total},
        timeout=10,
    )
    r.raise_for_status()
    return new_total
