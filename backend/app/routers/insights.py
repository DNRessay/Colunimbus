# Dashboard numbers and charts, AI suggestions and chat (Groq), and a profit & loss report, across a practice's
# companies or for one. Transfers between your own companies are left out of income and expenses.
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..db import Base
from ..deps import current_user, get_db
from ..models import BankTransaction, Client, User, fk, pk, text
from ..services import categorize

router = APIRouter(prefix="/api/insights", tags=["insights"])

INTERNAL = re.compile(r"intercompany|savings\s*&\s*transfers|round-?up", re.I)
FEES = re.compile(r"bank charges|banking & finance", re.I)
REFRESH_EVERY = timedelta(hours=6)
KEEP = timedelta(hours=30)


class AIInsight(Base):
    """Saved AI suggestions per practice (and optionally one company), so pages don't call the API every load."""

    __tablename__ = "ai_insights"
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    client_id: Mapped[Optional[int]] = fk("clients.id", "CASCADE", True)
    items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


def _clients(db: Session, user: User, client_id: Optional[int]):
    q = select(Client).where(Client.practice_id == user.practice_id, Client.is_active.is_(True))
    if client_id:
        q = q.where(Client.id == client_id)
    cs = list(db.scalars(q.order_by(Client.name)))
    if client_id and not cs:
        raise HTTPException(404, "Company not found.")
    return cs


def _flows(db: Session, clients, start: date, end: date):
    """Rows of (client, date, signed amount, fee, category name) without own-company transfers."""
    ids = [c.id for c in clients]
    if not ids:
        return []
    from ..services import yoco

    out = []
    with_yoco = yoco.clients_with_yoco(db, ids)
    for t in db.scalars(select(BankTransaction).where(BankTransaction.client_id.in_(ids), BankTransaction.date >= start,
                                                      BankTransaction.date <= end)):
        cat = t.category.name if t.category else ""
        if INTERNAL.search(cat) or yoco.is_payout(t, with_yoco):
            continue
        amt = float(t.amount or 0)
        signed = amt if t.transaction_type == "credit" else -amt
        out.append((t.client_id, t.date, signed, float(t.fee or 0), cat or "Uncategorized"))
    return out


def overview_data(db: Session, user: User, client_id: Optional[int] = None, months: int = 12):
    clients = _clients(db, user, client_id)
    names = {c.id: c.name for c in clients}
    today = date.today()
    start = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    rows = _flows(db, clients, start, today)
    by_month = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    cats, inc_cats, per_co = defaultdict(float), defaultdict(float), defaultdict(lambda: {"in": 0.0, "out": 0.0})
    fees = 0.0
    for cid, d, amt, fee, cat in rows:
        m = by_month[d.strftime("%Y-%m")]
        if amt >= 0:
            m["in"] += amt
            per_co[cid]["in"] += amt
            inc_cats[cat] += amt
        else:
            m["out"] += -amt + fee
            per_co[cid]["out"] += -amt + fee
            cats[cat] += -amt
        fees += fee + (-amt if FEES.search(cat) and amt < 0 else 0)
    keys = []
    y, mo = today.year, today.month
    for _ in range(months):
        keys.append(f"{y:04d}-{mo:02d}")
        y, mo = (y, mo - 1) if mo > 1 else (y - 1, 12)
    keys.reverse()
    this, last = by_month[keys[-1]], by_month[keys[-2]] if len(keys) > 1 else {"in": 0.0, "out": 0.0}
    ids = [c.id for c in clients]

    def count(*where):
        from sqlalchemy import func

        return db.scalar(select(func.count()).select_from(BankTransaction).where(BankTransaction.client_id.in_(ids), *where)) if ids else 0

    total_in, total_out = sum(by_month[k]["in"] for k in keys), sum(by_month[k]["out"] for k in keys)
    return {
        "scope": names.get(client_id, "All companies") if client_id else "All companies",
        "kpis": {"in_this_month": round(this["in"], 2), "out_this_month": round(this["out"], 2),
                 "net_this_month": round(this["in"] - this["out"], 2),
                 "in_last_month": round(last["in"], 2), "out_last_month": round(last["out"], 2),
                 "in_12m": round(total_in, 2), "out_12m": round(total_out, 2), "net_12m": round(total_in - total_out, 2),
                 "margin_12m": round((total_in - total_out) / total_in * 100, 1) if total_in else None,
                 "bank_fees_12m": round(fees, 2),
                 "uncategorized": count(BankTransaction.category_id.is_(None)),
                 "not_synced": count(BankTransaction.category_id.is_not(None), BankTransaction.erpnext_synced.is_(False))},
        "months": [{"month": k, "in": round(by_month[k]["in"], 2), "out": round(by_month[k]["out"], 2)} for k in keys],
        "expenses_by_category": [{"name": k, "amount": round(v, 2)} for k, v in sorted(cats.items(), key=lambda kv: -kv[1])][:12],
        "income_by_category": [{"name": k, "amount": round(v, 2)} for k, v in sorted(inc_cats.items(), key=lambda kv: -kv[1])][:8],
        "companies": sorted([{"id": c.id, "name": c.name, "in": round(per_co[c.id]["in"], 2), "out": round(per_co[c.id]["out"], 2),
                              "net": round(per_co[c.id]["in"] - per_co[c.id]["out"], 2)} for c in clients],
                            key=lambda x: -(x["in"] + x["out"])),
    }


