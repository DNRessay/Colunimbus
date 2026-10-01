# PayShap (South Africa's instant payment rail): spot PayShap lines on bank statements, and read the banks'
# PayShap notification emails so a payment is seen the moment it lands, then matched to its statement line later.
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BankTransaction, PayShapNotice

PAYSHAP_RE = re.compile(r"pay\s?shap|shap\s?id|shapid|rapid\s+payment|\brpp\b|real[\s-]?time\s+(?:clearing|payment)|\brtc\b", re.I)
AMOUNT_RE = re.compile(r"R\s?(\d{1,3}(?:[ ,]\d{3})*(?:\.\d{2})|\d+(?:\.\d{2}))")
IN_RE = re.compile(r"\b(received|receive|paid you|sent you|credited|into your account|deposit(?:ed)?|incoming)\b", re.I)
OUT_RE = re.compile(r"\b(you (?:have )?(?:sent|paid)|sent to|paid to|debited|from your account|outgoing|payment made)\b", re.I)
FROM_RE = re.compile(r"\bfrom\s+([A-Z0-9][\w .&'@-]{1,60}?)(?=\s+(?:on|at|with|via|using|ref|reference|into|to)\b|[.,\n]|$)", re.I)
TO_RE = re.compile(r"\bto\s+([A-Z0-9][\w .&'@-]{1,60}?)(?=\s+(?:on|at|with|via|using|ref|reference|from)\b|[.,\n]|$)", re.I)
REF_RE = re.compile(r"\b(?:ref(?:erence)?|description)\s*[:\-]?\s*([^\n.,]{2,60})", re.I)
QUERY = "(payshap OR \"pay shap\" OR shapid) -has:attachment newer_than:180d"
GENERIC_PARTY = re.compile(r"^(your|the|an?|account|bank|payshap|capitec|fnb|absa|nedbank|standard bank|tymebank)\b", re.I)


def is_payshap(description: str) -> bool:
    return bool(PAYSHAP_RE.search(description or ""))


def _amount(text):
    m = AMOUNT_RE.search(text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(" ", "").replace(",", ""))
    except InvalidOperation:
        return None


def parse_notice(subject: str, body: str):
    """{direction, amount, counterparty, reference} from a PayShap notification, or None if it isn't one."""
    text = f"{subject}\n{body}"
    if not is_payshap(text):
        return None
    amount = _amount(text)
    if not amount:
        return None
    out, inn = OUT_RE.search(text), IN_RE.search(text)
    direction = "out" if out and (not inn or out.start() < inn.start()) else "in"
    party_m = (FROM_RE if direction == "in" else TO_RE).search(text)
    party = party_m.group(1).strip() if party_m else ""
    if GENERIC_PARTY.match(party):
        party = ""
    ref_m = REF_RE.search(text)
    return {"direction": direction, "amount": amount, "counterparty": party[:200],
            "reference": (ref_m.group(1).strip() if ref_m else "")[:200]}


def match(db: Session, practice_id: int):
    """Link each unmatched notice to the statement line with the same amount and direction, within 4 days."""
    taken = set(db.scalars(select(PayShapNotice.transaction_id).where(PayShapNotice.transaction_id.is_not(None))))
    matched = 0
    for n in db.scalars(select(PayShapNotice).where(PayShapNotice.practice_id == practice_id,
                                                    PayShapNotice.client_id.is_not(None),
                                                    PayShapNotice.transaction_id.is_(None))):
        day = n.received_at.date()
        cands = db.scalars(select(BankTransaction).where(
            BankTransaction.client_id == n.client_id, BankTransaction.amount == n.amount,
            BankTransaction.transaction_type == ("credit" if n.direction == "in" else "debit"),
            BankTransaction.date >= day - timedelta(days=1), BankTransaction.date <= day + timedelta(days=4)))
        best = sorted((t for t in cands if t.id not in taken),
                      key=lambda t: (not is_payshap(t.description), abs((t.date - day).days)))
        if best:
            n.transaction_id = best[0].id
            taken.add(best[0].id)
            matched += 1
    db.commit()
    return matched


def summary(db: Session, client_ids, start, end):
    lines = [t for t in db.scalars(select(BankTransaction).where(BankTransaction.client_id.in_(list(client_ids)),
                                                                 BankTransaction.date >= start, BankTransaction.date <= end)
                                   .order_by(BankTransaction.date.desc()))
             if is_payshap(t.description) or "payshap" in (t.tags or "")]
    notices = list(db.scalars(select(PayShapNotice).where(PayShapNotice.client_id.in_(list(client_ids)),
                                                          PayShapNotice.received_at >= start)
                              .order_by(PayShapNotice.received_at.desc())))
    noticed = {n.transaction_id for n in notices if n.transaction_id}
    total = lambda rows, kind: round(float(sum(t.amount or 0 for t in rows if t.transaction_type == kind)), 2)
    pending = [n for n in notices if not n.transaction_id]
    return {
        "received": total(lines, "credit"), "sent": total(lines, "debit"), "count": len(lines),
        "pending_in": round(float(sum(n.amount for n in pending if n.direction == "in")), 2),
        "pending_out": round(float(sum(n.amount for n in pending if n.direction == "out")), 2),
        "notices": [{"id": n.id, "client_id": n.client_id, "date": n.received_at.isoformat(), "direction": n.direction,
                     "amount": float(n.amount), "counterparty": n.counterparty, "reference": n.reference,
                     "on_statement": bool(n.transaction_id)} for n in notices[:200]],
        "lines": [{"id": t.id, "client_id": t.client_id, "date": t.date.isoformat(), "description": t.description,
                   "amount": float(t.amount or 0), "type": t.transaction_type, "notified": t.id in noticed} for t in lines[:300]],
    }
