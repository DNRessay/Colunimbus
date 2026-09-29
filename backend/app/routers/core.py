from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import current_user, get_db, get_owned
from ..models import (
    BankAccount, BankTransaction, EmailStatement, ERPNextConfig, ERPNextSyncLog, Invoice, InvoiceItem, Job,
    PDFImportJob, TransactionCategory, User,
)
from ..schemas import (
    BankAccountIn, BankAccountOut, CategoryIn, CategoryOut, ERPNextConfigIn, ERPNextConfigOut, InvoiceIn,
    InvoiceOut, JobOut, PDFJobOut, StatementOut, SyncLogOut, TransactionIn, TransactionOut,
)
from ..services.categorize import junk_ids

router = APIRouter(prefix="/api", tags=["core"])


def apply(obj, data: dict, rename: Optional[dict] = None):
    for k, v in data.items():
        setattr(obj, (rename or {}).get(k, k), v)


def commit_or_400(db: Session, message="A record with these values already exists."):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, message)


@router.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        ok = True
    except Exception:
        ok = False
    return {"status": "ok" if ok else "degraded", "database": ok}


@router.get("/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    def count(model, *where):
        return db.scalar(select(func.count()).select_from(model).where(model.user_id == user.id, *where))

    statements = db.scalars(select(EmailStatement).where(EmailStatement.user_id == user.id)
                            .order_by(EmailStatement.received_date.desc()).limit(5))
    txns = db.scalars(select(BankTransaction).where(BankTransaction.user_id == user.id)
                      .order_by(BankTransaction.date.desc(), BankTransaction.id.desc()).limit(10))
    return {
        "stats": {
            "statements": count(EmailStatement),
            "transactions": count(BankTransaction),
            "categorized": count(BankTransaction, BankTransaction.category_id.is_not(None)),
            "synced": count(BankTransaction, BankTransaction.erpnext_synced.is_(True)),
        },
        "recent_statements": [StatementOut.model_validate(s) for s in statements],
        "recent_transactions": [TransactionOut.model_validate(t) for t in txns.unique()],
    }


# ── Bank accounts ───────────────────────────────────────────────────────────

@router.get("/accounts", response_model=list[BankAccountOut])
def list_accounts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(BankAccount).where(BankAccount.user_id == user.id).order_by(BankAccount.account_name)).all()


