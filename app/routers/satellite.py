# app/routers/satellite.py
# Sentinel Hub proxy: tile image + numeric NDVI + timeseries
import os
import time
import base64
import logging
import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

log = logging.getLogger('gaia.satellite')
router = APIRouter()

CLIENT_ID = os.environ.get("SENTINEL_CLIENT_ID", "sh-866fb988-5387-4d82-bb2d-6c2c2aab2e60")
CLIENT_SECRET = os.environ.get("SENTINEL_CLIENT_SECRET", "jG8r1sNnkLLHYAuhUMzDSwcwSwywvu3IV0nhRdAinNDeadueT5qANvRIgy4lo9rd2K1qzVPt21e5lQ7pEazC7h")
INSTANCE_ID = os.environ.get("SENTINEL_INSTANCE_ID", "575061f7-641d-4d0c-afd2-7cc9516e1848")

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
STATS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"

_token_cache = {"token": None, "expires_at": 0}


async def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    if r.status_code != 200:
        raise HTTPException(502, "Sentinel auth failed: " + r.text[:200])
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3500)
    return _token_cache["token"]


def _evalscript(layer: str) -> str:
    if layer == "TRUE_COLOR":
        return """//VERSION=3
        function setup() { return { input: ["B04","B03","B02"], output: { bands: 3 } }; }
        function evaluatePixel(s) {
            return [s.B04/10000*3.5, s.B03/10000*3.5, s.B02/10000*3.5];
        }"""
    if layer == "NDVI":
        return """//VERSION=3
        function setup() { return { input: ["B04","B08"], output: { bands: 3 } }; }
        function evaluatePixel(s) {
            let ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 0.0001);
            if (ndvi > 0.6) return [0, 0.8, 0.3];
            if (ndvi > 0.4) return [0.6, 0.9, 0.2];
            if (ndvi > 0.2) return [1, 0.8, 0.2];
            if (ndvi > 0.0) return [1, 0.4, 0.2];
            return [0.6, 0.2, 0.1];
        }"""
    if layer == "MOISTURE":
        return """//VERSION=3
        function setup() { return { input: ["B08","B11"], output: { bands: 3 } }; }
        function evaluatePixel(s) {
            let ndmi = (s.B08 - s.B11) / (s.B08 + s.B11 + 0.0001);
            if (ndmi > 0.4) return [0.1, 0.4, 0.9];
            if (ndmi > 0.2) return [0.2, 0.6, 0.9];
            if (ndmi > 0.0) return [0.9, 0.7, 0.3];
            return [0.9, 0.3, 0.1];
        }"""
    return _evalscript("TRUE_COLOR")


