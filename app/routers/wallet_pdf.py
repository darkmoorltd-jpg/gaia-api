# app/routers/wallet_pdf.py
# PDF generation for wallet statement and receipts.
import os
import io
from datetime import datetime
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER
from app.services.auth import verify_supabase_token
from supabase import create_client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

GREEN = colors.HexColor("#00b34a")
DARK = colors.HexColor("#0a0e0c")
LIGHT = colors.HexColor("#f4f6f8")


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


def fmt_naira(n):
    try:
        return "N" + f"{float(n):,.2f}"
    except Exception:
        return "N0.00"


@router.get("/wallet/statement/pdf")
async def statement_pdf(authorization: str = Header(None), days: int = 90):
    user = auth_user(authorization)
    uid = user["sub"]
    email = user.get("email") or ""

    s = svc()
    w = s.table("farmer_wallets").select(
        "balance,account_number,account_name,bank_name"
    ).eq("user_id", uid).limit(1).execute()
    wallet = w.data[0] if w.data else {}

    txns = s.table("wallet_transactions").select("*").eq(
        "user_id", uid
    ).order("created_at", desc=True).limit(500).execute()
    rows = txns.data or []

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title="GAIA Wallet Statement",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=DARK, fontSize=20, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=colors.HexColor("#666"), fontSize=10)
    right = ParagraphStyle("right", parent=styles["Normal"], alignment=TA_RIGHT, fontSize=10)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#888"))

    story = []
    story.append(Paragraph("GAIA WALLET STATEMENT", h1))
    story.append(Paragraph("Powered by Darkmoor Ltd", sub))
    story.append(Spacer(1, 8))

    meta = [
        ["Account Holder:", wallet.get("account_name") or email],
        ["Account Number:", wallet.get("account_number") or "-"],
        ["Bank:", wallet.get("bank_name") or "-"],
        ["Current Balance:", fmt_naira(wallet.get("balance") or 0)],
        ["Statement Period:", f"Last {days} days"],
        ["Generated:", datetime.utcnow().strftime("%d %b %Y %H:%M UTC")],
    ]
    meta_tbl = Table(meta, colWidths=[42 * mm, 130 * mm])
    meta_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), LIGHT),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#333")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 14))

    total_in = sum(float(t.get("amount") or 0) for t in rows if t.get("direction") == "in" and t.get("status") == "success")
    total_out = sum(float(t.get("amount") or 0) for t in rows if t.get("direction") == "out" and t.get("status") == "success")

    sums = [
        ["Money In", fmt_naira(total_in)],
        ["Money Out", fmt_naira(total_out)],
        ["Net", fmt_naira(total_in - total_out)],
    ]
    sums_tbl = Table(sums, colWidths=[42 * mm, 42 * mm, 88 * mm])
    sums_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e8f5e9")),
        ("TEXTCOLOR", (1, 0), (1, -1), GREEN),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(sums_tbl)
    story.append(Spacer(1, 14))

    table_data = [["Date", "Type", "Description", "Ref", "In", "Out", "Status"]]
    for t in rows[:200]:
        amt = float(t.get("amount") or 0)
        direction = t.get("direction")
        desc = t.get("counterparty_name") or "-"
        if t.get("type") == "deposit":
            desc = "Wallet top-up"
        elif t.get("type") == "withdrawal":
            desc = "To " + (t.get("counterparty_name") or "Bank")
        elif t.get("type") == "p2p_send":
            desc = "To " + (t.get("counterparty_name") or "GAIA user")
        elif t.get("type") == "p2p_receive":
            desc = "From " + (t.get("counterparty_name") or "GAIA user")
        elif t.get("type") == "scan_purchase":
            desc = t.get("counterparty_name") or "Scan purchase"

        dt = str(t.get("created_at") or "")[:16].replace("T", " ")

        table_data.append([
            dt,
            (t.get("type") or "").upper()[:14],
            desc[:26],
            (t.get("reference") or "")[:12],
            fmt_naira(amt) if direction == "in" else "",
            fmt_naira(amt) if direction == "out" else "",
            (t.get("status") or "").upper(),
        ])

    tbl = Table(table_data, colWidths=[24 * mm, 24 * mm, 42 * mm, 24 * mm, 22 * mm, 22 * mm, 18 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#ccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("ALIGN", (4, 1), (5, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 18))
    story.append(Paragraph(
        "This is a system-generated statement and does not require a signature. "
        "For questions contact darkmoorltd@gmail.com",
        small,
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="gaia-statement-{uid[:8]}-{datetime.utcnow().strftime("%Y%m%d")}.pdf"',
            "Cache-Control": "private, max-age=60",
        },
    )


@router.get("/wallet/receipt/{reference}/pdf")
async def receipt_pdf(reference: str, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user["sub"]

    s = svc()
    r = s.table("wallet_transactions").select("*").eq("reference", reference).eq("user_id", uid).limit(1).execute()
    if not r.data:
        raise HTTPException(404, "Receipt not found")
    tx = r.data[0]

    w = s.table("farmer_wallets").select("account_number,account_name,balance").eq("user_id", uid).limit(1).execute()
    wallet = w.data[0] if w.data else {}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=24 * mm, rightMargin=24 * mm, topMargin=24 * mm, bottomMargin=24 * mm)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=DARK, fontSize=20, alignment=TA_CENTER)
    sub = ParagraphStyle("sub", parent=styles["Normal"], alignment=TA_CENTER, fontSize=10, textColor=colors.HexColor("#666"))
    big = ParagraphStyle("big", parent=styles["Normal"], alignment=TA_CENTER, fontSize=28, textColor=GREEN)
    lbl = ParagraphStyle("lbl", parent=styles["Normal"], alignment=TA_CENTER, fontSize=9, textColor=colors.HexColor("#666"))

    story = []
    story.append(Paragraph("GAIA WALLET RECEIPT", h1))
    story.append(Paragraph("Darkmoor Ltd - Transaction Confirmation", sub))
    story.append(Spacer(1, 18))

    story.append(Paragraph("TRANSACTION AMOUNT", lbl))
    story.append(Paragraph(
        ("+" if tx.get("direction") == "in" else "-") + fmt_naira(tx.get("amount") or 0),
        big,
    ))
    story.append(Spacer(1, 14))

    lines = [
        ["Receipt Number", str(tx.get("receipt_number") or "-")],
        ["Reference", str(tx.get("reference") or "-")],
        ["Date & Time", str(tx.get("created_at") or "")[:19].replace("T", " ")],
        ["Type", str(tx.get("type") or "").upper()],
        ["Status", str(tx.get("status") or "").upper()],
        ["Counterparty", str(tx.get("counterparty_name") or "-")],
        ["Counterparty Account", str(tx.get("counterparty_acct") or "-")],
        ["Wallet Account", str(wallet.get("account_number") or "-")],
        ["Account Name", str(wallet.get("account_name") or "-")],
        ["Balance After", fmt_naira(tx.get("balance_after") or wallet.get("balance") or 0)],
    ]
    tbl = Table(lines, colWidths=[60 * mm, 100 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), LIGHT),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#444")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e0e0e0")),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 20))
    story.append(Paragraph(
        "This receipt is system-generated. Save for your records.",
        ParagraphStyle("smallc", parent=styles["Normal"], alignment=TA_CENTER, fontSize=8, textColor=colors.HexColor("#999")),
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="gaia-receipt-{reference[:16]}.pdf"',
            "Cache-Control": "private, max-age=300",
        },
    )
