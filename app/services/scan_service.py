import os
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get(
    "SUPABASE_SERVICE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InB4dnR2dXdscHp3bGtkb3hqcmVwIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NDIxMDQ1NywiZXhwIjoyMDk5Nzg2NDU3fQ.WMzSa17VBFfqWAFt6zNv65AOzEuboWa39cEG35ZElDY"
)

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def deduct_scan(user_id, cost=1):
    get_url = SUPABASE_URL + "/rest/v1/user_scans?user_id=eq." + user_id + "&select=scans_remaining"
    r = requests.get(get_url, headers=HEADERS, timeout=10)
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
    returned = r.json()
    if returned and len(returned) > 0:
        return returned[0]["scans_remaining"]
    return new_total
