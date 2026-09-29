import csv
import io
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..deps import current_user, get_db, get_owned
from ..models import BankTransaction, ERPNextJournalEntry, ReconciliationMatch, ReconciliationPeriod, User, utcnow
from ..schemas import JournalEntryOut, MatchIn, MatchOut, PeriodIn, PeriodOut, TransactionOut
from ..services import reconcile

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


def valid_month(year, month):
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        raise HTTPException(400, "Invalid period.")


@router.get("/journal-entries", response_model=list[JournalEntryOut])
def journal_entries(year: Optional[int] = None, month: Optional[int] = None, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if year and month:
        return reconcile.month_entries(db, user.id, year, month)
    return db.scalars(select(ERPNextJournalEntry).where(ERPNextJournalEntry.user_id == user.id)
                      .order_by(ERPNextJournalEntry.posting_date.desc()).limit(1000)).all()


@router.get("/matches", response_model=list[MatchOut])
def matches(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(ReconciliationMatch).where(ReconciliationMatch.user_id == user.id)
                      .order_by(ReconciliationMatch.matched_at.desc()).limit(2000)).all()


@router.post("/matches", response_model=MatchOut, status_code=201)
def create_match(body: MatchIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, body.transaction, user)
    je = get_owned(db, ERPNextJournalEntry, body.journal_entry, user) if body.journal_entry else None
    status = body.status if body.status in ("matched", "flagged", "manual") else "manual"
    reconcile.set_match(db, txn, user.id, je, status, body.flag_reason, "manual")
    db.commit()
    return txn.recon_match


@router.delete("/matches/{match_id}", status_code=204)
def delete_match(match_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    match = get_owned(db, ReconciliationMatch, match_id, user)
    match.transaction.recon_status = "unreconciled"
    db.delete(match)
    db.commit()


@router.get("/periods", response_model=list[PeriodOut])
def periods(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(ReconciliationPeriod).where(ReconciliationPeriod.user_id == user.id)
                      .order_by(ReconciliationPeriod.year.desc(), ReconciliationPeriod.month.desc())).all()


@router.post("/periods", response_model=PeriodOut, status_code=201)
def create_period(body: PeriodIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return reconcile.refresh_counts(db, reconcile.get_period(db, user.id, body.year, body.month))


@router.get("/months")
def months(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"months": reconcile.months_with_transactions(db, user.id)}


@router.get("/month/{year}/{month}")
def period_detail(year: int, month: int, status: str = "", user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    valid_month(year, month)
    period = reconcile.refresh_counts(db, reconcile.get_period(db, user.id, year, month))
    txns = reconcile.month_txns(db, user.id, year, month, status or None)
    return {
        "period": PeriodOut.model_validate(period),
        "transactions": [
            {**TransactionOut.model_validate(t).model_dump(),
             "match": MatchOut.model_validate(t.recon_match).model_dump() if t.recon_match else None,
             "journal_entry_name": t.recon_match.journal_entry.je_name if t.recon_match and t.recon_match.journal_entry else ""}
            for t in txns
        ],
        "journal_entries": [JournalEntryOut.model_validate(j) for j in reconcile.month_entries(db, user.id, year, month)],
    }


@router.post("/month/{year}/{month}/fetch")
def fetch(year: int, month: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    valid_month(year, month)
    try:
        total, created = reconcile.fetch_entries(db, user.id, year, month)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"ERPNext fetch failed: {e}")
    return {"message": f"Fetched {total} journal entries ({created} new)."}


@router.post("/month/{year}/{month}/match")
def match(year: int, month: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    valid_month(year, month)
    try:
        r = reconcile.run_matching(db, user.id, year, month)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": f"Matching complete — {r['matched']} matched, {r['flagged']} flagged.", **r}


@router.post("/month/{year}/{month}/close", response_model=PeriodOut)
def close(year: int, month: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    valid_month(year, month)
    period = reconcile.refresh_counts(db, reconcile.get_period(db, user.id, year, month))
    if not period.can_close:
        raise HTTPException(400, "Cannot close — unreconciled or flagged transactions remain.")
    period.status, period.closed_at = "closed", utcnow()
    db.commit()
    return period


@router.post("/periods/{period_id}/close", response_model=PeriodOut)
def close_by_id(period_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_owned(db, ReconciliationPeriod, period_id, user)
    return close(p.year, p.month, user, db)


@router.post("/month/{year}/{month}/reopen", response_model=PeriodOut)
def reopen(year: int, month: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    valid_month(year, month)
    period = reconcile.get_period(db, user.id, year, month)
    period.status, period.closed_at = "open", None
    db.commit()
    return period


@router.get("/month/{year}/{month}/export")
def export(year: int, month: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    valid_month(year, month)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Description", "Amount", "Type", "Recon Status", "Matched JE", "Flag Reason"])
    for t in reconcile.month_txns(db, user.id, year, month):
        m = t.recon_match
        w.writerow([t.date, t.description, t.value, t.direction, t.recon_status,
                    m.journal_entry.je_name if m and m.journal_entry else "", m.flag_reason if m else ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="recon_{year}_{month:02d}.csv"'})


@router.post("/transactions/{txn_id}/match")
def manual_match(txn_id: int, journal_entry_id: int = Body(..., embed=True), user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, user)
    je = get_owned(db, ERPNextJournalEntry, journal_entry_id, user)
    reconcile.set_match(db, txn, user.id, je, "manual", "", "manual")
    db.commit()
    return {"message": "Transaction manually matched."}


@router.post("/transactions/{txn_id}/unmatch")
def unmatch(txn_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, user)
    db.execute(delete(ReconciliationMatch).where(ReconciliationMatch.transaction_id == txn.id))
    txn.recon_status = "unreconciled"
    db.commit()
    return {"message": "Match removed — transaction is unreconciled."}
