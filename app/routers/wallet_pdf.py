# app/routers/wallet_pdf.py
# Premium statement + receipt generator (PDF + JPG)
import os
import io
from datetime import datetime, timedelta
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER
from reportlab.pdfgen import canvas as rl_canvas
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from app.services.auth import verify_supabase_token
from supabase import create_client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

GAIA_GREEN   = colors.HexColor("#00b34a")
GAIA_GREEN_D = colors.HexColor("#008a3a")
GAIA_INK     = colors.HexColor("#0d1410")
GAIA_MUTED   = colors.HexColor("#6b7772")
GAIA_LINE    = colors.HexColor("#e2e8e4")
GAIA_SOFT    = colors.HexColor("#f6faf8")
GAIA_RED     = colors.HexColor("#e5344b")
GAIA_AMBER   = colors.HexColor("#c58b00")
GAIA_BLUE    = colors.HexColor("#3c8fd1")


def svc():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def auth_user(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        return verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))


def naira(v):
    try:
        return "N " + f"{float(v):,.2f}"
    except Exception:
        return "N 0.00"


def _range_from_params(days, date_from, date_to):
    if date_from and date_to:
        try:
            df = datetime.fromisoformat(date_from)
            dt_ = datetime.fromisoformat(date_to)
            if dt_.date() < df.date():
                raise ValueError("end before start")
            return (df.isoformat(), dt_.isoformat(),
                    df.strftime("%d %b %Y") + " - " + dt_.strftime("%d %b %Y"))
        except Exception as e:
            raise HTTPException(400, "Invalid date range: " + str(e))
    end = datetime.utcnow()
    start = end - timedelta(days=max(1, int(days or 90)))
    return (start.isoformat(), end.isoformat(),
            f"Last {int(days or 90)} days")


def _fetch_rows(uid, start_iso, end_iso):
    s = svc()
    r = (s.table("wallet_transactions")
         .select("*")
         .eq("user_id", uid)
         .gte("created_at", start_iso)
         .lte("created_at", end_iso)
         .order("created_at", desc=True)
         .limit(1000)
         .execute())
    return r.data or []


def _fetch_wallet(uid):
    s = svc()
    r = (s.table("farmer_wallets")
         .select("balance,account_number,account_name,bank_name")
         .eq("user_id", uid).limit(1).execute())
    return r.data[0] if r.data else {}


# =========================================================================
# Canvas: top strip + footer on every page
# =========================================================================

class _BrandedCanvas(rl_canvas.Canvas):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for p in self._pages:
            self.__dict__.update(p)
            self._chrome(total)
            super().showPage()
        super().save()

    def _chrome(self, total):
        w, h = A4
        # Top accent strip
        self.setFillColor(GAIA_GREEN)
        self.rect(0, h - 5 * mm, w, 5 * mm, fill=1, stroke=0)
        self.setFillColor(colors.HexColor("#5cf4a0"))
        self.rect(0, h - 5.6 * mm, w, 0.6 * mm, fill=1, stroke=0)
        # Corner mark
        self.setFillColor(colors.white)
        self.setFont("Helvetica-Bold", 8)
        self.drawString(18 * mm, h - 3.6 * mm, "GAIA")
        self.setFillColor(colors.HexColor("#bff3d3"))
        self.setFont("Helvetica", 7)
        self.drawRightString(w - 18 * mm, h - 3.6 * mm, "WALLET")
        # Footer
        self.setStrokeColor(GAIA_LINE)
        self.setLineWidth(0.4)
        self.line(18 * mm, 14 * mm, w - 18 * mm, 14 * mm)
        self.setFillColor(GAIA_MUTED)
        self.setFont("Helvetica", 7)
        self.drawString(18 * mm, 10 * mm, "Darkmoor Ltd")
        self.setFont("Helvetica-Oblique", 7)
        self.drawString(48 * mm, 10 * mm, "·")
        self.drawString(52 * mm, 10 * mm, "darkmoorltd@gmail.com")
        self.setFont("Helvetica-Bold", 7)
        self.setFillColor(GAIA_GREEN_D)
        self.drawRightString(w - 18 * mm, 10 * mm,
                             "PAGE " + str(self._pageNumber) + " / " + str(total))


# =========================================================================
# Statement PDF — premium layout
# =========================================================================

