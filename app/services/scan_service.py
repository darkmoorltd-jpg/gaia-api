import os


def _supabase_url() -> str:
    return os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")


def _supabase_key() -> str:
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_KEY not set")
    return key


def deduct_scan(user_id: str, cost: int = 1) -> int:
    import requests
    url = _supabase_url()
    key = _supabase_key()
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    r = requests.get(
        url + "/rest/v1/user_scans?user_id=eq." + user_id + "&select=scans_remaining",
        headers=headers,
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json()

    if not rows:
        requests.post(
            url + "/rest/v1/user_scans",
            headers=headers,
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
        url + "/rest/v1/user_scans?user_id=eq." + user_id,
        headers=headers,
        json={"scans_remaining": new_total},
        timeout=10,
    )
    r.raise_for_status()
    return new_total