@router.post("/accounts", response_model=BankAccountOut, status_code=201)
def create_account(body: BankAccountIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not body.account_name:
        raise HTTPException(400, {"account_name": ["This field is required."]})
    acct = BankAccount(user_id=user.id)
    apply(acct, body.model_dump(exclude_none=True))
    db.add(acct)
    db.commit()
    return acct


@router.get("/accounts/{acct_id}", response_model=BankAccountOut)
def get_account(acct_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, BankAccount, acct_id, user)


@router.patch("/accounts/{acct_id}", response_model=BankAccountOut)
def update_account(acct_id: int, body: BankAccountIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acct = get_owned(db, BankAccount, acct_id, user)
    apply(acct, body.model_dump(exclude_unset=True, exclude_none=True))
    db.commit()
    return acct


@router.delete("/accounts/{acct_id}", status_code=204)
def delete_account(acct_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_owned(db, BankAccount, acct_id, user))
    db.commit()


# ── Categories (shared across users) ────────────────────────────────────────

@router.get("/categories", response_model=list[CategoryOut])
def list_categories(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(TransactionCategory).order_by(TransactionCategory.name)).all()


@router.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(body: CategoryIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not body.name:
        raise HTTPException(400, {"name": ["This field is required."]})
    cat = TransactionCategory(transaction_type="debit")
    apply(cat, body.model_dump(exclude_none=True))
    db.add(cat)
    commit_or_400(db, "A category with that name already exists.")
    return cat


@router.get("/categories/{cat_id}", response_model=CategoryOut)
def get_category(cat_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, TransactionCategory, cat_id, user)


@router.patch("/categories/{cat_id}", response_model=CategoryOut)
def update_category(cat_id: int, body: CategoryIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cat = get_owned(db, TransactionCategory, cat_id, user)
    apply(cat, body.model_dump(exclude_unset=True))
    commit_or_400(db, "A category with that name already exists.")
    return cat


@router.delete("/categories/{cat_id}", status_code=204)
def delete_category(cat_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_owned(db, TransactionCategory, cat_id, user))
    db.commit()


# ── Statements (created by Gmail / uploads) ─────────────────────────────────

@router.get("/statements", response_model=list[StatementOut])
def list_statements(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(EmailStatement).where(EmailStatement.user_id == user.id)
                      .order_by(EmailStatement.received_date.desc(), EmailStatement.id.desc())).all()


@router.get("/statements/{st_id}", response_model=StatementOut)
def get_statement(st_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, EmailStatement, st_id, user)


@router.delete("/statements/{st_id}", status_code=204)
def delete_statement(st_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_owned(db, EmailStatement, st_id, user))
    db.commit()


# ── Invoices (local) ────────────────────────────────────────────────────────

def _set_items(inv: Invoice, items):
    inv.items = [InvoiceItem(**i.model_dump(), total=0) for i in items]


@router.get("/invoices", response_model=list[InvoiceOut])
def list_invoices(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Invoice).where(Invoice.user_id == user.id).order_by(Invoice.invoice_date.desc())).all()


@router.post("/invoices", response_model=InvoiceOut, status_code=201)
def create_invoice(body: InvoiceIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not body.invoice_number or not body.invoice_date or not body.customer_name:
        raise HTTPException(400, "invoice_number, invoice_date and customer_name are required.")
    inv = Invoice(user_id=user.id)
    apply(inv, body.model_dump(exclude_none=True, exclude={"items"}))
    _set_items(inv, body.items or [])
    inv.calculate_totals()
    db.add(inv)
    commit_or_400(db, "An invoice with that number already exists.")
    return inv


@router.get("/invoices/{inv_id}", response_model=InvoiceOut)
def get_invoice(inv_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, Invoice, inv_id, user)


@router.patch("/invoices/{inv_id}", response_model=InvoiceOut)
def update_invoice(inv_id: int, body: InvoiceIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    inv = get_owned(db, Invoice, inv_id, user)
    apply(inv, body.model_dump(exclude_unset=True, exclude_none=True, exclude={"items"}))
    if body.items is not None:
        _set_items(inv, body.items)
    inv.calculate_totals()
    commit_or_400(db, "An invoice with that number already exists.")
    return inv


@router.delete("/invoices/{inv_id}", status_code=204)
def delete_invoice(inv_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_owned(db, Invoice, inv_id, user))
    db.commit()


# ── Transactions ────────────────────────────────────────────────────────────

@router.get("/transactions", response_model=list[TransactionOut])
def list_transactions(
    category: Optional[int] = None, recon_status: Optional[str] = None, bank_account: Optional[int] = None,
    uncategorized: bool = False, not_synced: bool = False, q: str = "", limit: int = 500, offset: int = 0,
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    stmt = select(BankTransaction).where(BankTransaction.user_id == user.id)
    if category:
        stmt = stmt.where(BankTransaction.category_id == category)
    if recon_status:
        stmt = stmt.where(BankTransaction.recon_status == recon_status)
    if bank_account:
        stmt = stmt.where(BankTransaction.bank_account_id == bank_account)
    if uncategorized:
        junk = junk_ids(db)
        cond = BankTransaction.category_id.is_(None)
        stmt = stmt.where(or_(cond, BankTransaction.category_id.in_(junk)) if junk else cond)
    if not_synced:
        stmt = stmt.where(BankTransaction.erpnext_synced.is_(False))
    if q:
        stmt = stmt.where(BankTransaction.description.ilike(f"%{q}%"))
    stmt = stmt.order_by(BankTransaction.date.desc(), BankTransaction.id.desc()).offset(offset).limit(min(limit, 5000))
    return db.scalars(stmt).unique().all()


def _check_refs(db, user, data):
    if data.get("bank_account"):
        get_owned(db, BankAccount, data["bank_account"], user)
    if data.get("category") and not db.get(TransactionCategory, data["category"]):
        raise HTTPException(400, {"category": ["Invalid category."]})


TXN_RENAME = {"bank_account": "bank_account_id", "category": "category_id"}


@router.post("/transactions", response_model=TransactionOut, status_code=201)
def create_transaction(body: TransactionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_none=True)
    if not data.get("date") or not data.get("description"):
        raise HTTPException(400, "date and description are required.")
    _check_refs(db, user, data)
    txn = BankTransaction(user_id=user.id)
    apply(txn, data, TXN_RENAME)
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


@router.get("/transactions/{txn_id}", response_model=TransactionOut)
def get_transaction(txn_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, BankTransaction, txn_id, user)


@router.patch("/transactions/{txn_id}", response_model=TransactionOut)
def update_transaction(txn_id: int, body: TransactionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, user)
    data = body.model_dump(exclude_unset=True)
    _check_refs(db, user, data)
    apply(txn, data, TXN_RENAME)
    db.commit()
    db.refresh(txn)
    return txn


@router.delete("/transactions/{txn_id}", status_code=204)
def delete_transaction(txn_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_owned(db, BankTransaction, txn_id, user))
    db.commit()


# ── ERPNext configs / logs ──────────────────────────────────────────────────

@router.get("/erpnext-configs", response_model=list[ERPNextConfigOut])
def list_configs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(ERPNextConfig).where(ERPNextConfig.user_id == user.id).order_by(ERPNextConfig.name)).all()


@router.post("/erpnext-configs", response_model=ERPNextConfigOut, status_code=201)
def create_config(body: ERPNextConfigIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_none=True)
    missing = [f for f in ("name", "base_url", "api_key", "api_secret") if not data.get(f)]
    if missing:
        raise HTTPException(400, {f: ["This field is required."] for f in missing})
    cfg = ERPNextConfig(user_id=user.id)
    apply(cfg, data)
    if cfg.is_active is not False:
        # only one active config per user
        for other in db.scalars(select(ERPNextConfig).where(ERPNextConfig.user_id == user.id)):
            other.is_active = False
        cfg.is_active = True
    db.add(cfg)
    db.commit()
    return cfg


@router.get("/erpnext-configs/{cfg_id}", response_model=ERPNextConfigOut)
def get_config(cfg_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, ERPNextConfig, cfg_id, user)


@router.patch("/erpnext-configs/{cfg_id}", response_model=ERPNextConfigOut)
def update_config(cfg_id: int, body: ERPNextConfigIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = get_owned(db, ERPNextConfig, cfg_id, user)
    # blank secrets on edit mean "keep the current one"
    apply(cfg, {k: v for k, v in body.model_dump(exclude_unset=True, exclude_none=True).items()
                if not (k in ("api_key", "api_secret") and not v)})
    db.commit()
    return cfg


@router.delete("/erpnext-configs/{cfg_id}", status_code=204)
def delete_config(cfg_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_owned(db, ERPNextConfig, cfg_id, user))
    db.commit()


@router.get("/erpnext-sync-logs", response_model=list[SyncLogOut])
def list_sync_logs(limit: int = 200, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(
        select(ERPNextSyncLog).join(ERPNextConfig).where(ERPNextConfig.user_id == user.id)
        .order_by(ERPNextSyncLog.sync_date.desc()).limit(min(limit, 2000))
    ).all()


# ── Jobs ────────────────────────────────────────────────────────────────────

@router.get("/pdf-jobs", response_model=list[PDFJobOut])
def list_pdf_jobs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(PDFImportJob).where(PDFImportJob.user_id == user.id)
                      .order_by(PDFImportJob.created_at.desc()).limit(100)).all()


@router.get("/pdf-jobs/{job_id}", response_model=PDFJobOut)
def get_pdf_job(job_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, PDFImportJob, job_id, user)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, Job, job_id, user)