@router.get("/overview")
def overview(client_id: Optional[int] = None, months: int = 12, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return overview_data(db, user, client_id, max(2, min(months, 36)))


# ── AI ──────────────────────────────────────────────────────────────────────

NOTE = ("You advise a small South African business owner about their companies' bank-based books. Amounts are in rand. "
        "Be specific and use their numbers. You are not their accountant or tax adviser; say so briefly when it matters.")


def _context(db, user, client_id):
    d = overview_data(db, user, client_id, 12)
    return {k: d[k] for k in ("scope", "kpis", "months", "expenses_by_category", "income_by_category", "companies")}


def _items(raw):
    data = raw if isinstance(raw, dict) else {}
    out = []
    for i in data.get("suggestions", []) if isinstance(data.get("suggestions"), list) else []:
        if isinstance(i, dict) and i.get("title"):
            out.append({"title": str(i["title"])[:140], "detail": str(i.get("detail", ""))[:700],
                        "kind": i.get("kind") if i.get("kind") in ("save", "cash", "risk", "books", "grow") else "books",
                        "impact": i.get("impact") if i.get("impact") in ("high", "medium", "low") else "medium",
                        "value": i.get("rand_per_year") if isinstance(i.get("rand_per_year"), (int, float)) else None})
    return out[:6]


@router.get("/suggestions")
def suggestions(client_id: Optional[int] = None, refresh: bool = False, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    """Saved suggestions; a refresh asks Groq at most every 6 hours (per company or for all)."""
    _clients(db, user, client_id)
    row = db.scalar(select(AIInsight).where(AIInsight.practice_id == user.practice_id, AIInsight.client_id == client_id)
                    .order_by(AIInsight.id.desc()))
    now = datetime.utcnow()
    fresh = row and now - row.created_at < (REFRESH_EVERY if refresh else KEEP)
    if fresh or (not refresh and row):
        return {"items": row.items, "created_at": row.created_at.isoformat(), "cached": True,
                "next_refresh_at": (row.created_at + REFRESH_EVERY).isoformat()}
    if not refresh:
        return {"items": [], "created_at": None, "available": bool(categorize.settings.groq_api_keys)}
    system = (NOTE + ' Reply with JSON only: {"suggestions": [{"title": short, "detail": 1-3 sentences with their numbers, '
              '"kind": "save"|"cash"|"risk"|"books"|"grow", "impact": "high"|"medium"|"low", "rand_per_year": number or null}]}. '
              "3 to 5 suggestions, most useful first: cutting costs, cash-flow risks, bookkeeping fixes (uncategorised or "
              "unsynced lines), and growth. Only what the data supports.")
    try:
        items = _items(categorize.groq_json(system, json.dumps(_context(db, user, client_id), default=str)))
    except RuntimeError as e:
        raise HTTPException(503, f"AI unavailable: {e}"[:300])
    row = AIInsight(practice_id=user.practice_id, client_id=client_id, items=items, created_at=now)
    db.add(row)
    db.commit()
    return {"items": items, "created_at": now.isoformat(), "cached": False, "next_refresh_at": (now + REFRESH_EVERY).isoformat()}


class Msg(BaseModel):
    role: str
    content: str = Field(max_length=4000)


class ChatIn(BaseModel):
    messages: List[Msg] = Field(min_length=1, max_length=30)
    client_id: Optional[int] = None


@router.post("/chat")
def chat(body: ChatIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ctx = _context(db, user, body.client_id)
    system = (NOTE + " Answer in Markdown: short paragraphs, **bold** key numbers, lists, and a small table when comparing. "
              "End with one line exactly like: FOLLOWUPS: question | question | question. "
              f"Their numbers (last 12 months, JSON):\n{json.dumps(ctx, default=str)}")
    msgs = [{"role": "system", "content": system}] + [
        {"role": m.role if m.role in ("user", "assistant") else "user", "content": m.content} for m in body.messages[-12:]]
    try:
        raw = categorize.groq_chat(msgs)
    except RuntimeError as e:
        raise HTTPException(503, f"AI unavailable: {e}"[:300])
    m = re.search(r"\n*\s*FOLLOW-?UPS?:\s*(.+?)\s*$", raw, re.I | re.S)
    reply = raw[:m.start()].strip() if m else raw.strip()
    ups = [q.strip(" -•*") for q in re.split(r"\s*\|\s*", m.group(1))][:3] if m else []
    return {"reply": reply, "followups": [q for q in ups if len(q) > 5]}


# ── Report: profit & loss and cash flow for a period ───────────────────────

@router.get("/report")
def report(start: str, end: str, client_id: Optional[int] = None, user: User = Depends(current_user),
           db: Session = Depends(get_db)):
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "Dates are YYYY-MM-DD.")
    if s > e:
        s, e = e, s
    clients = _clients(db, user, client_id)
    rows = _flows(db, clients, s, e)
    inc, exp, months = defaultdict(float), defaultdict(float), defaultdict(lambda: [0.0, 0.0])
    fees = 0.0
    for _, d, amt, fee, cat in rows:
        mk = d.strftime("%Y-%m")
        if amt >= 0:
            inc[cat] += amt
            months[mk][0] += amt
        else:
            exp[cat] += -amt
            months[mk][1] += -amt + fee
        if fee:
            exp["Bank charges (fees column)"] += fee
            fees += fee
    total_in, total_out = sum(inc.values()), sum(exp.values())
    uncategorised = sum(1 for r in rows if r[4] == "Uncategorized")
    notes = []
    if uncategorised:
        notes.append(f"{uncategorised} transaction(s) are still uncategorised; categorise them for an accurate report.")
    if total_out > total_in:
        notes.append(f"Costs were R{total_out - total_in:,.2f} more than income this period.")
    elif total_in and (total_in - total_out) / total_in < 0.05:
        notes.append("Margin is under 5% this period: costs are eating almost all income.")
    if exp:
        top = max(exp.items(), key=lambda kv: kv[1])
        notes.append(f"Biggest cost: {top[0]} at R{top[1]:,.2f} ({top[1] / total_out * 100:.0f}% of expenses).")
    return {
        "scope": clients[0].name if client_id and clients else "All companies", "period": {"start": s.isoformat(), "end": e.isoformat()},
        "generated_at": datetime.utcnow().isoformat(),
        "income": [{"name": k, "amount": round(v, 2)} for k, v in sorted(inc.items(), key=lambda kv: -kv[1])],
        "expenses": [{"name": k, "amount": round(v, 2)} for k, v in sorted(exp.items(), key=lambda kv: -kv[1])],
        "total_income": round(total_in, 2), "total_expenses": round(total_out, 2), "net": round(total_in - total_out, 2),
        "margin": round((total_in - total_out) / total_in * 100, 1) if total_in else None, "bank_fees": round(fees, 2),
        "months": [{"month": k, "in": round(v[0], 2), "out": round(v[1], 2), "net": round(v[0] - v[1], 2)} for k, v in sorted(months.items())],
        "notes": notes,
    }
