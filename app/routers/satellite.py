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

CLIENT_ID = os.environ.get("SENTINEL_CLIENT_ID", "86ed44fa-793b-47da-973b-345a83ae18c0")
CLIENT_SECRET = os.environ.get("SENTINEL_CLIENT_SECRET", "qYTQXnQFpgstJSrAulJ6NREflI2m2eCN")

TOKEN_URL = "https://services.sentinel-hub.com/oauth/token"
PROCESS_URL = "https://services.sentinel-hub.com/api/v1/process"
STATS_URL = "https://services.sentinel-hub.com/api/v1/statistics"

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
):
    """Return numeric mean NDVI + NDMI for a small area."""
    token = await _get_token()
    delta = 0.003
    bbox = [lon - delta, lat - delta, lon + delta, lat + delta]
    from_dt = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    to_dt = time.strftime("%Y-%m-%d", time.gmtime())

    evalscript = """//VERSION=3
    function setup() {
        return {
            input: ["B04","B08","B11","SCL"],
            output: [
                { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
                { id: "ndmi", bands: 1, sampleType: "FLOAT32" },
                { id: "dataMask", bands: 1 }
            ]
        };
    }
    function evaluatePixel(s) {
        let ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 0.0001);
        let ndmi = (s.B08 - s.B11) / (s.B08 + s.B11 + 0.0001);
        let cloud = [3, 8, 9, 10].includes(s.SCL) ? 0 : 1;
        return { ndvi: [ndvi], ndmi: [ndmi], dataMask: [cloud] };
    }"""

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
        "aggregation": {
            "timeRange": {"from": from_dt + "T00:00:00Z", "to": to_dt + "T23:59:59Z"},
            "aggregationInterval": {"of": "P7D"},
            "evalscript": evalscript,
            "resx": 10,
            "resy": 10,
        },
        "calculations": {
            "default": {
                "statistics": {"default": {"percentiles": {"k": [50]}}}
            }
        },
    }

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            STATS_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code != 200:
        raise HTTPException(502, "Sentinel stats failed: " + r.text[:200])

    try:
        body = r.json()
    except Exception:
        raise HTTPException(502, "Sentinel returned invalid JSON")

    series = []
    for interval in body.get("data", []):
        interval_from = interval.get("interval", {}).get("from", "")
        ndvi_stats = (
            interval.get("outputs", {})
            .get("ndvi", {})
            .get("bands", {})
            .get("B0", {})
            .get("stats", {})
        )
        ndmi_stats = (
            interval.get("outputs", {})
            .get("ndmi", {})
            .get("bands", {})
            .get("B0", {})
            .get("stats", {})
        )
        ndvi_p50 = ndvi_stats.get("percentiles", {}).get("50.0")
        ndmi_p50 = ndmi_stats.get("percentiles", {}).get("50.0")
        if ndvi_p50 is None:
            continue
        series.append({
            "date": interval_from[:10],
            "ndvi": round(float(ndvi_p50), 4),
            "ndmi": round(float(ndmi_p50), 4) if ndmi_p50 is not None else None,
        })

    latest = series[-1] if series else None
    return {
        "ok": True,
        "series": series,
        "latest": latest,
    }