def _statement_pdf_bytes(uid, email, wallet, rows, label):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title="GAIA Wallet Statement", author="Darkmoor Ltd",
    )

    ss = getSampleStyleSheet()

    kicker = ParagraphStyle("k", parent=ss["Normal"], fontName="Helvetica-Bold",
                            fontSize=8, leading=10, textColor=GAIA_GREEN_D)
    title = ParagraphStyle("t", parent=ss["Normal"], fontName="Helvetica-Bold",
                           fontSize=26, leading=30, textColor=GAIA_INK,
                           spaceBefore=4, spaceAfter=2)
    sub = ParagraphStyle("s", parent=ss["Normal"], fontName="Helvetica",
                         fontSize=9, leading=12, textColor=GAIA_MUTED)

    meta_lbl = ParagraphStyle("ml", parent=ss["Normal"], fontName="Helvetica-Bold",
                              fontSize=7.5, leading=9, textColor=GAIA_MUTED)
    meta_val = ParagraphStyle("mv", parent=ss["Normal"], fontName="Helvetica-Bold",
                              fontSize=10.5, leading=14, textColor=GAIA_INK)

    hero_lbl = ParagraphStyle("hl", parent=ss["Normal"], fontName="Helvetica-Bold",
                              fontSize=9, leading=11, textColor=colors.HexColor("#bff3d3"))
    hero_val = ParagraphStyle("hv", parent=ss["Normal"], fontName="Helvetica-Bold",
                              fontSize=42, leading=48, textColor=colors.white,
                              spaceBefore=4)
    hero_sub = ParagraphStyle("hs", parent=ss["Normal"], fontName="Helvetica",
                              fontSize=9, leading=11, textColor=colors.HexColor("#dcf7e7"))

    kpi_lbl = ParagraphStyle("kl", parent=ss["Normal"], fontName="Helvetica-Bold",
                             fontSize=7.5, leading=9, textColor=GAIA_MUTED)
    kpi_val = ParagraphStyle("kv", parent=ss["Normal"], fontName="Helvetica-Bold",
                             fontSize=17, leading=20, textColor=GAIA_INK,
                             spaceBefore=3)
    kpi_val_g = ParagraphStyle("kvg", parent=kpi_val, textColor=GAIA_GREEN_D)
    kpi_val_r = ParagraphStyle("kvr", parent=kpi_val, textColor=GAIA_RED)

    sec = ParagraphStyle("sec", parent=ss["Normal"], fontName="Helvetica-Bold",
                         fontSize=8, leading=10, textColor=GAIA_MUTED)

    foot = ParagraphStyle("f", parent=ss["Normal"], fontName="Helvetica",
                          fontSize=7, leading=10, textColor=GAIA_MUTED,
                          alignment=TA_CENTER)

    story = []

    # ---- Kicker + Title ----
    story.append(Paragraph("GAIA · DARKMOOR LTD", kicker))
    story.append(Paragraph("Account Statement", title))
    story.append(Paragraph(label + "  ·  Generated " +
                           datetime.utcnow().strftime("%d %b %Y, %H:%M UTC"), sub))
    story.append(Spacer(1, 14))

    # ---- Meta card ----
    meta_rows = [
        [Paragraph("ACCOUNT HOLDER", meta_lbl),
         Paragraph("ACCOUNT NUMBER", meta_lbl),
         Paragraph("BANK", meta_lbl)],
        [Paragraph(wallet.get("account_name") or email or "-", meta_val),
         Paragraph(wallet.get("account_number") or "-", meta_val),
         Paragraph(wallet.get("bank_name") or "-", meta_val)],
    ]
    meta = Table(meta_rows, colWidths=[58 * mm, 58 * mm, 58 * mm])
    meta.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GAIA_SOFT),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (0, 0), 14),
        ("BOTTOMPADDING", (0, 0), (0, 0), 2),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("BOTTOMPADDING", (0, 1), (0, 1), 16),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(meta)
    story.append(Spacer(1, 14))

    # ---- Hero green band ----
    hero_inner = Table(
        [[Paragraph("CURRENT BALANCE", hero_lbl)],
         [Paragraph(naira(wallet.get("balance") or 0), hero_val)],
         [Paragraph("All amounts shown in Nigerian Naira (NGN)", hero_sub)]],
        colWidths=[174 * mm])
    hero_inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 22),
        ("RIGHTPADDING", (0, 0), (-1, -1), 22),
        ("TOPPADDING", (0, 0), (0, 0), 20),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("TOPPADDING", (0, 2), (0, 2), 6),
        ("BOTTOMPADDING", (0, -1), (0, -1), 22),
    ]))
    hero_wrap = Table([[hero_inner]], colWidths=[174 * mm])
    hero_wrap.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GAIA_GREEN),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(hero_wrap)
    story.append(Spacer(1, 14))

    # ---- KPI tiles ----
    total_in = sum(float(t.get("amount") or 0) for t in rows
                   if t.get("direction") == "in" and t.get("status") == "success")
    total_out = sum(float(t.get("amount") or 0) for t in rows
                    if t.get("direction") == "out" and t.get("status") == "success")
    net = total_in - total_out

    def kpi(label, value, valstyle, top_color):
        inner = Table(
            [[Paragraph(label, kpi_lbl)], [Paragraph(value, valstyle)]],
            colWidths=[54 * mm])
        inner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), GAIA_SOFT),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (0, 0), 14),
            ("TOPPADDING", (0, 1), (0, 1), 0),
            ("BOTTOMPADDING", (0, -1), (0, -1), 16),
            ("LINEABOVE", (0, 0), (0, 0), 2.5, top_color),
        ]))
        return inner

    kpi_row = Table(
        [[kpi("MONEY IN", naira(total_in), kpi_val_g, GAIA_GREEN),
          "",
          kpi("MONEY OUT", naira(total_out), kpi_val_r, GAIA_RED),
          "",
          kpi("NET CHANGE", naira(net), kpi_val, GAIA_INK)]],
        colWidths=[54 * mm, 6 * mm, 54 * mm, 6 * mm, 54 * mm])
    kpi_row.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(kpi_row)
    story.append(Spacer(1, 20))

    # ---- Transactions ----
    story.append(Paragraph("TRANSACTIONS  ·  " + str(len(rows)) + " RECORDS", sec))
    story.append(Spacer(1, 6))

    head = ["DATE", "TYPE", "DESCRIPTION", "REFERENCE", "IN", "OUT", "STATUS"]
    data = [head]
    for t in rows[:500]:
        amt = float(t.get("amount") or 0)
        direction = t.get("direction")
        typ = (t.get("type") or "").lower()
        desc = t.get("counterparty_name") or "-"
        if typ == "deposit":
            desc = "Wallet top-up"
        elif typ == "withdrawal":
            desc = "To " + (t.get("counterparty_name") or "Bank")
        elif typ == "p2p_send":
            desc = "To " + (t.get("counterparty_name") or "GAIA user")
        elif typ == "p2p_receive":
            desc = "From " + (t.get("counterparty_name") or "GAIA user")
        elif typ == "scan_purchase":
            desc = t.get("counterparty_name") or "Scan purchase"
        elif typ == "bill":
            desc = t.get("counterparty_name") or "Bill payment"

        dt = str(t.get("created_at") or "")[:16].replace("T", " ")
        data.append([
            dt,
            typ.upper()[:12],
            desc[:40],
            (t.get("reference") or "")[:16],
            naira(amt) if direction == "in" else "",
            naira(amt) if direction == "out" else "",
            (t.get("status") or "").upper(),
        ])

    tbl = Table(data,
                colWidths=[22 * mm, 20 * mm, 46 * mm, 24 * mm,
                           22 * mm, 22 * mm, 18 * mm],
                repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), GAIA_INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("FONTSIZE", (0, 1), (-1, -1), 7.5),
        ("ALIGN", (4, 1), (5, -1), "RIGHT"),
        ("ALIGN", (6, 1), (6, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GAIA_SOFT]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, GAIA_LINE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, GAIA_GREEN),
    ]
    for i, t in enumerate(rows[:500], start=1):
        st = (t.get("status") or "").lower()
        if st == "success":
            style.append(("TEXTCOLOR", (6, i), (6, i), GAIA_GREEN_D))
        elif st in ("failed", "rejected"):
            style.append(("TEXTCOLOR", (6, i), (6, i), GAIA_RED))
        elif st in ("pending", "processing"):
            style.append(("TEXTCOLOR", (6, i), (6, i), GAIA_AMBER))
        elif st == "refunded":
            style.append(("TEXTCOLOR", (6, i), (6, i), GAIA_BLUE))
        if t.get("direction") == "in":
            style.append(("TEXTCOLOR", (4, i), (4, i), GAIA_GREEN_D))
            style.append(("FONTNAME", (4, i), (4, i), "Helvetica-Bold"))
        if t.get("direction") == "out":
            style.append(("FONTNAME", (5, i), (5, i), "Helvetica-Bold"))
    tbl.setStyle(TableStyle(style))
    story.append(tbl)
    story.append(Spacer(1, 16))
    story.append(Paragraph(
        "This document is system-generated and requires no signature. "
        "For support: darkmoorltd@gmail.com", foot))

    doc.build(story, canvasmaker=_BrandedCanvas)
    return buf.getvalue()


