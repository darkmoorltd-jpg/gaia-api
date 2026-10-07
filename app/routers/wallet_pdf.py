# app/routers/wallet_pdf.py
# Badass PDF + JPG statement/receipt generator
import os
import io
from datetime import datetime, timedelta
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, KeepTogether,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4 as A4_SIZE
from PIL import Image, ImageDraw, ImageFont
from app.services.auth import verify_supabase_token
from supabase import create_client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Brand palette
GAIA_GREEN   = colors.HexColor("#00b34a")
GAIA_GREEN_D = colors.HexColor("#009e52")
GAIA_DARK    = colors.HexColor("#0b0f0d")
GAIA_INK     = colors.HexColor("#111814")
GAIA_MUTED   = colors.HexColor("#6b7772")
GAIA_LINE    = colors.HexColor("#e4eae7")
GAIA_SOFT    = colors.HexColor("#f4f9f6")
GAIA_RED     = colors.HexColor("#e5344b")


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
    """Return (start_iso, end_iso, label)."""
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
# Statement PDF — dark green hero, kpi tiles, elegant table
# =========================================================================

class _PageNumberedCanvas(rl_canvas.Canvas):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for page in self._saved:
            self.__dict__.update(page)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total):
        w, h = A4_SIZE
        self.setFillColor(GAIA_MUTED)
        self.setFont("Helvetica", 7)
        self.drawString(18 * mm, 12 * mm, "GAIA Wallet - Darkmoor Ltd")
        self.drawRightString(w - 18 * mm, 12 * mm,
                             "Page " + str(self._pageNumber) + " of " + str(total))


def _pdf_header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(GAIA_GREEN)
    canvas.rect(0, A4[1] - 8 * mm, A4[0], 8 * mm, fill=1, stroke=0)
    canvas.restoreState()


