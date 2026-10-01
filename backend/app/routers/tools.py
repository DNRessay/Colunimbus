# VAT 201 prep, payroll tracker, debtor/creditor ageing, AI reviews of each, and the monthly management email.
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import JSON, Boolean, DateTime, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..db import Base
from ..deps import current_user, get_db
from ..models import BankTransaction, ERPNextInvoice, User, fk, pk, text
from ..services import categorize
from .insights import INTERNAL, _clients

router = APIRouter(prefix="/api/tools", tags=["tools"])
VAT_RATE = 0.15

# VAT treatment per category, by name. standard: VAT at 15% is in the amount; zero / exempt: no VAT;
# none: outside VAT (wages, transfers, loans, tax payments, interest).
TREATMENT = [
    ("none", r"intercompany|transfer|savings|round-?up|salar|wage|payroll|loan|drawings|sars|tax|uif|paye|dividend"),
    ("exempt", r"interest|rates|residential rent|life insurance|medical aid|bonitas|discovery health|momentum health"),
    ("zero", r"export|petrol|fuel|diesel|basic food|maize"),
]


def treatment(category: str) -> str:
    low = (category or "").lower()
    for kind, pattern in TREATMENT:
        if re.search(pattern, low):
            return kind
    return "standard"


def _lines(db: Session, clients, start: date, end: date):
    ids = [c.id for c in clients]
    if not ids:
        return []
    from ..services import yoco

    with_yoco = yoco.clients_with_yoco(db, ids)
    return [t for t in db.scalars(select(BankTransaction).where(BankTransaction.client_id.in_(ids), BankTransaction.date >= start,
                                                                BankTransaction.date <= end)) if not yoco.is_payout(t, with_yoco)]


def _period(start: str, end: str):
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "Dates are YYYY-MM-DD.")
    return (s, e) if s <= e else (e, s)


# ── VAT 201 ────────────────────────────────────────────────────────────────

def vat_data(db: Session, user: User, client_id: int, start: date, end: date):
    client = _clients(db, user, client_id)[0]
    sales, purchases = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float))
    skipped = {"uncategorised": 0, "uncategorised_amount": 0.0, "outside_vat": 0.0}
    for t in _lines(db, [client], start, end):
        cat = t.category.name if t.category else ""
        amt = float(t.amount or 0)
        if not cat:
            skipped["uncategorised"] += 1
            skipped["uncategorised_amount"] += amt
            continue
        kind = treatment(cat)
        if kind == "none" or INTERNAL.search(cat):
            skipped["outside_vat"] += amt
            continue
        book = sales if t.transaction_type == "credit" else purchases
        book[kind][cat] += amt
        if t.transaction_type != "credit" and t.fee:
            purchases["standard"]["Bank charges (fees column)"] += float(t.fee)
    std_sales = sum(sales["standard"].values())
    std_purch = sum(purchases["standard"].values())
    output_vat = std_sales * VAT_RATE / (1 + VAT_RATE)
    input_vat = std_purch * VAT_RATE / (1 + VAT_RATE)
    fmt = lambda book: [{"category": k, "amount": round(v, 2), "treatment": t}  # noqa: E731
                        for t, cats in book.items() for k, v in sorted(cats.items(), key=lambda kv: -kv[1])]
    return {
        "company": client.name, "vat_registered": client.vat_registered,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "fields": {"1_standard_rated_supplies": round(std_sales, 2), "2_zero_rated_supplies": round(sum(sales["zero"].values()), 2),
                   "3_exempt_supplies": round(sum(sales["exempt"].values()), 2), "4_output_tax": round(output_vat, 2),
                   "15_input_tax_other": round(input_vat, 2), "19_total_input_tax": round(input_vat, 2),
                   "20_vat_payable": round(output_vat - input_vat, 2)},
        "sales": fmt(sales), "purchases": fmt(purchases),
        "skipped": {k: round(v, 2) if isinstance(v, float) else v for k, v in skipped.items()},
        "notes": ["A draft from bank transactions: amounts include VAT and the 15% is worked out (×15/115). Claim input "
                  "VAT only where you hold a valid tax invoice, and check each category's treatment before filing on eFiling."]
                 + ([f"{skipped['uncategorised']} uncategorised line(s) (R{skipped['uncategorised_amount']:,.2f}) are left out: "
                     "categorise them first."] if skipped["uncategorised"] else [])
                 + ([] if client.vat_registered else [f"{client.name} isn't marked VAT-registered in Companies."]),
    }