# =========================================================================
# Statement JPG — instagram-ready story card
# =========================================================================

def _load_font(size, bold=False):
    names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _gradient(size, top, bot):
    w, h = size
    t = _hex_to_rgb(top)
    b = _hex_to_rgb(bot)
    img = Image.new("RGB", size, b)
    d = ImageDraw.Draw(img)
    for y in range(h):
        f = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=(
            int(t[0] + (b[0] - t[0]) * f),
            int(t[1] + (b[1] - t[1]) * f),
            int(t[2] + (b[2] - t[2]) * f),
        ))
    return img


def _gaia_mark(d, x, y, size):
    d.rounded_rectangle([x, y, x + size, y + size],
                        radius=int(size * 0.24),
                        fill=_hex_to_rgb("#00b34a"))
    f = _load_font(int(size * 0.62), bold=True)
    d.text((x + size // 2, y + size // 2), "G", font=f,
           fill=(255, 255, 255), anchor="mm")


def _statement_jpg_bytes(email, wallet, rows, label):
    W, H = 1080, 1920
    img = _gradient((W, H), "#0e2019", "#050b07")
    d = ImageDraw.Draw(img)

    # Top accent
    d.rectangle([0, 0, W, 8], fill=_hex_to_rgb("#5cf4a0"))

    # Brand
    _gaia_mark(d, 80, 80, 96)
    d.text((196, 96), "GAIA", font=_load_font(56, bold=True), fill=(255, 255, 255))
    d.text((196, 158), "WALLET STATEMENT", font=_load_font(22),
           fill=_hex_to_rgb("#7fe9a8"))

    # Date on right
    d.text((W - 80, 100), datetime.utcnow().strftime("%d %b %Y"),
           font=_load_font(26, bold=True), fill=(230, 245, 235), anchor="ra")
    d.text((W - 80, 140), label, font=_load_font(20),
           fill=_hex_to_rgb("#7fe9a8"), anchor="ra")

    # Hero card
    cx0, cy0, cx1, cy1 = 60, 260, W - 60, 760
    d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=44,
                        fill=_hex_to_rgb("#0f2a1e"),
                        outline=_hex_to_rgb("#1f5236"), width=2)

    d.text((cx0 + 44, cy0 + 44), "CURRENT BALANCE",
           font=_load_font(24, bold=True), fill=_hex_to_rgb("#7fe9a8"))
    d.text((cx0 + 44, cy0 + 96), naira(wallet.get("balance") or 0),
           font=_load_font(92, bold=True), fill=(255, 255, 255))

    # Divider inside hero
    d.line([(cx0 + 44, cy0 + 260), (cx1 - 44, cy0 + 260)],
           fill=_hex_to_rgb("#1f5236"), width=2)

    d.text((cx0 + 44, cy0 + 296), "ACCOUNT NUMBER",
           font=_load_font(20, bold=True), fill=_hex_to_rgb("#7fe9a8"))
    d.text((cx0 + 44, cy0 + 328), wallet.get("account_number") or "-",
           font=_load_font(46, bold=True), fill=(255, 255, 255))

    name_str = (wallet.get("account_name") or email or "-")[:34]
    d.text((cx0 + 44, cy0 + 400), name_str.upper(),
           font=_load_font(22, bold=True), fill=(220, 240, 228))
    d.text((cx0 + 44, cy0 + 432), wallet.get("bank_name") or "-",
           font=_load_font(20), fill=_hex_to_rgb("#7fe9a8"))

    # KPI row
    total_in = sum(float(t.get("amount") or 0) for t in rows
                   if t.get("direction") == "in" and t.get("status") == "success")
    total_out = sum(float(t.get("amount") or 0) for t in rows
                    if t.get("direction") == "out" and t.get("status") == "success")
    net = total_in - total_out

    kpi_y0, kpi_y1 = 830, 1060
    gap = 20
    card_w = (W - 120 - 2 * gap) // 3
    cards = [
        ("MONEY IN", naira(total_in), "#0f2a1e", "#1f5236", "#7fe9a8"),
        ("MONEY OUT", naira(total_out), "#2a1014", "#4a1e28", "#ff8b99"),
        ("NET", naira(net), "#101218", "#282a3a", "#c9d2ff"),
    ]
    for i, (lbl, val, bg, br, lblc) in enumerate(cards):
        x0 = 60 + i * (card_w + gap)
        x1 = x0 + card_w
        d.rounded_rectangle([x0, kpi_y0, x1, kpi_y1], radius=32,
                            fill=_hex_to_rgb(bg),
                            outline=_hex_to_rgb(br), width=2)
        d.text((x0 + 26, kpi_y0 + 26), lbl,
               font=_load_font(18, bold=True), fill=_hex_to_rgb(lblc))
        # Value — scale font down if long
        vsize = 40 if len(val) <= 12 else 32 if len(val) <= 16 else 24
        d.text((x0 + 26, kpi_y0 + 74), val,
               font=_load_font(vsize, bold=True), fill=(255, 255, 255))

    # Recent activity
    d.text((60, 1110), "RECENT ACTIVITY",
           font=_load_font(22, bold=True), fill=_hex_to_rgb("#7fe9a8"))
    d.line([(60, 1152), (W - 60, 1152)], fill=_hex_to_rgb("#1f5236"), width=1)

    y = 1180
    for t in rows[:6]:
        amt = float(t.get("amount") or 0)
        direction = t.get("direction")
        typ = (t.get("type") or "").lower()
        if typ == "deposit":
            desc = "Wallet top-up"
        elif typ == "withdrawal":
            desc = "To " + (t.get("counterparty_name") or "Bank")
        elif typ == "p2p_send":
            desc = "To " + (t.get("counterparty_name") or "GAIA user")
        elif typ == "p2p_receive":
            desc = "From " + (t.get("counterparty_name") or "GAIA user")
        elif typ == "scan_purchase":
            desc = t.get("counterparty_name") or "Scan purchase"
        else:
            desc = typ.upper() or "Transaction"

        amt_color = "#7fe9a8" if direction == "in" else "#ff8b99"
        sign = "+" if direction == "in" else "-"

        dt = str(t.get("created_at") or "")[:16].replace("T", " ")
        d.text((60, y), dt, font=_load_font(18), fill=_hex_to_rgb("#5c8a72"))
        d.text((60, y + 26), desc[:42], font=_load_font(22, bold=True),
               fill=(230, 245, 235))
        d.text((W - 60, y + 12), sign + naira(amt),
               font=_load_font(24, bold=True), fill=_hex_to_rgb(amt_color),
               anchor="ra")
        d.line([(60, y + 72), (W - 60, y + 72)],
               fill=_hex_to_rgb("#133024"), width=1)
        y += 90
        if y > 1740:
            break

    # Footer
    d.rectangle([0, H - 90, W, H], fill=_hex_to_rgb("#0a1a11"))
    d.rectangle([0, H - 94, W, H - 90], fill=_hex_to_rgb("#00b34a"))
    d.text((60, H - 58), "Darkmoor Ltd", font=_load_font(20, bold=True),
           fill=_hex_to_rgb("#7fe9a8"))
    d.text((60, H - 34), "darkmoorltd@gmail.com",
           font=_load_font(16), fill=_hex_to_rgb("#5c8a72"))
    d.text((W - 60, H - 50), "gaia", font=_load_font(28, bold=True),
           fill=_hex_to_rgb("#7fe9a8"), anchor="ra")

    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True, progressive=True)
    return out.getvalue()


