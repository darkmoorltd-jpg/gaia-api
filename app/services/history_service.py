import os
import requests
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "https://pxvtvuwlpzwlkdoxjrep.supabase.co",
)
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": "Bearer " + SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def save_diagnosis(user_id, model_key, context_type, predictions):
    if not SERVICE_KEY:
        return None
    if not predictions:
        return None
    top = predictions[0]
    payload = {
        "user_id": user_id,
        "type": context_type,
        "model_key": model_key,
        "top_label": top.get("label", "Unknown"),
        "confidence": float(top.get("confidence", 0)),
        "emoji": "",
        "predictions": predictions,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        r = requests.post(
            SUPABASE_URL + "/rest/v1/scan_history",
            headers=HEADERS,
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            data = r.json()
            if isinstance(data, list) and data:
                return data[0].get("id")
    except Exception:
        pass
    return None