def _statement_pdf_bytes(uid, email, wallet, rows, label, start_iso, end_iso):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=22 * mm, bottomMargin=20 * mm,
        title="GAIA Wallet Statement",
        author="Darkmoor Ltd",
        subject="Wallet Statement",
    )

    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], textColor=GAIA_DARK,
                        fontSize=22, spaceAfter=2, leading=26)
    kicker = ParagraphStyle("kick", parent=ss["Normal"], textColor=GAIA_GREEN,
                            fontSize=9, leading=11, spaceAfter=4,
                            fontName="Helvetica-Bold")
    sub = ParagraphStyle("sub", parent=ss["Normal"], textColor=GAIA_MUTED,
                         fontSize=9, leading=12)
    right = ParagraphStyle("right", parent=ss["Normal"], alignment=TA_RIGHT,
                           fontSize=9, textColor=GAIA_MUTED)
    biglabel = ParagraphStyle("biglabel", parent=ss["Normal"],
                              textColor=colors.white, fontSize=9, leading=11,
                              fontName="Helvetica-Bold")
    bigvalue = ParagraphStyle("bigvalue", parent=ss["Normal"],
                              textColor=colors.white, fontSize=26, leading=30,
                              fontName="Helvetica-Bold")
    kpi_label = ParagraphStyle("kl", parent=ss["Normal"], fontSize=8,
                               textColor=GAIA_MUTED, leading=10)
    kpi_value = ParagraphStyle("kv", parent=ss["Normal"], fontSize=16,
                               textColor=GAIA_INK, leading=20,
                               fontName="Helvetica-Bold")
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=7,
                           textColor=GAIA_MUTED, leading=10)

    story = []
    story.append(Paragraph("GAIA WALLET", kicker))
    story.append(Paragraph("Account Statement", h1))
    story.append(Paragraph(label, sub))
    story.append(Spacer(1, 10))

    # Account meta table
    acc_rows = [
        ["Account Holder", wallet.get("account_name") or email or "-"],
        ["Account Number", wallet.get("account_number") or "-"],
        ["Bank", wallet.get("bank_name") or "-"],
        ["Generated", datetime.utcnow().strftime("%d %b %Y, %H:%M UTC")],
    ]
    acc_tbl = Table(acc_rows, colWidths=[40 * mm, 132 * mm])
    acc_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), GAIA_SOFT),
        ("TEXTCOLOR", (0, 0), (0, -1), GAIA_MUTED),
        ("TEXTCOLOR", (1, 0), (1, -1), GAIA_INK),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, GAIA_LINE),
    ]))
    story.append(acc_tbl)
    story.append(Spacer(1, 14))

    # Balance hero band
    bal_tbl = Table(
        [[Paragraph("CURRENT BALANCE", biglabel)],
         [Paragraph(naira(wallet.get("balance") or 0), bigvalue)]],
        colWidths=[172 * mm])
    bal_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GAIA_GREEN),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (0, 0), 14),
        ("BOTTOMPADDING", (0, -1), (0, -1), 18),
        ("TOPPADDING", (0, -1), (0, -1), 0),
    ]))
    story.append(bal_tbl)
    story.append(Spacer(1, 12))

    total_in = sum(float(t.get("amount") or 0) for t in rows
                   if t.get("direction") == "in" and t.get("status") == "success")
    total_out = sum(float(t.get("amount") or 0) for t in rows
                    if t.get("direction") == "out" and t.get("status") == "success")
    net = total_in - total_out

    kpi_data = [[
        [Paragraph("MONEY IN", kpi_label), Paragraph(naira(total_in), kpi_value)],
        [Paragraph("MONEY OUT", kpi_label), Paragraph(naira(total_out), kpi_value)],
        [Paragraph("NET CHANGE", kpi_label), Paragraph(naira(net), kpi_value)],
    ]]
    kpi_tbl = Table(kpi_data, colWidths=[57 * mm, 57 * mm, 58 * mm])
    kpi_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#e6f7ed")),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#fdecee")),
        ("BACKGROUND", (2, 0), (2, 0), GAIA_SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LINEAFTER", (0, 0), (1, 0), 0.5, colors.white),
    ]))
    story.append(kpi_tbl)
    story.append(Spacer(1, 18))

    # Transactions table
    head = ["DATE", "TYPE", "DESCRIPTION", "REFERENCE", "IN", "OUT", "STATUS"]
    data = [head]
    for t in rows[:300]:
        amt = float(t.get("amount") or 0)
        direction = t.get("direction")
        desc = t.get("counterparty_name") or "-"
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
        elif typ == "bill":
            desc = t.get("counterparty_name") or "Bill payment"

        dt = str(t.get("created_at") or "")[:16].replace("T", " ")
        data.append([
            dt,
            typ.upper()[:12],
            desc[:32],
            (t.get("reference") or "")[:14],
            naira(amt) if direction == "in" else "",
            naira(amt) if direction == "out" else "",
            (t.get("status") or "").upper(),
        ])

    tbl = Table(data,
                colWidths=[24 * mm, 22 * mm, 44 * mm, 24 * mm,
                           22 * mm, 22 * mm, 20 * mm],
                repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), GAIA_DARK),
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
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, GAIA_SOFT]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, GAIA_LINE),
    ]
    for i, t in enumerate(rows[:300], start=1):
        st = (t.get("status") or "").lower()
        if st == "success":
            style.append(("TEXTCOLOR", (6, i), (6, i), GAIA_GREEN_D))
        elif st in ("failed", "rejected"):
            style.append(("TEXTCOLOR", (6, i), (6, i), GAIA_RED))
        elif st in ("pending", "processing"):
            style.append(("TEXTCOLOR", (6, i), (6, i),
                          colors.HexColor("#c58b00")))
        if t.get("direction") == "in":
            style.append(("TEXTCOLOR", (4, i), (4, i), GAIA_GREEN_D))
        if t.get("direction") == "out":
            style.append(("TEXTCOLOR", (5, i), (5, i), GAIA_INK))
    tbl.setStyle(TableStyle(style))
    story.append(tbl)
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "This statement is system-generated. For support: darkmoorltd@gmail.com",
        small))

    doc.build(story, onFirstPage=_pdf_header_footer,
              onLaterPages=_pdf_header_footer,
              canvasmaker=_PageNumberedCanvas)
    return buf.getvalue()


# =========================================================================
# Statement JPG — shareable card for WhatsApp
# =========================================================================