# =========================================================================
# Endpoints — statement
# =========================================================================

@router.get("/wallet/statement/pdf")
async def statement_pdf(
    authorization: str = Header(None),
    days: int = 90,
    date_from: str = None,
    date_to: str = None,
):
    user = auth_user(authorization)
    uid = user["sub"]
    email = user.get("email") or ""
    start_iso, end_iso, label = _range_from_params(days, date_from, date_to)
    wallet = _fetch_wallet(uid)
    rows = _fetch_rows(uid, start_iso, end_iso)
    pdf = _statement_pdf_bytes(uid, email, wallet, rows, label)
    fname = "gaia-statement-" + uid[:8] + "-" + datetime.utcnow().strftime("%Y%m%d") + ".pdf"
    return Response(
        content=pdf, media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="' + fname + '"',
            "Cache-Control": "private, max-age=60",
        },
    )


@router.get("/wallet/statement/jpg")
async def statement_jpg(
    authorization: str = Header(None),
    days: int = 90,
    date_from: str = None,
    date_to: str = None,
):
    user = auth_user(authorization)
    uid = user["sub"]
    email = user.get("email") or ""
    start_iso, end_iso, label = _range_from_params(days, date_from, date_to)
    wallet = _fetch_wallet(uid)
    rows = _fetch_rows(uid, start_iso, end_iso)
    jpg = _statement_jpg_bytes(email, wallet, rows, label)
    fname = "gaia-statement-" + uid[:8] + "-" + datetime.utcnow().strftime("%Y%m%d") + ".jpg"
    return Response(
        content=jpg, media_type="image/jpeg",
        headers={
            "Content-Disposition": 'attachment; filename="' + fname + '"',
            "Cache-Control": "private, max-age=60",
        },
    )


