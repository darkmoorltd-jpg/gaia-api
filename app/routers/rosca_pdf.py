# app/routers/rosca_pdf.py
# Group statement PDF for ROSCA savings groups.
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
AMBER = colors.HexColor("#ff9800")


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


def fmt_n(n):
    try:
        return "N" + f"{float(n):,.2f}"
    except Exception:
        return "N0.00"


@router.get("/rosca/{group_id}/statement/pdf")
async def rosca_statement_pdf(group_id: str, authorization: str = Header(None)):
    user = auth_user(authorization)

    s = svc()
    r = s.rpc("rosca_statement_data", {"p_group_id": group_id}).execute()
    if r.data is None:
        raise HTTPException(404, "Group not found or access denied")

    d = r.data
    g = d.get("group") or {}
    w = d.get("wallet") or {}
    trust = d.get("trust") or {}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title="GAIA ROSCA Statement",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=DARK, fontSize=20, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=colors.HexColor("#666"), fontSize=10)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#888"))
    center = ParagraphStyle("c", parent=styles["Normal"], alignment=TA_CENTER, fontSize=9, textColor=colors.HexColor("#666"))
    big = ParagraphStyle("big", parent=styles["Normal"], alignment=TA_CENTER, fontSize=26, textColor=GREEN)

    story = []
    story.append(Paragraph("GAIA ROSCA STATEMENT", h1))
    story.append(Paragraph("Savings group record · powered by Darkmoor Ltd", sub))
    story.append(Spacer(1, 10))

    story.append(Paragraph(g.get("name") or "-", ParagraphStyle("gname", parent=styles["Heading2"], textColor=DARK, fontSize=16)))
    story.append(Paragraph(
        (g.get("variant") or "esusu").upper() + " · " + (g.get("frequency") or "").upper() +
        " · " + (g.get("state") or "-") + (", " + g.get("lga") if g.get("lga") else ""),
        sub,
    ))
    story.append(Spacer(1, 12))

    meta = [
        ["Status", (g.get("status") or "").upper()],
        ["Owner", d.get("owner_email") or "-"],
        ["Contribution", fmt_n(g.get("contribution_amount")) + " / " + (g.get("frequency") or "-")],
        ["Members", str(len(d.get("members") or [])) + " of " + str(g.get("cycle_members") or 0)],
        ["Pool per round", fmt_n(float(g.get("contribution_amount") or 0) * int(g.get("cycle_members") or 0))],
        ["Escrow held", fmt_n(w.get("escrow") or 0)],
        ["Trust score", str(trust.get("score", "-")) + " / 100 (" + (trust.get("band") or "-") + ")"],
        ["Invite code", (g.get("invite_code") or "-") if g.get("status") == "draft" else "closed"],
        ["Created", str(g.get("created_at") or "")[:10]],
        ["Started", str(g.get("started_at") or "-")[:10]],
        ["Completed", str(g.get("completed_at") or "-")[:10]],
    ]
    t1 = Table(meta, colWidths=[45 * mm, 130 * mm])
    t1.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), LIGHT),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#333")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e0e0e0")),
    ]))
    story.append(t1)
    story.append(Spacer(1, 16))

    story.append(Paragraph("MEMBER ROTATION", ParagraphStyle("sh", parent=styles["Heading3"], textColor=DARK, fontSize=12)))
    story.append(Spacer(1, 6))
    rows = [["#", "Member", "Paid", "Penalties", "Collected", "Status"]]
    for m in (d.get("members") or []):
        rows.append([
            str(m.get("position", "")),
            (m.get("name") or m.get("email") or "-")[:28],
            fmt_n(m.get("total_paid") or 0),
            fmt_n(m.get("penalty_accrued") or 0),
            (str(m.get("collected_at") or "")[:10] if m.get("has_collected") else "-"),
            (m.get("status") or "").upper(),
        ])
    t2 = Table(rows, colWidths=[10 * mm, 62 * mm, 30 * mm, 28 * mm, 26 * mm, 22 * mm], repeatRows=1)
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#ddd")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("ALIGN", (2, 1), (3, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t2)
    story.append(Spacer(1, 16))

    story.append(Paragraph("CONTRIBUTIONS", ParagraphStyle("sh2", parent=styles["Heading3"], textColor=DARK, fontSize=12)))
    story.append(Spacer(1, 6))
    rows = [["Round", "Member", "Amount", "Paid", "Late", "Penalty", "Approved"]]
    for c in (d.get("contributions") or [])[:80]:
        rows.append([
            str(c.get("round") or ""),
            (c.get("email") or "-")[:24],
            fmt_n(c.get("amount") or 0),
            str(c.get("paid_at") or "")[:10],
            "yes" if c.get("paid_late") else "no",
            fmt_n(c.get("penalty") or 0),
            "yes" if c.get("approved") else "no",
        ])
    if len(rows) == 1:
        rows.append(["-", "No contributions yet", "-", "-", "-", "-", "-"])
    t3 = Table(rows, colWidths=[14 * mm, 52 * mm, 26 * mm, 22 * mm, 14 * mm, 24 * mm, 22 * mm], repeatRows=1)
    t3.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#ddd")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t3)
    story.append(Spacer(1, 18))
    story.append(Paragraph(
        "This is a system-generated ROSCA statement. Save it for your records. Questions: darkmoorltd@gmail.com",
        small,
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in (g.get("name") or "group"))
    filename = "gaia-rosca-" + safe_name + "-" + datetime.utcnow().strftime("%Y%m%d") + ".pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="' + filename + '"',
            "Cache-Control": "private, max-age=300",
        },
    )