@router.get("/vat")
def vat(client_id: int, start: str, end: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s, e = _period(start, end)
    return vat_data(db, user, client_id, s, e)


# ── Payroll ────────────────────────────────────────────────────────────────

SALARY_RE = re.compile(r"salar|wage|payroll|staff pay|bonus|commission paid", re.I)
STATUTORY_RE = re.compile(r"\bsars\b.*(emp|paye|uif|sdl)|\b(paye|uif|sdl|emp201)\b", re.I)


def payroll_data(db: Session, user: User, client_id: Optional[int], start: date, end: date):
    clients = _clients(db, user, client_id)
    months = defaultdict(lambda: {"salaries": 0.0, "statutory": 0.0, "people": set()})
    total_out = total_in = 0.0
    for t in _lines(db, clients, start, end):
        cat = t.category.name if t.category else ""
        amt = float(t.amount or 0)
        if INTERNAL.search(cat):
            continue
        if t.transaction_type == "credit":
            total_in += amt
            continue
        total_out += amt
        text_ = f"{cat} {t.description}"
        m = months[t.date.strftime("%Y-%m")]
        if STATUTORY_RE.search(text_):
            m["statutory"] += amt
        elif SALARY_RE.search(text_):
            m["salaries"] += amt
            payee = re.sub(r"\b(salary|salaries|wages?|payroll|staff pay|bonus|ref\w*|\d+)\b", " ", t.description, flags=re.I)
            payee = re.sub(r"\s+", " ", payee).strip(" -:")[:40]
            if payee:
                m["people"].add(payee.title())
    rows = [{"month": k, "salaries": round(v["salaries"], 2), "statutory": round(v["statutory"], 2),
             "total": round(v["salaries"] + v["statutory"], 2), "payees": len(v["people"]), "names": sorted(v["people"])[:15]}
            for k, v in sorted(months.items()) if v["salaries"] or v["statutory"]]
    total = sum(r["total"] for r in rows)
    return {"scope": clients[0].name if client_id else "All companies", "period": {"start": start.isoformat(), "end": end.isoformat()},
            "months": rows, "total": round(total, 2), "salaries": round(sum(r["salaries"] for r in rows), 2),
            "statutory": round(sum(r["statutory"] for r in rows), 2),
            "share_of_expenses": round(total / total_out * 100, 1) if total_out else None,
            "share_of_income": round(total / total_in * 100, 1) if total_in else None,
            "average_month": round(total / len(rows), 2) if rows else 0,
            "notes": ([] if rows else ["No payroll lines found. Name a category with 'Salaries' or 'Wages' (or use those words "
                                       "in payment references) and SARS EMP201 payments are picked up automatically."])}


@router.get("/payroll")
def payroll(start: str, end: str, client_id: Optional[int] = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s, e = _period(start, end)
    return payroll_data(db, user, client_id, s, e)


# ── Debtor / creditor ageing (ERPNext invoices) ────────────────────────────

BUCKETS = [("current", None), ("1-30", 30), ("31-60", 60), ("61-90", 90), ("90+", 10 ** 6)]


def ageing_data(db: Session, user: User, client_id: Optional[int], as_of: Optional[date] = None):
    as_of = as_of or date.today()
    clients = _clients(db, user, client_id)
    ids = [c.id for c in clients]
    out = {}
    for side, inv_type in (("debtors", "Sales Invoice"), ("creditors", "Purchase Invoice")):
        buckets = {b: 0.0 for b, _ in BUCKETS}
        parties = defaultdict(lambda: {"total": 0.0, "overdue": 0.0, "oldest_days": 0, "invoices": 0})
        for inv in db.scalars(select(ERPNextInvoice).where(ERPNextInvoice.client_id.in_(ids), ERPNextInvoice.invoice_type == inv_type,
                                                          ERPNextInvoice.outstanding_amount > 0)) if ids else []:
            amt = float(inv.outstanding_amount or 0)
            days = (as_of - (inv.due_date or inv.posting_date)).days
            bucket = "current" if days <= 0 else next(b for b, limit in BUCKETS[1:] if days <= limit)
            buckets[bucket] += amt
            p = parties[inv.party_name or inv.party_id or "Unknown"]
            p["total"] += amt
            p["invoices"] += 1
            if days > 0:
                p["overdue"] += amt
                p["oldest_days"] = max(p["oldest_days"], days)
        total = sum(buckets.values())
        out[side] = {"total": round(total, 2), "overdue": round(total - buckets["current"], 2),
                     "buckets": {k: round(v, 2) for k, v in buckets.items()},
                     "parties": [{"name": k, **{kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}}
                                 for k, v in sorted(parties.items(), key=lambda kv: -kv[1]["overdue"])][:15]}
    has = out["debtors"]["total"] or out["creditors"]["total"]
    return {"scope": clients[0].name if client_id else "All companies", "as_of": as_of.isoformat(), **out,
            "notes": [] if has else ["No unpaid ERPNext invoices. Sync invoices on the Invoices page to see ageing."]}


@router.get("/ageing")
def ageing(client_id: Optional[int] = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return ageing_data(db, user, client_id)


# ── AI review of any of the above (Groq, cached) ───────────────────────────

class AIReview(Base):
    __tablename__ = "ai_reviews"
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    key: Mapped[str] = text(64)  # topic + scope + data hash: same numbers, same answer, no new call
    items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


TOPICS = {"vat": "a draft VAT 201 return: check categories' VAT treatment, missing invoices, unusual input/output ratios",
          "payroll": "payroll costs: affordability against income, trends, statutory (PAYE/UIF/SDL) payments that look missing",
          "ageing": "debtors and creditors ageing: collection risk, who to chase first, supplier payments falling overdue",
          "report": "a profit and loss management report for the period: profitability and margin, cost drivers, "
                    "monthly cash-flow trend, bank fees, and the most useful actions for next month"}


class ReviewIn(BaseModel):
    topic: str
    client_id: Optional[int] = None
    start: str = ""
    end: str = ""


@router.post("/review")
def review(body: ReviewIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.topic not in TOPICS:
        raise HTTPException(400, "Unknown topic.")
    if body.topic == "vat":
        if not body.client_id:
            raise HTTPException(400, "Pick a company.")
        data = vat_data(db, user, body.client_id, *_period(body.start, body.end))
    elif body.topic == "payroll":
        data = payroll_data(db, user, body.client_id, *_period(body.start, body.end))
        for m in data["months"]:
            m.pop("names", None)  # payee names stay here
    elif body.topic == "report":
        from .insights import report as pl_report

        data = pl_report(body.start, body.end, body.client_id, user, db)
        data.pop("generated_at", None)
    else:
        data = ageing_data(db, user, body.client_id)
    blob = json.dumps(data, sort_keys=True, default=str)
    key = hashlib.sha256(f"{body.topic}|{body.client_id}|{blob}".encode()).hexdigest()
    hit = db.scalar(select(AIReview).where(AIReview.practice_id == user.practice_id, AIReview.key == key))
    if hit:
        return {"items": hit.items, "cached": True}
    system = ("You review a small South African business's numbers for its owner. Topic: " + TOPICS[body.topic] + ". "
              'Reply with JSON only: {"points": [{"title": short, "detail": 1-2 sentences with their numbers, '
              '"level": "high"|"medium"|"low"}]}. 3 to 5 points, most important first. You are not their accountant or tax '
              "practitioner; say so if a point needs one.")
    try:
        out = categorize.groq_json(system, blob)
    except RuntimeError as e:
        raise HTTPException(503, f"AI unavailable: {e}"[:300])
    items = [{"title": str(p.get("title"))[:140], "detail": str(p.get("detail", ""))[:600],
              "level": p.get("level") if p.get("level") in ("high", "medium", "low") else "medium"}
             for p in (out.get("points") or []) if isinstance(p, dict) and p.get("title")][:6]
    db.add(AIReview(practice_id=user.practice_id, key=key, items=items))
    db.commit()
    return {"items": items, "cached": False}


# ── Monthly management report by email ─────────────────────────────────────

class MonthlyEmail(Base):
    __tablename__ = "monthly_emails"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = fk("users.id")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_month_sent: Mapped[str] = text(7)  # YYYY-MM it covered


def _last_month(today: date):
    end = today.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def build_email(db: Session, user: User, today: Optional[date] = None):
    from .insights import AIInsight, report

    today = today or date.today()
    s, e = _last_month(today)
    money = lambda v: f"R{v:,.2f}"  # noqa: E731
    clients = _clients(db, user, None)
    rows, text_rows = [], []
    for c in clients:
        r = report(s.isoformat(), e.isoformat(), c.id, user, db)
        rows.append(f"<tr><td>{c.name}</td><td align='right'>{money(r['total_income'])}</td><td align='right'>{money(r['total_expenses'])}</td>"
                    f"<td align='right' style='color:{'#8c0b10' if r['net'] < 0 else '#17244e'}'><b>{money(r['net'])}</b></td></tr>")
        text_rows.append(f"- {c.name}: in {money(r['total_income'])}, out {money(r['total_expenses'])}, net {money(r['net'])}")
    age = ageing_data(db, user, None, today)
    pay = payroll_data(db, user, None, s, e)
    vat_lines = []
    for c in clients:
        if c.vat_registered:
            v = vat_data(db, user, c.id, s, e)["fields"]["20_vat_payable"]
            vat_lines.append(f"{c.name}: about {money(v)} {'to pay' if v >= 0 else 'refund'} for {s:%B}")
    ai = db.scalar(select(AIInsight).where(AIInsight.practice_id == user.practice_id, AIInsight.client_id.is_(None))
                   .order_by(AIInsight.id.desc()))
    tips = (ai.items if ai else [])[:3]
    period = s.strftime("%B %Y")
    html = f"""<div style="font-family:Arial,sans-serif;color:#1b1b18;max-width:640px">
<div style="background:#17244e;color:#fff;padding:16px 20px;border-bottom:4px solid #8c0b10"><b style="font-size:18px">C.T.H.A.I</b>
<div style="opacity:.8">Management report · {period}</div></div>
<h3 style="color:#17244e">Profit &amp; loss by company</h3>
<table width="100%" cellpadding="6" style="border-collapse:collapse;border:1px solid #dde1ea"><tr style="background:#f4f5f9">
<th align="left">Company</th><th align="right">In</th><th align="right">Out</th><th align="right">Net</th></tr>{''.join(rows)}</table>
<h3 style="color:#17244e">Who owes whom</h3>
<p>Customers owe <b>{money(age['debtors']['total'])}</b> ({money(age['debtors']['overdue'])} overdue).
You owe suppliers <b>{money(age['creditors']['total'])}</b> ({money(age['creditors']['overdue'])} overdue).</p>
<h3 style="color:#17244e">Payroll</h3><p>{money(pay['total'])} in {period}{f" ({pay['share_of_expenses']}% of costs)" if pay['share_of_expenses'] else ""}.</p>
{"<h3 style='color:#17244e'>VAT estimate</h3><p>" + "<br>".join(vat_lines) + "</p>" if vat_lines else ""}
{"<h3 style='color:#8c0b10'>AI suggestions</h3><ol>" + "".join(f"<li><b>{t['title']}</b>: {t['detail']}</li>" for t in tips) + "</ol>" if tips else ""}
<p style="color:#5a5f6e;font-size:12px">From bank transactions and ERPNext invoices in C.T.H.A.I. A management summary, not audited
statements or tax advice. Switch this email off on the Reports page.</p></div>"""
    plain = (f"C.T.H.A.I management report · {period}\n\nProfit & loss by company:\n" + "\n".join(text_rows)
             + f"\n\nCustomers owe {money(age['debtors']['total'])}; you owe suppliers {money(age['creditors']['total'])}."
             + f"\nPayroll: {money(pay['total'])}." + ("\nVAT: " + "; ".join(vat_lines) if vat_lines else ""))
    return f"C.T.H.A.I report for {period}", plain, html, s.strftime("%Y-%m")


@router.get("/monthly-email")
def monthly_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.scalar(select(MonthlyEmail).where(MonthlyEmail.user_id == user.id))
    return {"enabled": bool(row and row.enabled), "last_month_sent": row.last_month_sent if row else "", "to": user.email}


class ToggleIn(BaseModel):
    enabled: bool


@router.put("/monthly-email")
def monthly_toggle(body: ToggleIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.scalar(select(MonthlyEmail).where(MonthlyEmail.user_id == user.id)) or MonthlyEmail(user_id=user.id, last_month_sent="")
    row.enabled = body.enabled
    db.add(row)
    db.commit()
    return monthly_status(user, db)


@router.post("/monthly-email/send-now")
def monthly_send_now(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..services.mailer import send_mail

    subject, plain, html, _ = build_email(db, user)
    try:
        send_mail(user.email, subject, plain, html=html)
    except Exception as e:
        raise HTTPException(502, f"Couldn't send the email ({type(e).__name__}).")
    return {"sent_to": user.email, "subject": subject}


def send_monthly(db: Session, today: Optional[date] = None):
    """Nightly worker: on the 1st (or the first run after it), send last month's report to everyone who switched it on."""
    from ..services.mailer import send_mail

    today = today or date.today()
    month = _last_month(today)[0].strftime("%Y-%m")
    sent = 0
    for row in db.scalars(select(MonthlyEmail).where(MonthlyEmail.enabled.is_(True), MonthlyEmail.last_month_sent != month)):
        user = db.get(User, row.user_id)
        if not user or not user.is_active:
            continue
        try:
            subject, plain, html, covered = build_email(db, user, today)
            send_mail(user.email, subject, plain, html=html)
            row.last_month_sent = covered
            db.commit()
            sent += 1
        except Exception:
            db.rollback()
    return sent