# =========================================================================
# Receipt — PDF + JPG
# =========================================================================

def _receipt_pdf_bytes(email, tx, wallet):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=24 * mm, rightMargin=24 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title="GAIA Receipt", author="Darkmoor Ltd",
    )
    ss = getSampleStyleSheet()
    kicker = ParagraphStyle("k", parent=ss["Normal"], fontName="Helvetica-Bold",
                            fontSize=9, leading=11, textColor=GAIA_GREEN_D)
    title = ParagraphStyle("t", parent=ss["Normal"], fontName="Helvetica-Bold",
                           fontSize=26, leading=30, textColor=GAIA_INK,
                           spaceBefore=6, spaceAfter=2)
    sub = ParagraphStyle("s", parent=ss["Normal"], fontName="Helvetica",
                         fontSize=9, leading=12, textColor=GAIA_MUTED)
    amt_lbl = ParagraphStyle("al", parent=ss["Normal"], fontName="Helvetica-Bold",
                             fontSize=8.5, leading=10,
                             textColor=colors.HexColor("#bff3d3"),
                             alignment=TA_CENTER)
    amt_val = ParagraphStyle("av", parent=ss["Normal"], fontName="Helvetica-Bold",
                             fontSize=38, leading=44, textColor=colors.white,
                             alignment=TA_CENTER, spaceBefore=6)
    status_lbl = ParagraphStyle("sl", parent=ss["Normal"], fontName="Helvetica-Bold",
                                fontSize=8, leading=10, textColor=GAIA_MUTED,
                                alignment=TA_CENTER)
    kv_l = ParagraphStyle("kvl", parent=ss["Normal"], fontName="Helvetica-Bold",
                          fontSize=7.5, leading=9, textColor=GAIA_MUTED)
    kv_v = ParagraphStyle("kvv", parent=ss["Normal"], fontName="Helvetica-Bold",
                          fontSize=10.5, leading=14, textColor=GAIA_INK)
    foot = ParagraphStyle("f", parent=ss["Normal"], fontName="Helvetica",
                          fontSize=7, leading=10, textColor=GAIA_MUTED,
                          alignment=TA_CENTER)

    story = []
    story.append(Paragraph("GAIA · DARKMOOR LTD", kicker))
    story.append(Paragraph("Transaction Receipt", title))
    story.append(Paragraph(
        "Receipt " + str(tx.get("receipt_number") or "-") + " · " +
        str(tx.get("created_at") or "")[:19].replace("T", " ") + " UTC", sub))
    story.append(Spacer(1, 18))

    sign = "+" if tx.get("direction") == "in" else "-"
    direction_label = "CREDITED" if tx.get("direction") == "in" else "DEBITED"
    hero_inner = Table(
        [[Paragraph(direction_label, amt_lbl)],
         [Paragraph(sign + naira(tx.get("amount") or 0), amt_val)]],
        colWidths=[162 * mm])
    hero_inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 20),
        ("RIGHTPADDING", (0, 0), (-1, -1), 20),
        ("TOPPADDING", (0, 0), (0, 0), 22),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("BOTTOMPADDING", (0, -1), (0, -1), 24),
    ]))
    hero = Table([[hero_inner]], colWidths=[162 * mm])
    hero.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GAIA_GREEN),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(hero)
    story.append(Spacer(1, 10))

    status_str = str(tx.get("status") or "").upper()
    status_color = {
        "SUCCESS": GAIA_GREEN_D, "FAILED": GAIA_RED,
        "PENDING": GAIA_AMBER, "PROCESSING": GAIA_AMBER,
        "REFUNDED": GAIA_BLUE,
    }.get(status_str, GAIA_MUTED)

    badge_inner = Table([[Paragraph("STATUS", status_lbl)],
                         [Paragraph(status_str, ParagraphStyle(
                             "sb", parent=ss["Normal"], fontName="Helvetica-Bold",
                             fontSize=14, leading=18, textColor=status_color,
                             alignment=TA_CENTER, spaceBefore=2))]],
                        colWidths=[60 * mm])
    badge_inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (0, 0), 10),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("BOTTOMPADDING", (0, -1), (0, -1), 12),
        ("LINEABOVE", (0, 0), (0, 0), 2.5, status_color),
        ("BACKGROUND", (0, 0), (-1, -1), GAIA_SOFT),
    ]))
    badge_wrap = Table([["", badge_inner, ""]],
                       colWidths=[51 * mm, 60 * mm, 51 * mm])
    badge_wrap.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(badge_wrap)
    story.append(Spacer(1, 20))

    lines = [
        ("RECEIPT NUMBER", str(tx.get("receipt_number") or "-")),
        ("REFERENCE", str(tx.get("reference") or "-")),
        ("DATE & TIME", str(tx.get("created_at") or "")[:19].replace("T", " ") + " UTC"),
        ("TRANSACTION TYPE", str(tx.get("type") or "").upper()),
        ("COUNTERPARTY", str(tx.get("counterparty_name") or "-")),
        ("COUNTERPARTY ACCOUNT", str(tx.get("counterparty_acct") or "-")),
        ("WALLET ACCOUNT", str(wallet.get("account_number") or "-")),
        ("ACCOUNT NAME", str(wallet.get("account_name") or "-")),
        ("BALANCE AFTER", naira(tx.get("balance_after") or wallet.get("balance") or 0)),
    ]
    rows_kv = [[Paragraph(k, kv_l), Paragraph(v, kv_v)] for k, v in lines]
    tbl = Table(rows_kv, colWidths=[58 * mm, 104 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), GAIA_SOFT),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, GAIA_LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 22))
    story.append(Paragraph(
        "System-generated receipt · Save for your records · " + (email or ""),
        foot))

    doc.build(story, canvasmaker=_BrandedCanvas)
    return buf.getvalue()