def _load_font(size, bold=False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _gradient(size, top_hex, bot_hex):
    w, h = size
    t = _hex_to_rgb(top_hex)
    b = _hex_to_rgb(bot_hex)
    img = Image.new("RGB", size, b)
    dr = ImageDraw.Draw(img)
    for y in range(h):
        f = y / max(1, h - 1)
        r = int(t[0] + (b[0] - t[0]) * f)
        g = int(t[1] + (b[1] - t[1]) * f)
        bl = int(t[2] + (b[2] - t[2]) * f)
        dr.line([(0, y), (w, y)], fill=(r, g, bl))
    return img


def _statement_jpg_bytes(email, wallet, rows, label):
    W, H = 1080, 1350
    img = _gradient((W, H), "#0d1e15", "#08120c")
    d = ImageDraw.Draw(img)

    # Green header band
    d.rectangle([0, 0, W, 240], fill=_hex_to_rgb("#00b34a"))
    d.rectangle([0, 0, W, 8], fill=_hex_to_rgb("#5cf4a0"))

    # GAIA logo text
    f_logo = _load_font(56, bold=True)
    f_kicker = _load_font(24)
    f_label = _load_font(26)
    f_big = _load_font(84, bold=True)
    f_val = _load_font(42, bold=True)
    f_val_sm = _load_font(32, bold=True)
    f_small = _load_font(22)

    d.text((60, 40), "GAIA", font=f_logo, fill=(255, 255, 255))
    d.text((60, 110), "WALLET STATEMENT", font=f_kicker, fill=(230, 250, 240))
    d.text((60, 148), label, font=f_small, fill=(210, 240, 220))
    d.text((60, 182), datetime.utcnow().strftime("%d %b %Y, %H:%M UTC"),
           font=f_small, fill=(210, 240, 220))

    # Balance card
    y = 280
    d.text((60, y), "CURRENT BALANCE", font=f_kicker, fill=(150, 200, 170))
    y += 40
    d.text((60, y), naira(wallet.get("balance") or 0), font=f_big, fill=(255, 255, 255))

    # Account numbers
    y += 130
    acct = wallet.get("account_number") or "-"
    accname = (wallet.get("account_name") or email or "-")[:40]
    bank = wallet.get("bank_name") or "-"
    d.text((60, y), "ACCOUNT NUMBER", font=f_kicker, fill=(150, 200, 170))
    d.text((60, y + 34), acct, font=f_val, fill=(255, 255, 255))
    d.text((60, y + 96), accname, font=f_small, fill=(180, 220, 200))
    d.text((60, y + 126), bank, font=f_small, fill=(180, 220, 200))

    # Divider
    y += 190
    d.line([(60, y), (W - 60, y)], fill=(60, 90, 70), width=2)

    # Three KPI tiles
    total_in = sum(float(t.get("amount") or 0) for t in rows
                   if t.get("direction") == "in" and t.get("status") == "success")
    total_out = sum(float(t.get("amount") or 0) for t in rows
                    if t.get("direction") == "out" and t.get("status") == "success")
    net = total_in - total_out

    y += 30
    tile_w = (W - 120 - 40) // 3
    for i, (lab, val, col) in enumerate([
        ("MONEY IN", naira(total_in), "#00b34a"),
        ("MONEY OUT", naira(total_out), "#e5344b"),
        ("NET CHANGE", naira(net), "#ffffff"),
    ]):
        x = 60 + i * (tile_w + 20)
        d.rounded_rectangle(
            [x, y, x + tile_w, y + 180],
            radius=20, fill=(20, 32, 26), outline=(40, 60, 50), width=1)
        d.text((x + 20, y + 20), lab, font=f_kicker, fill=(140, 180, 160))
        d.text((x + 20, y + 70), val, font=f_val_sm,
               fill=_hex_to_rgb(col))
    y += 220

    # Txn count
    d.text((60, y), "TRANSACTIONS IN PERIOD", font=f_kicker, fill=(150, 200, 170))
    y += 36
    d.text((60, y), str(len(rows)), font=f_val, fill=(255, 255, 255))

    # Footer
    d.line([(60, H - 120), (W - 60, H - 120)], fill=(60, 90, 70), width=2)
    d.text((60, H - 100), "Darkmoor Ltd - Powered by GAIA", font=f_small,
           fill=(140, 180, 160))
    d.text((60, H - 70), "darkmoorltd@gmail.com", font=f_small,
           fill=(140, 180, 160))
    d.text((W - 260, H - 70), "gaia.app", font=f_small, fill=(90, 200, 130))

    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


# =========================================================================
# Endpoints — Statement
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
    pdf = _statement_pdf_bytes(uid, email, wallet, rows, label, start_iso, end_iso)
    fname = "gaia-statement-" + uid[:8] + "-" + datetime.utcnow().strftime("%Y%m%d") + ".pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
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
        content=jpg,
        media_type="image/jpeg",
        headers={
            "Content-Disposition": 'attachment; filename="' + fname + '"',
            "Cache-Control": "private, max-age=60",
        },
    )


