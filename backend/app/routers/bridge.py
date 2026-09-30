from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import Ctx, client_ctx, current_user, get_db, get_owned
from ..jobs import start_job
from ..models import BankTransaction, TransactionCategory, User
from ..schemas import CategoryOut, TransactionOut
from ..services import categorize
from ..services.erpnext import account_map

router = APIRouter(prefix="/api/bridge", tags=["categorization"])


@router.get("/categories")
def category_stats(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    counts = {
        (cid, synced): n for cid, synced, n in db.execute(
            select(BankTransaction.category_id, BankTransaction.erpnext_synced, func.count())
            .where(BankTransaction.client_id == ctx.client_id, BankTransaction.category_id.is_not(None))
            .group_by(BankTransaction.category_id, BankTransaction.erpnext_synced)
        )
    }
    accounts = account_map(db, ctx.client_id)
    out = []
    for c in categorize.categories(db, ctx.client.practice_id, active_only=False):
        synced, pending = counts.get((c.id, True), 0), counts.get((c.id, False), 0)
        cat = CategoryOut.model_validate(c).model_copy(update={"erpnext_account": accounts.get(c.id, "")})
        out.append({"category": cat, "total": synced + pending, "synced": synced, "pending": pending})
    return {"categories": out}


@router.get("/categories/{cat_id}/transactions")
def category_transactions(cat_id: int, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    cat = get_owned(db, TransactionCategory, cat_id, ctx.user)
    txns = db.scalars(select(BankTransaction).where(BankTransaction.client_id == ctx.client_id,
                                                    BankTransaction.category_id == cat.id)
                      .order_by(BankTransaction.date.desc())).unique()
    out = CategoryOut.model_validate(cat).model_copy(update={"erpnext_account": account_map(db, ctx.client_id).get(cat.id, "")})
    return {"category": out, "transactions": [TransactionOut.model_validate(t) for t in txns]}


@router.get("/bulk-operations")
def bulk_stats(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    base = select(func.count()).select_from(BankTransaction).where(BankTransaction.client_id == ctx.client_id)
    categorized = BankTransaction.category_id.is_not(None)
    return {
        "stats": {
            "total": db.scalar(base),
            "uncategorized": db.scalar(base.where(BankTransaction.category_id.is_(None))),
            "categorized": db.scalar(base.where(categorized)),
            "synced": db.scalar(base.where(BankTransaction.erpnext_synced.is_(True))),
            "ready_to_sync": db.scalar(base.where(categorized, BankTransaction.erpnext_synced.is_(False))),
        },
        "ai_enabled": bool(settings.groq_api_keys),
    }


@router.post("/bulk-operations/auto-categorize")
def auto_categorize(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    categorized, total = categorize.auto_categorize(db, ctx.client)
    return {"categorized": categorized, "total": total}


@router.post("/bulk-operations/auto-categorize-ai", status_code=202)
def auto_categorize_ai(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    if not settings.groq_api_keys:
        raise HTTPException(400, "GROQ_API_KEYS is not configured on the server.")
    job = start_job(db, ctx.user, ctx.client, "ai_categorize")
    return {"message": "AI categorization job started.", "job_id": job.id}


@router.post("/bulk-operations/preview-categorization")
def preview(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    return categorize.preview(db, ctx.client)


@router.post("/classify")
def classify(transaction: str = Body("", embed=True), user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not transaction.strip():
        raise HTTPException(400, "transaction field required")
    return categorize.classify_text(db, user.practice_id, transaction.strip())


@router.post("/transactions/{txn_id}/categorize")
def categorize_one(txn_id: int, category_id: int = Body(None, embed=True), learn: bool = Body(True, embed=True),
                   ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, ctx)
    if txn.erpnext_synced:
        raise HTTPException(400, "Already synced to ERPNext.")
    if not category_id:
        raise HTTPException(400, "category_id is required")
    cat = get_owned(db, TransactionCategory, category_id, ctx.user)
    txn.category = cat
    # learn the merchant so the next import auto-matches
    first = (txn.description or "").split()[0].lower().strip("*#:") if txn.description else ""
    if learn and len(first) > 2 and not first.isdigit():
        cat.add_tag(first)
    db.commit()
    return {"message": f'Categorized as "{cat.name}".'}


@router.post("/transactions/{txn_id}/uncategorize")
def uncategorize_one(txn_id: int, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, ctx)
    if txn.erpnext_synced:
        raise HTTPException(400, "Cannot uncategorize a synced transaction.")
    txn.category_id = None
    db.commit()
    return {"message": "Transaction uncategorized."}
