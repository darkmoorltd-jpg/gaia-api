import os
import base64
import time
import requests

CLIENT_ID = os.environ.get("SENTINEL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SENTINEL_CLIENT_SECRET", "")

_token_cache = {"token": None, "expires": 0}


def get_token() -> str:
    if _token_cache["token"] and _token_cache["expires"] > time.time() + 60:
        return _token_cache["token"]
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("SENTINEL_CLIENT_ID / SENTINEL_CLIENT_SECRET not set")
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        "https://services.sentinel-hub.com/oauth/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires"] = time.time() + data.get("expires_in", 3600)
    return data["access_token"]


EVALSCRIPTS = {
    "TRUE_COLOR": """
        //VERSION=3
        function setup() {
            return { input: ["B04","B03","B02"], output: { bands: 3 } };
        }
        function evaluatePixel(s) {
            return [s.B04/3000, s.B03/3000, s.B02/3000];
        }
    """,
    "NDVI": """
        //VERSION=3
        function setup() {
            return { input: ["B04","B08"], output: { bands: 1 } };
        }
        function evaluatePixel(s) {
            let ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 0.0001);
            return [ndvi];
        }
    """,
    "MOISTURE": """
        //VERSION=3
        function setup() {
            return { input: ["B08","B11"], output: { bands: 1 } };
        }
        function evaluatePixel(s) {
            let ndmi = (s.B08 - s.B11) / (s.B08 + s.B11 + 0.0001);
            return [(ndmi + 1) / 2];
        }
    """,
}


def fetch_tile(lat: float, lon: float, layer: str = "TRUE_COLOR", size: int = 512) -> bytes:
    token = get_token()
    evalscript = EVALSCRIPTS.get(layer, EVALSCRIPTS["TRUE_COLOR"])
    delta = 0.003
    bbox = [lon - delta, lat - delta, lon + delta, lat + delta]
    payload = {
        "input": {
            "bounds": {
                "bbox": bbox,
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {"maxCloudCoverage": 60, "mosaickingOrder": "leastCC"},
            }],
        },
        "output": {
            "width": size,
            "height": size,
            "responses": [{"identifier": "default", "format": {"type": "image/png"}}],
        },
        "evalscript": evalscript,
    }
    r = requests.post(
        "https://services.sentinel-hub.com/api/v1/process",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "image/png",
        },
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.content
