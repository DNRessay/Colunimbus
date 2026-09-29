from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import current_user, get_db, get_owned
from ..jobs import start_job
from ..models import BankTransaction, ERPNextConfig, TransactionCategory, User
from ..schemas import CategoryOut, ERPNextConfigOut, TransactionOut
from ..services import categorize, erpnext

router = APIRouter(prefix="/api/bridge", tags=["categorization"])


def active_config(db, user):
    return db.scalar(select(ERPNextConfig).where(ERPNextConfig.user_id == user.id, ERPNextConfig.is_active.is_(True)))


@router.get("/categories")
def category_stats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    counts = {
        (cid, synced): n for cid, synced, n in db.execute(
            select(BankTransaction.category_id, BankTransaction.erpnext_synced, func.count())
            .where(BankTransaction.user_id == user.id, BankTransaction.category_id.is_not(None))
            .group_by(BankTransaction.category_id, BankTransaction.erpnext_synced)
        )
    }
    out = []
    for c in db.scalars(select(TransactionCategory).order_by(TransactionCategory.name)):
        synced, pending = counts.get((c.id, True), 0), counts.get((c.id, False), 0)
        out.append({"category": CategoryOut.model_validate(c), "total": synced + pending, "synced": synced,
                    "pending": pending, "is_junk": categorize.is_junk(c.name)})
    return {"categories": out}


@router.get("/categories/{cat_id}/transactions")
def category_transactions(cat_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cat = get_owned(db, TransactionCategory, cat_id, user)
    txns = db.scalars(select(BankTransaction).where(BankTransaction.user_id == user.id, BankTransaction.category_id == cat.id)
                      .order_by(BankTransaction.date.desc())).unique()
    return {"category": CategoryOut.model_validate(cat), "is_junk": categorize.is_junk(cat.name),
            "transactions": [TransactionOut.model_validate(t) for t in txns]}


@router.get("/bulk-operations")
def bulk_stats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    junk = categorize.junk_ids(db)
    base = select(func.count()).select_from(BankTransaction).where(BankTransaction.user_id == user.id)
    not_junk = BankTransaction.category_id.not_in(junk) if junk else BankTransaction.category_id.is_not(None)
    needs = list(db.scalars(categorize.needs_categorizing(db, user.id)).unique())
    config = active_config(db, user)
    recent = db.scalars(select(BankTransaction).where(BankTransaction.user_id == user.id)
                        .order_by(BankTransaction.date.desc()).limit(10)).unique()
    return {
        "stats": {
            "total": db.scalar(base),
            "uncategorized": len(needs),
            "categorized": db.scalar(base.where(BankTransaction.category_id.is_not(None), not_junk)),
            "synced": db.scalar(base.where(BankTransaction.erpnext_synced.is_(True))),
            "ready_to_sync": db.scalar(base.where(BankTransaction.category_id.is_not(None), not_junk,
                                                  BankTransaction.erpnext_synced.is_(False))),
            "junk_categorized": db.scalar(base.where(BankTransaction.category_id.in_(junk),
                                                     BankTransaction.erpnext_synced.is_(False))) if junk else 0,
            "truly_null": db.scalar(base.where(BankTransaction.category_id.is_(None), BankTransaction.erpnext_synced.is_(False))),
        },
        "ai_enabled": bool(settings.groq_api_keys),
        "erpnext_config": ERPNextConfigOut.model_validate(config) if config else None,
        "recent_transactions": [TransactionOut.model_validate(t) for t in recent],
        "needs_categorizing": [TransactionOut.model_validate(t) for t in needs[:500]],
    }


@router.post("/bulk-operations/auto-categorize")
def auto_categorize(user: User = Depends(current_user), db: Session = Depends(get_db)):
    categorized, total = categorize.auto_categorize(db, user.id)
    return {"categorized": categorized, "total": total}


@router.post("/bulk-operations/auto-categorize-ai", status_code=202)
def auto_categorize_ai(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not settings.groq_api_keys:
        raise HTTPException(400, "GROQ_API_KEYS is not configured on the server.")
    job = start_job(db, user.id, "ai_categorize")
    return {"message": "AI categorization job started.", "job_id": job.id}


@router.post("/bulk-operations/preview-categorization")
def preview(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return categorize.preview(db, user.id)


@router.post("/bulk-operations/sync-to-erpnext")
def bulk_sync(user: User = Depends(current_user), db: Session = Depends(get_db)):
    config = active_config(db, user)
    if not config:
        raise HTTPException(400, "No active ERPNext configuration found.")
    ok, failed, total = erpnext.sync_all_ready(db, config)
    if total == 0:
        msg = "No transactions ready to sync."
    elif failed == 0:
        msg = f"Synced {ok} of {total} transactions."
    else:
        msg = f"Synced {ok}, failed {failed} out of {total}."
    return {"message": msg, "success": ok, "failed": failed, "total": total}


@router.post("/classify")
def classify(transaction: str = Body("", embed=True), user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not transaction.strip():
        raise HTTPException(400, "transaction field required")
    return categorize.classify_text(db, transaction.strip())


@router.post("/transactions/{txn_id}/categorize")
def categorize_one(txn_id: int, category_id: int = Body(None, embed=True), user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, user)
    if not category_id:
        raise HTTPException(400, "category_id is required")
    cat = get_owned(db, TransactionCategory, category_id, user)
    txn.category_id = cat.id
    # learn the merchant so the next import auto-matches
    first = (txn.description or "").split()[0].lower().strip("*") if txn.description else ""
    if len(first) > 2:
        cat.add_tag(first)
    db.commit()
    return {"message": f'Categorized as "{cat.name}".'}


@router.post("/transactions/{txn_id}/uncategorize")
def uncategorize_one(txn_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, user)
    if txn.erpnext_synced:
        raise HTTPException(400, "Cannot uncategorize a synced transaction.")
    txn.category_id = None
    db.commit()
    return {"message": "Transaction uncategorized."}