def _receipt_jpg_bytes(email, tx, wallet):
    W, H = 1080, 1350
    img = _gradient((W, H), "#0e2019", "#050b07")
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 8], fill=_hex_to_rgb("#5cf4a0"))

    _gaia_mark(d, 80, 70, 88)
    d.text((192, 84), "GAIA", font=_load_font(52, bold=True), fill=(255, 255, 255))
    d.text((192, 144), "TRANSACTION RECEIPT",
           font=_load_font(20, bold=True), fill=_hex_to_rgb("#7fe9a8"))

    d.text((W - 80, 100), str(tx.get("receipt_number") or "-")[:30],
           font=_load_font(20, bold=True), fill=(220, 240, 228), anchor="ra")
    d.text((W - 80, 134),
           str(tx.get("created_at") or "")[:10],
           font=_load_font(20), fill=_hex_to_rgb("#7fe9a8"), anchor="ra")

    # Amount block
    y0 = 240
    y1 = 620
    d.rounded_rectangle([60, y0, W - 60, y1], radius=44,
                        fill=_hex_to_rgb("#0f2a1e"),
                        outline=_hex_to_rgb("#1f5236"), width=2)

    direction = tx.get("direction")
    sign = "+" if direction == "in" else "-"
    d.text((W // 2, y0 + 60),
           ("CREDITED" if direction == "in" else "DEBITED"),
           font=_load_font(24, bold=True),
           fill=_hex_to_rgb("#7fe9a8"), anchor="mm")

    amount_str = sign + naira(tx.get("amount") or 0)
    asize = 84 if len(amount_str) <= 16 else 68 if len(amount_str) <= 20 else 54
    amount_color = "#7fe9a8" if direction == "in" else "#ffffff"
    d.text((W // 2, y0 + 180), amount_str,
           font=_load_font(asize, bold=True),
           fill=_hex_to_rgb(amount_color), anchor="mm")

    # Status pill
    status_str = (tx.get("status") or "").upper()
    st_map = {
        "SUCCESS": "#00b34a", "FAILED": "#e5344b",
        "PENDING": "#c58b00", "PROCESSING": "#c58b00",
        "REFUNDED": "#3c8fd1",
    }
    st_col = st_map.get(status_str, "#6b7772")
    pill_w = 320
    pill_h = 62
    px = (W - pill_w) // 2
    py = y1 - 90
    d.rounded_rectangle([px, py, px + pill_w, py + pill_h], radius=31,
                        fill=_hex_to_rgb(st_col))
    d.text((px + pill_w // 2, py + pill_h // 2), status_str,
           font=_load_font(24, bold=True), fill=(255, 255, 255), anchor="mm")

    # Details grid
    y = 680
    d.text((60, y), "TRANSACTION DETAILS",
           font=_load_font(20, bold=True), fill=_hex_to_rgb("#7fe9a8"))
    d.line([(60, y + 34), (W - 60, y + 34)],
           fill=_hex_to_rgb("#1f5236"), width=1)
    y += 60

    details = [
        ("REFERENCE", str(tx.get("reference") or "-")[:36]),
        ("TYPE", str(tx.get("type") or "").upper()),
        ("COUNTERPARTY", str(tx.get("counterparty_name") or "-")[:36]),
        ("ACCOUNT", str(tx.get("counterparty_acct") or "-")[:24]),
        ("WALLET ACCT", str(wallet.get("account_number") or "-")),
        ("BALANCE AFTER", naira(tx.get("balance_after") or wallet.get("balance") or 0)),
    ]
    for k, v in details:
        d.text((60, y), k, font=_load_font(18, bold=True),
               fill=_hex_to_rgb("#5c8a72"))
        d.text((60, y + 26), v, font=_load_font(24, bold=True),
               fill=(255, 255, 255))
        y += 82

    # Footer
    d.rectangle([0, H - 90, W, H], fill=_hex_to_rgb("#0a1a11"))
    d.rectangle([0, H - 94, W, H - 90], fill=_hex_to_rgb("#00b34a"))
    d.text((60, H - 58), "Darkmoor Ltd", font=_load_font(20, bold=True),
           fill=_hex_to_rgb("#7fe9a8"))
    d.text((60, H - 34), email or "", font=_load_font(16),
           fill=_hex_to_rgb("#5c8a72"))
    d.text((W - 60, H - 50), "gaia", font=_load_font(28, bold=True),
           fill=_hex_to_rgb("#7fe9a8"), anchor="ra")

    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True, progressive=True)
    return out.getvalue()


@router.get("/wallet/receipt/{reference}/pdf")
async def receipt_pdf(reference: str, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    email = user.get("email") or ""
    s = svc()
    r = s.table("wallet_transactions").select("*").eq(
        "reference", reference).eq("user_id", uid).limit(1).execute()
    if not r.data:
        raise HTTPException(404, "Receipt not found")
    tx = r.data[0]
    wallet = _fetch_wallet(uid)
    pdf = _receipt_pdf_bytes(email, tx, wallet)
    fname = "gaia-receipt-" + reference[:16] + ".pdf"
    return Response(
        content=pdf, media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="' + fname + '"',
            "Cache-Control": "private, max-age=300",
        },
    )


@router.get("/wallet/receipt/{reference}/jpg")
async def receipt_jpg(reference: str, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    email = user.get("email") or ""
    s = svc()
    r = s.table("wallet_transactions").select("*").eq(
        "reference", reference).eq("user_id", uid).limit(1).execute()
    if not r.data:
        raise HTTPException(404, "Receipt not found")
    tx = r.data[0]
    wallet = _fetch_wallet(uid)
    jpg = _receipt_jpg_bytes(email, tx, wallet)
    fname = "gaia-receipt-" + reference[:16] + ".jpg"
    return Response(
        content=jpg, media_type="image/jpeg",
        headers={
            "Content-Disposition": 'attachment; filename="' + fname + '"',
            "Cache-Control": "private, max-age=300",
        },
    )