@router.get("/satellite/tile")
async def satellite_tile(
    lat: float = Query(...),
    lon: float = Query(...),
    layer: str = Query("TRUE_COLOR"),
    days: int = Query(60),
    size: int = Query(512),
):
    token = await _get_token()
    delta = 0.003
    bbox = [lon - delta, lat - delta, lon + delta, lat + delta]
    from_dt = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    to_dt = time.strftime("%Y-%m-%d", time.gmtime())

    payload = {
        "input": {
            "bounds": {"bbox": bbox, "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}},
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": from_dt + "T00:00:00Z", "to": to_dt + "T23:59:59Z"},
                    "maxCloudCoverage": 40,
                    "mosaickingOrder": "leastCC",
                },
            }],
        },
        "output": {"width": size, "height": size, "responses": [{"identifier": "default", "format": {"type": "image/png"}}]},
        "evalscript": _evalscript(layer.upper()),
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            PROCESS_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code != 200:
        raise HTTPException(502, "Sentinel process failed: " + r.text[:200])
    return Response(content=r.content, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/satellite/ndvi")
async def satellite_ndvi(
    lat: float = Query(...),
    lon: float = Query(...),
    days: int = Query(30),
    time_mode: str = Query("latest"),
):
    """Compute NDVI from Copernicus WMTS tile via pixel counting.
    Uses the same algorithm as the mobile app for consistency."""
    import math
    from io import BytesIO
    from PIL import Image

    try:
        # Convert lat/lon to WMTS tile coords (zoom 14 = ~10m/px, good for farm)
        zoom = 14
        n = 2.0 ** zoom
        lat_rad = math.radians(lat)
        xtile = int((lon + 180.0) / 360.0 * n)
        ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)

        # Time parameter
        if time_mode == "peak":
            time_param = "2024-07-01/2024-09-30"
        elif time_mode == "30d":
            from_dt = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
            to_dt = time.strftime("%Y-%m-%d", time.gmtime())
            time_param = from_dt + "/" + to_dt
        else:
            time_param = "2023-01-01/2026-12-31"

        # Fetch tile from WMTS
        token = await _get_token()
        url = (
            WMTS_BASE + "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
            "&LAYER=NDVI&STYLE=default&TILEMATRIXSET=PopularWebMercator256"
            "&TILEMATRIX=" + str(zoom) + "&TILEROW=" + str(ytile) + "&TILECOL=" + str(xtile) +
            "&FORMAT=image/png&TIME=" + time_param + "&access_token=" + token
        )

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url)

        if r.status_code != 200:
            raise HTTPException(502, "WMTS tile fetch failed: " + r.text[:200])

        # Decode PNG and count pixels by color bucket
        img = Image.open(BytesIO(r.content)).convert('RGB')
        pixels = list(img.getdata())
        total = len(pixels)

        # Copernicus NDVI evalscript color buckets:
        # RED       [0.6, 0.2, 0.1]   → ndvi < 0.2  (bare/water)
        # ORANGE    [1, 0.4, 0.2]     → 0.2-0.4     (sparse)
        # YELLOW    [1, 0.8, 0.2]     → 0.4-0.6     (moderate)
        # LGREEN    [0.6, 0.9, 0.2]   → 0.6-0.8     (good)
        # DGREEN    [0, 0.8, 0.3]     → > 0.8       (excellent)

        red = sum(1 for p in pixels if p[0] > 130 and p[1] < 100 and p[2] < 80)
        orange = sum(1 for p in pixels if p[0] > 200 and 80 < p[1] < 150 and p[2] < 100)
        yellow = sum(1 for p in pixels if p[0] > 200 and p[1] > 180 and p[2] < 100)
        lgreen = sum(1 for p in pixels if 120 < p[0] < 200 and p[1] > 180 and p[2] < 120)
        dgreen = sum(1 for p in pixels if p[0] < 100 and p[1] > 180 and p[2] < 150)

        # Weighted average NDVI
        weighted = (
            red * 0.1 +
            orange * 0.3 +
            yellow * 0.5 +
            lgreen * 0.7 +
            dgreen * 0.9
        )
        avg_ndvi = (weighted / total) if total > 0 else 0.0

        status = (
            "healthy" if avg_ndvi >= 0.6 else
            "moderate" if avg_ndvi >= 0.4 else
            "stressed" if avg_ndvi >= 0.2 else
            "bare"
        )

        return {
            "ok": True,
            "latest": {
                "date": time.strftime("%Y-%m-%d"),
                "ndvi": round(avg_ndvi, 3),
                "ndmi": None,
            },
            "series": [],
            "status": status,
            "pixel_breakdown": {
                "red": red,
                "orange": orange,
                "yellow": yellow,
                "light_green": lgreen,
                "dark_green": dgreen,
                "total": total,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        log.exception("NDVI compute failed")
        raise HTTPException(500, "NDVI compute failed: " + str(e)[:200])



# ============================================================
# FAST WMTS endpoint (uses your pre-configured Copernicus layers)
# ============================================================
WMTS_BASE = f"https://sh.dataspace.copernicus.eu/ogc/wmts/{INSTANCE_ID}"


@router.get("/satellite/wmts")
async def satellite_wmts(
    lat: float = Query(...),
    lon: float = Query(...),
    layer: str = Query("NDVI"),
    z: int = Query(14),
    time_mode: str = Query("latest"),
):
    """Direct WMTS proxy - much faster than Process API."""
    import math
    lat_rad = math.radians(lat)
    n = 2.0 ** z
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)

    if time_mode == "peak":
        time_param = "2024-07-01/2024-09-30"
    elif time_mode == "30d":
        from_dt = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
        to_dt = time.strftime("%Y-%m-%d", time.gmtime())
        time_param = f"{from_dt}/{to_dt}"
    else:
        time_param = "2023-01-01/2026-12-31"

    token = await _get_token()
    url = (
        f"{WMTS_BASE}?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
        f"&LAYER={layer}&STYLE=default&TILEMATRIXSET=PopularWebMercator256"
        f"&TILEMATRIX={z}&TILEROW={ytile}&TILECOL={xtile}"
        f"&FORMAT=image/png&TIME={time_param}&access_token={token}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)

    if r.status_code != 200:
        raise HTTPException(502, "WMTS tile failed: " + r.text[:200])

    return Response(
        content=r.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )
