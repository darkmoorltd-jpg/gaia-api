import os
import io
import base64
import requests
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageDraw

router = APIRouter()

GOOGLE_MAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SENTINEL_CLIENT_ID = os.getenv("SENTINEL_CLIENT_ID", "")
SENTINEL_CLIENT_SECRET = os.getenv("SENTINEL_CLIENT_SECRET", "")


def _hg_headers():
    return {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"}


def _fetch_batch(batch_code: str):
    url = f"{SUPABASE_URL}/rest/v1/batches?batch_code=eq.{batch_code}&select=*"
    r = requests.get(url, headers=_hg_headers(), timeout=15)
    if r.status_code != 200 or not r.json():
        raise HTTPException(404, "Batch not found")
    return r.json()[0]


def _fetch_farmer(farmer_id: str):
    url = f"{SUPABASE_URL}/rest/v1/user_profiles?user_id=eq.{farmer_id}&select=first_name,last_name,state,lga,phone"
    r = requests.get(url, headers=_hg_headers(), timeout=15)
    return r.json()[0] if r.json() else {}


def _fetch_events(batch_id: str):
    url = f"{SUPABASE_URL}/rest/v1/batch_events?batch_id=eq.{batch_id}&order=created_at.asc"
    r = requests.get(url, headers=_hg_headers(), timeout=15)
    return r.json() if r.status_code == 200 else []


def _centroid(polygon):
    if not polygon:
        return None
    return (sum(p["lat"] for p in polygon) / len(polygon),
            sum(p["lon"] for p in polygon) / len(polygon))


def _polygon_bbox(polygon, pad=0.003):
    lats = [p["lat"] for p in polygon]
    lons = [p["lon"] for p in polygon]
    return (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)


def _google_maps_static(lat, lon, size=600, zoom=17):
    if not GOOGLE_MAPS_KEY:
        return None
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/staticmap",
            params={"center": f"{lat},{lon}", "zoom": zoom, "size": f"{size}x{size}",
                    "maptype": "satellite", "scale": 2, "key": GOOGLE_MAPS_KEY},
            timeout=20,
        )
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        print(f"Google Maps: {r.status_code}")
    except Exception as e:
        print(f"Google Maps failed: {e}")
    return None


def _sentinel_token():
    if not SENTINEL_CLIENT_ID or not SENTINEL_CLIENT_SECRET:
        return None
    try:
        creds = base64.b64encode(f"{SENTINEL_CLIENT_ID}:{SENTINEL_CLIENT_SECRET}".encode()).decode()
        r = requests.post(
            "https://services.sentinel-hub.com/oauth/token",
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"}, timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("access_token")
    except Exception as e:
        print(f"Sentinel auth: {e}")
    return None


def _sentinel_image(bbox, date_from, date_to, size=600):
    token = _sentinel_token()
    if not token:
        return None
    evalscript = """
    //VERSION=3
    function setup() { return { input: ["B04","B03","B02"], output: { bands: 3 } }; }
    function evaluatePixel(s) {
        return [Math.min(1, s.B04/10000*2.5), Math.min(1, s.B03/10000*2.5), Math.min(1, s.B02/10000*2.5)];
    }
    """
    payload = {
        "input": {
            "bounds": {"bbox": bbox, "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}},
            "data": [{"type": "sentinel-2-l2a", "dataFilter": {"timeRange": {"from": f"{date_from}T00:00:00Z", "to": f"{date_to}T23:59:59Z"}, "maxCloudCoverage": 30, "mosaickingOrder": "leastCC"}}],
        },
        "output": {"width": size, "height": size, "responses": [{"identifier": "default", "format": {"type": "image/png"}}]},
        "evalscript": evalscript,
    }
    try:
        r = requests.post(
            "https://services.sentinel-hub.com/api/v1/process",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=45,
        )
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        print(f"Sentinel img: {r.status_code} {r.text[:150]}")
    except Exception as e:
        print(f"Sentinel img failed: {e}")
    return None


def _sentinel_ndvi(bbox, date_from, date_to):
    """Fetch average NDVI using Sentinel Statistical API."""
    token = _sentinel_token()
    if not token:
        return None
    payload = {
        "input": {
            "bounds": {"bbox": bbox, "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}},
            "data": [{"type": "sentinel-2-l2a", "dataFilter": {"timeRange": {"from": f"{date_from}T00:00:00Z", "to": f"{date_to}T23:59:59Z"}, "maxCloudCoverage": 30}}],
        },
        "aggregation": {
            "timeRange": {"from": f"{date_from}T00:00:00Z", "to": f"{date_to}T23:59:59Z"},
            "aggregationInterval": {"of": "P30D"},
            "evalscript": """
                //VERSION=3
                function setup() { return { input: ["B04","B08"], output: [{ id: "ndvi", bands: 1 }, { id: "dataMask", bands: 1 }] }; }
                function evaluatePixel(s) {
                    let ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 0.001);
                    return { ndvi: [ndvi], dataMask: [s.dataMask] };
                }
            """,
            "resx": 10, "resy": 10,
        },
        "calculations": {"default": {}},
    }
    try:
        r = requests.post(
            "https://services.sentinel-hub.com/api/v1/statistics",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=45,
        )
        if r.status_code == 200:
            data = r.json()
            means = []
            for interval in data.get("data", []):
                stats = interval.get("outputs", {}).get("ndvi", {}).get("bands", {}).get("B0", {}).get("stats", {})
                if "mean" in stats:
                    means.append(stats["mean"])
            return sum(means) / len(means) if means else None
        print(f"Sentinel stats: {r.status_code}")
    except Exception as e:
        print(f"Sentinel stats failed: {e}")
    return None


def _draw_polygon(img, polygon, bbox):
    if not polygon or not bbox:
        return img
    W, H = img.size
    lon_min, lat_min, lon_max, lat_max = bbox
    draw = ImageDraw.Draw(img, "RGBA")

    def to_px(p):
        x = (p["lon"] - lon_min) / (lon_max - lon_min) * W
        y = (lat_max - p["lat"]) / (lat_max - lat_min) * H
        return (x, y)

    pts = [to_px(p) for p in polygon]
    draw.polygon(pts, fill=(0, 255, 136, 60), outline=(0, 255, 136, 255), width=4)
    for x, y in pts:
        draw.ellipse([x-6, y-6, x+6, y+6], fill=(0, 255, 136, 255), outline=(255, 255, 255, 255), width=2)
    return img


def _ndvi_to_forest_pct(ndvi):
    if ndvi is None:
        return None
    # NDVI 0.1 → 0% forest, 0.8 → 100% forest
    return min(100, max(0, (ndvi - 0.1) / 0.7 * 100))


def _generate_pdf(batch, farmer, events) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    # ===== HEADER =====
    c.setFillColor(HexColor("#1b5e20"))
    c.rect(0, H - 90, W, 90, fill=1, stroke=0)
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, H - 50, "EUDR COMPLIANCE CERTIFICATE")
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 66, "Deforestation-free supply chain · GAIA Verified · Powered by Darkmoor Ltd")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 40, H - 50, batch.get("batch_code", ""))
    c.setFont("Helvetica", 8)
    c.drawRightString(W - 40, H - 66, f"Issued {datetime.now().strftime('%d %b %Y')}")

    # ===== BATCH DETAILS =====
    y = H - 115
    c.setFillColor(HexColor("#000000"))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "Batch Details")
    y -= 18
    details = [
        ("Crop", batch.get("crop_name", "")),
        ("Quantity", f"{batch.get('quantity_kg', 0)} kg"),
        ("Grade", batch.get("quality_grade", "")),
        ("Harvest Date", str(batch.get("harvest_date", ""))),
    ]
    if farmer:
        details.append(("Farmer", f"{farmer.get('first_name','')} {farmer.get('last_name','')}".strip()))
        details.append(("Location", f"{farmer.get('lga','')}, {farmer.get('state','')}"))
    c.setFont("Helvetica", 9)
    for k, v in details:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(40, y, k + ":")
        c.setFont("Helvetica", 9)
        c.drawString(120, y, str(v))
        y -= 13

    # ===== GEO DATA =====
    polygon = batch.get("farm_polygon")
    gps_lat = batch.get("gps_lat")
    gps_lon = batch.get("gps_lon")

    if not polygon and gps_lat and gps_lon:
        lat, lon = gps_lat, gps_lon
        bbox = (lon - 0.003, lat - 0.003, lon + 0.003, lat + 0.003)
        polygon = []
    elif polygon:
        lat, lon = _centroid(polygon)
        bbox = _polygon_bbox(polygon)
    else:
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(40, y, "No GPS data recorded for this batch.")
        c.showPage(); c.save()
        buf.seek(0)
        return buf.read()

    # ===== CURRENT SATELLITE =====
    y -= 20
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "Farm Polygon — Current Satellite")
    y -= 12

    # Try Google Maps first, fall back to Sentinel
    img_cur = _google_maps_static(lat, lon, size=600, zoom=17)
    source = "Google Maps · Maxar"
    if img_cur is None:
        today = datetime.now()
        img_cur = _sentinel_image(bbox, (today - timedelta(days=60)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), size=600)
        source = "Sentinel-2 L2A · ESA Copernicus"

    if img_cur:
        img_cur = _draw_polygon(img_cur, polygon, bbox)
        iw, ih = img_cur.size
        pw = W - 80
        ph = pw * ih / iw
        if ph > 300:
            ph = 300; pw = ph * iw / ih
        imgbuf = io.BytesIO()
        img_cur.save(imgbuf, format="PNG")
        imgbuf.seek(0)
        c.drawImage(ImageReader(imgbuf), 40, y - ph, width=pw, height=ph)
        c.setFont("Helvetica-Oblique", 7)
        c.drawString(40, y - ph - 10, f"Source: {source}")
        c.drawString(40, y - ph - 19, f"Polygon: {len(polygon or [])} vertices · {batch.get('farm_area_hectares', 0)} ha")
        y = y - ph - 30

    # ===== PAGE 2: 10-YEAR FOREST COVER =====
    c.showPage()
    c.setFillColor(HexColor("#1b5e20"))
    c.rect(0, H - 60, W, 60, fill=1, stroke=0)
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Helvetica-Bold", 15)
    c.drawString(40, H - 35, "10-Year Forest Cover Comparison")
    c.setFont("Helvetica", 8)
    c.drawString(40, H - 50, "EUDR requires proving land was NOT deforested after 31 Dec 2020")

    y = H - 85
    c.setFillColor(HexColor("#000000"))
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "Comparing forest cover at the farm polygon in 2015 (baseline) vs today. Sentinel-2 · 10m resolution.")
    y -= 30

    # Fetch 2015 baseline + current
    today = datetime.now()
    img_2015 = _sentinel_image(bbox, "2015-01-01", "2015-12-31", size=600)
    ndvi_2015 = _sentinel_ndvi(bbox, "2015-01-01", "2015-12-31")

    img_now = _sentinel_image(bbox, (today - timedelta(days=90)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), size=600)
    ndvi_now = _sentinel_ndvi(bbox, (today - timedelta(days=90)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))

    cw = (W - 100) / 2
    ch = cw

    # ===== 2015 =====
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "2015 — Baseline")
    c.setFont("Helvetica", 8)
    c.setFillColor(HexColor("#666666"))
    c.drawString(40, y - 11, "Pre-2020 forest state")
    c.setFillColor(HexColor("#000000"))

    if img_2015:
        img_2015 = _draw_polygon(img_2015, polygon, bbox)
        b15 = io.BytesIO(); img_2015.save(b15, format="PNG"); b15.seek(0)
        c.drawImage(ImageReader(b15), 40, y - 25 - ch, width=cw, height=ch)
    else:
        c.setFont("Helvetica-Oblique", 8)
        c.drawString(40, y - 60, "Sentinel-2 not available for 2015")

    pct_2015 = _ndvi_to_forest_pct(ndvi_2015)
    if pct_2015 is not None:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(40, y - 30 - ch, f"Forest Cover: {pct_2015:.1f}%")
        c.setFont("Helvetica", 8)
        c.drawString(40, y - 41 - ch, f"Avg NDVI: {ndvi_2015:.3f}")

    # ===== TODAY =====
    c.setFont("Helvetica-Bold", 11)
    c.drawString(W/2 + 10, y, f"{today.year} — Current")
    c.setFont("Helvetica", 8)
    c.setFillColor(HexColor("#666666"))
    c.drawString(W/2 + 10, y - 11, "Post-2020 state")
    c.setFillColor(HexColor("#000000"))

    if img_now:
        img_now = _draw_polygon(img_now, polygon, bbox)
        bn = io.BytesIO(); img_now.save(bn, format="PNG"); bn.seek(0)
        c.drawImage(ImageReader(bn), W/2 + 10, y - 25 - ch, width=cw, height=ch)
    else:
        c.setFont("Helvetica-Oblique", 8)
        c.drawString(W/2 + 10, y - 60, "Sentinel-2 not available")

    pct_now = _ndvi_to_forest_pct(ndvi_now)
    if pct_now is not None:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(W/2 + 10, y - 30 - ch, f"Forest Cover: {pct_now:.1f}%")
        c.setFont("Helvetica", 8)
        c.drawString(W/2 + 10, y - 41 - ch, f"Avg NDVI: {ndvi_now:.3f}")

    # ===== VERDICT =====
    yv = y - ch - 75
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, yv, "EUDR Compliance Verdict")
    yv -= 20

    if pct_2015 is not None and pct_now is not None:
        delta = pct_now - pct_2015
        delta_pct = (delta / pct_2015 * 100) if pct_2015 > 0 else 0

        if delta_pct >= -5:
            verdict = "COMPLIANT"
            color = "#00a651"
            text = "No significant deforestation detected. Batch qualifies for EUDR export."
        else:
            verdict = "AT RISK"
            color = "#e65100"
            text = "Significant forest loss detected. Manual verification required."

        c.setFillColor(HexColor(color))
        c.setFont("Helvetica-Bold", 15)
        c.drawString(40, yv, f"✓ {verdict}")
        c.setFillColor(HexColor("#000000"))
        c.setFont("Helvetica", 10)
        c.drawString(40, yv - 18, text)

        yv -= 45
        c.setFont("Helvetica-Bold", 10)
        c.drawString(40, yv, "Metrics:")
        c.setFont("Helvetica", 9)
        c.drawString(120, yv, f"Forest cover 2015: {pct_2015:.1f}%")
        c.drawString(120, yv - 13, f"Forest cover {today.year}: {pct_now:.1f}%")
        c.drawString(120, yv - 26, f"Change: {delta:+.1f} pp ({delta_pct:+.1f}%)")
    else:
        c.setFont("Helvetica-Oblique", 10)
        c.drawString(40, yv, "Insufficient satellite data for automated verification.")
        c.drawString(40, yv - 14, "Manual review required before EUDR export.")

    # ===== FOOTER =====
    c.setFillColor(HexColor("#1b5e20"))
    c.rect(0, 0, W, 28, fill=1, stroke=0)
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Helvetica", 7)
    c.drawString(40, 11, "GAIA · Darkmoor Ltd · EUDR Compliance Certificate")
    c.drawRightString(W - 40, 11, f"Page 2/2 · {datetime.now().strftime('%d %b %Y')}")

    c.save()
    buf.seek(0)
    return buf.read()


@router.get("/logistics/eudr/{batch_code}/pdf")
async def eudr_pdf(batch_code: str):
    batch = _fetch_batch(batch_code)
    farmer = _fetch_farmer(batch["farmer_id"])
    events = _fetch_events(batch["id"])
    pdf = _generate_pdf(batch, farmer, events)
    return StreamingResponse(
        io.BytesIO(pdf), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="EUDR_{batch_code}.pdf"'},
    )