# =========================================================================
# Receipt PDF + JPG
# =========================================================================

def _receipt_pdf_bytes(email, tx, wallet):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=24 * mm, rightMargin=24 * mm,
        topMargin=24 * mm, bottomMargin=20 * mm,
    )
    ss = getSampleStyleSheet()
    kicker = ParagraphStyle("k", parent=ss["Normal"], textColor=GAIA_GREEN,
                            fontSize=10, leading=12, spaceAfter=2,
                            fontName="Helvetica-Bold")
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], textColor=GAIA_DARK,
                        fontSize=22, leading=26, spaceAfter=4)
    amt_lbl = ParagraphStyle("al", parent=ss["Normal"], fontSize=9,
                             textColor=GAIA_MUTED, alignment=TA_CENTER,
                             leading=11)
    amt_val = ParagraphStyle("av", parent=ss["Normal"], fontSize=34,
                             textColor=GAIA_GREEN, alignment=TA_CENTER,
                             leading=40, fontName="Helvetica-Bold")
    small = ParagraphStyle("s", parent=ss["Normal"], fontSize=7,
                           textColor=GAIA_MUTED, alignment=TA_CENTER, leading=10)

    story = []
    story.append(Paragraph("GAIA WALLET", kicker))
    story.append(Paragraph("Transaction Receipt", h1))
    story.append(Spacer(1, 18))

    amount_line = ("+" if tx.get("direction") == "in" else "-") + naira(tx.get("amount") or 0)
    band = Table(
        [[Paragraph(tx.get("direction") == "in" and "CREDITED" or "DEBITED", amt_lbl)],
         [Paragraph(amount_line, amt_val)]],
        colWidths=[162 * mm])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eafaf0")),
        ("BOX", (0, 0), (-1, -1), 1, GAIA_GREEN),
        ("TOPPADDING", (0, 0), (0, 0), 16),
        ("BOTTOMPADDING", (0, -1), (0, -1), 20),
    ]))
    story.append(band)
    story.append(Spacer(1, 16))

    lines = [
        ["Receipt Number", str(tx.get("receipt_number") or "-")],
        ["Reference", str(tx.get("reference") or "-")],
        ["Date & Time", str(tx.get("created_at") or "")[:19].replace("T", " ") + " UTC"],
        ["Type", str(tx.get("type") or "").upper()],
        ["Status", str(tx.get("status") or "").upper()],
        ["Counterparty", str(tx.get("counterparty_name") or "-")],
        ["Counterparty Account", str(tx.get("counterparty_acct") or "-")],
        ["Wallet Account", str(wallet.get("account_number") or "-")],
        ["Account Name", str(wallet.get("account_name") or "-")],
        ["Balance After", naira(tx.get("balance_after") or wallet.get("balance") or 0)],
    ]
    tbl = Table(lines, colWidths=[58 * mm, 104 * mm])
    st = [
        ("BACKGROUND", (0, 0), (0, -1), GAIA_SOFT),
        ("TEXTCOLOR", (0, 0), (0, -1), GAIA_MUTED),
        ("TEXTCOLOR", (1, 0), (1, -1), GAIA_INK),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, GAIA_LINE),
    ]
    tbl.setStyle(TableStyle(st))
    story.append(tbl)
    story.append(Spacer(1, 20))
    story.append(Paragraph(
        "System-generated receipt. Save for your records. " + (email or ""),
        small))

    doc.build(story)
    return buf.getvalue()


