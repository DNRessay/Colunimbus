from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from ..models import BankTransaction, ERPNextConfig, ERPNextJournalEntry, ReconciliationMatch, ReconciliationPeriod
from .erpnext import ERPNextClient

DATE_TOLERANCE = 2
AMOUNT_TOLERANCE = Decimal("0.05")


def month_bounds(year, month):
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def month_txns(db: Session, user_id, year, month, status=None):
    start, end = month_bounds(year, month)
    q = select(BankTransaction).where(
        BankTransaction.user_id == user_id, BankTransaction.date >= start, BankTransaction.date <= end
    ).order_by(BankTransaction.date, BankTransaction.id)
    if status:
        q = q.where(BankTransaction.recon_status == status)
    return list(db.scalars(q).unique())


def month_entries(db: Session, user_id, year, month):
    start, end = month_bounds(year, month)
    return list(db.scalars(select(ERPNextJournalEntry).where(
        ERPNextJournalEntry.user_id == user_id,
        ERPNextJournalEntry.posting_date >= start, ERPNextJournalEntry.posting_date <= end,
    ).order_by(ERPNextJournalEntry.posting_date)))


def get_period(db: Session, user_id, year, month):
    period = db.scalar(select(ReconciliationPeriod).where(
        ReconciliationPeriod.user_id == user_id, ReconciliationPeriod.year == year, ReconciliationPeriod.month == month))
    if not period:
        period = ReconciliationPeriod(user_id=user_id, year=year, month=month, status="open")
        db.add(period)
        db.flush()
    return period


def refresh_counts(db: Session, period: ReconciliationPeriod):
    txns = month_txns(db, period.user_id, period.year, period.month)
    period.total_transactions = len(txns)
    period.matched_count = sum(t.recon_status == "matched" for t in txns)
    period.flagged_count = sum(t.recon_status == "flagged" for t in txns)
    period.unreconciled_count = sum(t.recon_status == "unreconciled" for t in txns)
    db.commit()
    return period


def set_match(db: Session, txn, user_id, je=None, status="matched", reason="", by="auto"):
    match = txn.recon_match or ReconciliationMatch(user_id=user_id, transaction_id=txn.id)
    match.journal_entry_id = je.id if je else None
    match.status = status
    match.flag_reason = reason
    match.matched_by = by
    db.add(match)
    txn.recon_status = "flagged" if status == "flagged" else "matched"


def run_matching(db: Session, user_id, year, month):
    period = get_period(db, user_id, year, month)
    if period.status == "closed":
        raise ValueError("Period is closed — re-matching is not allowed.")

    pool = month_entries(db, user_id, year, month)
    used = set()
    result = {"matched": 0, "flagged": 0}
    for txn in month_txns(db, user_id, year, month, "unreconciled"):
        best, best_score = None, 0
        for je in pool:
            if je.id in used:
                continue
            score = 0
            if abs(txn.value - abs(je.amount)) <= AMOUNT_TOLERANCE:
                score += 3
            if abs((txn.date - je.posting_date).days) <= DATE_TOLERANCE:
                score += 2
            if txn.reference_number and txn.reference_number == je.reference_number:
                score += 5
            if score > best_score:
                best, best_score = je, score

        if best and best_score >= 3:
            set_match(db, txn, user_id, best)
            used.add(best.id)
            result["matched"] += 1
        else:
            available = [je for je in pool if je.id not in used]
            if not available:
                reason = "No journal entries found for this period."
            elif not any(abs(txn.value - abs(je.amount)) <= AMOUNT_TOLERANCE for je in available):
                reason = f"No journal entry found with matching amount (R {txn.value})."
            else:
                reason = "Amount found but date or reference mismatch — review manually."
            set_match(db, txn, user_id, status="flagged", reason=reason)
            result["flagged"] += 1
    db.commit()
    refresh_counts(db, period)
    return result


def fetch_entries(db: Session, user_id, year, month):
    config = db.scalar(select(ERPNextConfig).where(ERPNextConfig.user_id == user_id, ERPNextConfig.is_active.is_(True)))
    if not config:
        raise ValueError("No active ERPNext configuration.")
    start, end = month_bounds(year, month)
    entries = ERPNextClient(config).fetch_journal_entries(start.isoformat(), end.isoformat())
    created = 0
    for e in entries:
        exists = db.scalar(select(ERPNextJournalEntry.id).where(
            ERPNextJournalEntry.user_id == user_id, ERPNextJournalEntry.je_name == e["name"]))
        if exists:
            continue
        db.add(ERPNextJournalEntry(
            user_id=user_id, je_name=e["name"],
            posting_date=date.fromisoformat(str(e.get("posting_date") or start)),
            amount=Decimal(str(e.get("total_debit") or e.get("total_credit") or 0)),
            # LSuite writes the bank reference into cheque_no when it creates the JE.
            reference_number=(e.get("cheque_no") or e.get("user_remark") or "")[:255],
            remark=e.get("remark") or e.get("user_remark") or "",
        ))
        created += 1
    db.commit()
    return len(entries), created


def months_with_transactions(db: Session, user_id):
    y, m = extract("year", BankTransaction.date), extract("month", BankTransaction.date)
    rows = db.execute(
        select(y, m, func.count()).where(BankTransaction.user_id == user_id).group_by(y, m).order_by(y.desc(), m.desc())
    )
    return [{"year": int(a), "month": int(b), "count": c} for a, b, c in rows]