def _receipt_jpg_bytes(email, tx, wallet):
    W, H = 1080, 1350
    img = _gradient((W, H), "#0d1e15", "#08120c")
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 240], fill=_hex_to_rgb("#00b34a"))
    d.rectangle([0, 0, W, 8], fill=_hex_to_rgb("#5cf4a0"))

    f_logo = _load_font(56, bold=True)
    f_kicker = _load_font(24)
    f_small = _load_font(22)
    f_big = _load_font(120, bold=True)
    f_val = _load_font(36, bold=True)
    f_row_lbl = _load_font(22)
    f_row_val = _load_font(28, bold=True)
    f_badge = _load_font(26, bold=True)

    d.text((60, 40), "GAIA", font=f_logo, fill=(255, 255, 255))
    d.text((60, 110), "TRANSACTION RECEIPT", font=f_kicker, fill=(230, 250, 240))
    d.text((60, 150), str(tx.get("receipt_number") or "")[:40], font=f_small,
           fill=(210, 240, 220))

    # Amount block
    y = 300
    direction = tx.get("direction")
    sign = "+" if direction == "in" else "-"
    d.text((60, y), ("CREDITED" if direction == "in" else "DEBITED"),
           font=f_kicker, fill=(150, 200, 170))
    y += 40
    amt_col = _hex_to_rgb("#5cf4a0") if direction == "in" else (255, 255, 255)
    d.text((60, y), sign + naira(tx.get("amount") or 0), font=f_big, fill=amt_col)

    # Status badge
    y += 180
    st = (tx.get("status") or "").lower()
    st_col = {"success": "#00b34a", "failed": "#e5344b",
              "pending": "#c58b00", "processing": "#c58b00",
              "refunded": "#3c8fd1"}.get(st, "#667")
    d.rounded_rectangle([60, y, 60 + 260, y + 60], radius=16,
                        fill=_hex_to_rgb(st_col))
    d.text((80, y + 14), st.upper(), font=f_badge, fill=(255, 255, 255))

    # Divider
    y += 110
    d.line([(60, y), (W - 60, y)], fill=(60, 90, 70), width=2)

    # Detail rows
    y += 30
    details = [
        ("Date & Time", str(tx.get("created_at") or "")[:19].replace("T", " ")),
        ("Type", str(tx.get("type") or "").upper()),
        ("Counterparty", str(tx.get("counterparty_name") or "-")[:32]),
        ("Account", str(tx.get("counterparty_acct") or "-")[:24]),
        ("Reference", str(tx.get("reference") or "")[:28]),
        ("Wallet Account", str(wallet.get("account_number") or "-")),
        ("Balance After", naira(tx.get("balance_after") or wallet.get("balance") or 0)),
    ]
    for k, v in details:
        d.text((60, y), k, font=f_row_lbl, fill=(150, 200, 170))
        d.text((60, y + 30), str(v), font=f_row_val, fill=(255, 255, 255))
        y += 90

    # Footer
    d.line([(60, H - 120), (W - 60, H - 120)], fill=(60, 90, 70), width=2)
    d.text((60, H - 100), "Darkmoor Ltd - Powered by GAIA", font=f_small,
           fill=(140, 180, 160))
    d.text((60, H - 70), email or "", font=f_small, fill=(140, 180, 160))

    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


@router.get("/wallet/receipt/{reference}/pdf")
async def receipt_pdf(reference: str, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]
    email = user.get("email") or ""
    s = svc()
    r = s.table("wallet_transactions").select("*").eq("reference", reference).eq(
        "user_id", uid).limit(1).execute()
    if not r.data:
        raise HTTPException(404, "Receipt not found")
    tx = r.data[0]
    wallet = _fetch_wallet(uid)
    pdf = _receipt_pdf_bytes(email, tx, wallet)
    fname = "gaia-receipt-" + reference[:16] + ".pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
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
    r = s.table("wallet_transactions").select("*").eq("reference", reference).eq(
        "user_id", uid).limit(1).execute()
    if not r.data:
        raise HTTPException(404, "Receipt not found")
    tx = r.data[0]
    wallet = _fetch_wallet(uid)
    jpg = _receipt_jpg_bytes(email, tx, wallet)
    fname = "gaia-receipt-" + reference[:16] + ".jpg"
    return Response(
        content=jpg,
        media_type="image/jpeg",
        headers={
            "Content-Disposition": 'attachment; filename="' + fname + '"',
            "Cache-Control": "private, max-age=300",
        },
    )
