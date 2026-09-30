from datetime import date
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import Ctx, client_ctx, current_user, get_db, get_owned, owner
from ..models import (
    BankAccount, BankTransaction, Client, EmailStatement, ERPNextConfig, ERPNextSyncLog, Job, PDFImportJob,
    ReconciliationPeriod, TransactionCategory, User,
)
from ..schemas import (
    BankAccountIn, BankAccountOut, CategoryIn, CategoryOut, ClientIn, ClientOut, ERPNextConfigIn, ERPNextConfigOut,
    JobOut, PDFJobOut, StatementIn, StatementOut, SyncLogOut, TransactionIn, TransactionOut,
)
from ..security import seal
from ..services import intercompany
from ..services.erpnext import account_map, set_category_account

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


# ── Companies (clients) ─────────────────────────────────────────────────────

@router.get("/clients", response_model=list[ClientOut])
def list_clients(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Client).where(Client.practice_id == user.practice_id).order_by(Client.name)).all()


@router.post("/clients", response_model=ClientOut, status_code=201)
def create_client(body: ClientIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not (body.name or "").strip():
        raise HTTPException(400, {"name": ["This field is required."]})
    client = Client(practice_id=user.practice_id)
    apply(client, body.model_dump(exclude_none=True))
    db.add(client)
    db.commit()
    return client


@router.get("/clients/{client_id}", response_model=ClientOut)
def get_client(client_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, Client, client_id, user)


@router.patch("/clients/{client_id}", response_model=ClientOut)
def update_client(client_id: int, body: ClientIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    client = get_owned(db, Client, client_id, user)
    apply(client, body.model_dump(exclude_unset=True, exclude_none=True))
    db.commit()
    return client


@router.delete("/clients/{client_id}", status_code=204)
def delete_client(client_id: int, user: User = Depends(owner), db: Session = Depends(get_db)):
    db.delete(get_owned(db, Client, client_id, user))
    db.commit()


# ── Dashboard: one row per company ──────────────────────────────────────────

@router.get("/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    today = date.today()
    month_start = today.replace(day=1)
    last_month = (month_start.replace(year=month_start.year - 1, month=12) if month_start.month == 1
                  else month_start.replace(month=month_start.month - 1))
    clients = db.scalars(select(Client).where(Client.practice_id == user.practice_id, Client.is_active.is_(True))
                         .order_by(Client.name)).all()

    def count(cid, *where):
        return db.scalar(select(func.count()).select_from(BankTransaction).where(BankTransaction.client_id == cid, *where))

    rows = []
    for c in clients:
        period = db.scalar(select(ReconciliationPeriod).where(
            ReconciliationPeriod.client_id == c.id, ReconciliationPeriod.year == last_month.year,
            ReconciliationPeriod.month == last_month.month))
        rows.append({
            "client": ClientOut.model_validate(c),
            "transactions": count(c.id),
            "this_month": count(c.id, BankTransaction.date >= month_start),
            "uncategorized": count(c.id, BankTransaction.category_id.is_(None)),
            "not_synced": count(c.id, BankTransaction.category_id.is_not(None), BankTransaction.erpnext_synced.is_(False)),
            "latest_transaction": db.scalar(select(func.max(BankTransaction.date)).where(BankTransaction.client_id == c.id)),
            "last_month_closed": bool(period and period.status == "closed"),
        })
    inbox = db.scalar(select(func.count()).select_from(EmailStatement).where(
        EmailStatement.practice_id == user.practice_id, EmailStatement.client_id.is_(None)))
    return {"companies": rows, "unassigned_statements": inbox, "last_month": last_month.strftime("%B %Y")}


# ── Bank accounts ───────────────────────────────────────────────────────────

@router.get("/accounts", response_model=list[BankAccountOut])
def list_accounts(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    return db.scalars(select(BankAccount).where(BankAccount.client_id == ctx.client_id)
                      .order_by(BankAccount.account_name)).all()


@router.post("/accounts", response_model=BankAccountOut, status_code=201)
def create_account(body: BankAccountIn, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    if not body.account_name:
        raise HTTPException(400, {"account_name": ["This field is required."]})
    acct = BankAccount(client_id=ctx.client_id)
    apply(acct, body.model_dump(exclude_none=True))
    db.add(acct)
    db.commit()
    return acct


@router.patch("/accounts/{acct_id}", response_model=BankAccountOut)
def update_account(acct_id: int, body: BankAccountIn, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    acct = get_owned(db, BankAccount, acct_id, ctx)
    apply(acct, body.model_dump(exclude_unset=True, exclude_none=True))
    db.commit()
    return acct


@router.delete("/accounts/{acct_id}", status_code=204)
def delete_account(acct_id: int, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    db.delete(get_owned(db, BankAccount, acct_id, ctx))
    db.commit()


# ── Categories (shared across the organisation's companies) ─────────────────

def _optional_client(request: Request, user: User, db: Session) -> Optional[Client]:
    raw = request.headers.get("x-client-id") or ""
    client = db.get(Client, int(raw)) if raw.isdigit() else None
    return client if client and client.practice_id == user.practice_id else None


def _category_out(cat, accounts):
    return CategoryOut.model_validate(cat).model_copy(update={"erpnext_account": accounts.get(cat.id, "")})


@router.get("/categories", response_model=list[CategoryOut])
def list_categories(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    client = _optional_client(request, user, db)
    accounts = account_map(db, client.id) if client else {}
    cats = db.scalars(select(TransactionCategory).where(TransactionCategory.practice_id == user.practice_id)
                      .order_by(TransactionCategory.name))
    return [_category_out(c, accounts) for c in cats]


@router.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(body: CategoryIn, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if not (body.name or "").strip():
        raise HTTPException(400, {"name": ["This field is required."]})
    cat = TransactionCategory(practice_id=user.practice_id)
    apply(cat, body.model_dump(exclude_none=True, exclude={"erpnext_account"}))
    db.add(cat)
    commit_or_400(db, "A category with that name already exists.")
    client = _optional_client(request, user, db)
    if client and body.erpnext_account:
        set_category_account(db, client.id, cat.id, body.erpnext_account)
        db.commit()
    return _category_out(cat, account_map(db, client.id) if client else {})


@router.patch("/categories/{cat_id}", response_model=CategoryOut)
def update_category(cat_id: int, body: CategoryIn, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    cat = get_owned(db, TransactionCategory, cat_id, user)
    data = body.model_dump(exclude_unset=True)
    account = data.pop("erpnext_account", None)
    apply(cat, {k: v for k, v in data.items() if v is not None})
    client = _optional_client(request, user, db)
    if "erpnext_account" in body.model_fields_set:
        if not client:
            raise HTTPException(400, "Select a company to set its ERPNext account.")
        set_category_account(db, client.id, cat.id, account or "")
    commit_or_400(db, "A category with that name already exists.")
    return _category_out(cat, account_map(db, client.id) if client else {})


@router.delete("/categories/{cat_id}", status_code=204)
def delete_category(cat_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_owned(db, TransactionCategory, cat_id, user))
    db.commit()


# ── Statements ──────────────────────────────────────────────────────────────

@router.get("/statements", response_model=list[StatementOut])
def list_statements(request: Request, unassigned: bool = False, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    q = select(EmailStatement).where(EmailStatement.practice_id == user.practice_id)
    if unassigned:
        q = q.where(EmailStatement.client_id.is_(None))
    else:
        client = _optional_client(request, user, db)
        if not client:
            raise HTTPException(400, "Select a company first.")
        q = q.where(EmailStatement.client_id == client.id)
    return db.scalars(q.order_by(EmailStatement.received_date.desc(), EmailStatement.id.desc()).limit(500)).all()


@router.patch("/statements/{st_id}", response_model=StatementOut)
def assign_statement(st_id: int, body: StatementIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Moves an inbox statement onto a company (and optionally one of its bank accounts)."""
    st = get_owned(db, EmailStatement, st_id, user)
    if body.client_id is not None:
        client = get_owned(db, Client, body.client_id, user)
        if st.client_id != client.id and db.scalar(select(func.count()).select_from(BankTransaction)
                                                   .where(BankTransaction.statement_id == st.id)):
            raise HTTPException(400, "This statement already has transactions; delete them first.")
        st.client_id, st.bank_account_id = client.id, None
    if body.bank_account_id is not None:
        ba = db.get(BankAccount, body.bank_account_id)
        if not ba or ba.client_id != st.client_id:
            raise HTTPException(400, "That bank account belongs to a different company.")
        st.bank_account_id = ba.id
        st.bank_name = ba.bank_name or st.bank_name
    db.commit()
    return st


@router.delete("/statements/{st_id}", status_code=204)
def delete_statement(st_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_owned(db, EmailStatement, st_id, user))
    db.commit()


# ── Transactions ────────────────────────────────────────────────────────────

@router.get("/transactions", response_model=list[TransactionOut])
def list_transactions(
    category: Optional[int] = None, recon_status: Optional[str] = None, bank_account: Optional[int] = None,
    uncategorized: bool = False, not_synced: bool = False, q: str = "", limit: int = 500, offset: int = 0,
    ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db),
):
    stmt = select(BankTransaction).where(BankTransaction.client_id == ctx.client_id)
    if category:
        stmt = stmt.where(BankTransaction.category_id == category)
    if recon_status:
        stmt = stmt.where(BankTransaction.recon_status == recon_status)
    if bank_account:
        stmt = stmt.where(BankTransaction.bank_account_id == bank_account)
    if uncategorized:
        stmt = stmt.where(BankTransaction.category_id.is_(None))
    if not_synced:
        stmt = stmt.where(BankTransaction.erpnext_synced.is_(False))
    if q:
        stmt = stmt.where(BankTransaction.description.ilike(f"%{q}%"))
    stmt = stmt.order_by(BankTransaction.date.desc(), BankTransaction.id.desc()).offset(offset).limit(min(limit, 5000))
    return db.scalars(stmt).unique().all()


TXN_RENAME = {"bank_account": "bank_account_id", "category": "category_id"}


def _check_refs(db, ctx, data):
    if data.get("bank_account"):
        get_owned(db, BankAccount, data["bank_account"], ctx)
    if data.get("category"):
        get_owned(db, TransactionCategory, data["category"], ctx.user)


@router.post("/transactions", response_model=TransactionOut, status_code=201)
def create_transaction(body: TransactionIn, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_none=True)
    if not data.get("date") or not data.get("description"):
        raise HTTPException(400, "date and description are required.")
    _check_refs(db, ctx, data)
    txn = BankTransaction(client_id=ctx.client_id)
    apply(txn, data, TXN_RENAME)
    txn.amount = txn.withdrawal or txn.deposit
    txn.transaction_type = txn.transaction_type or ("debit" if txn.withdrawal else "credit")
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


@router.get("/transactions/{txn_id}", response_model=TransactionOut)
def get_transaction(txn_id: int, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    return get_owned(db, BankTransaction, txn_id, ctx)


@router.patch("/transactions/{txn_id}", response_model=TransactionOut)
def update_transaction(txn_id: int, body: TransactionIn, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, ctx)
    data = body.model_dump(exclude_unset=True)
    _check_refs(db, ctx, data)
    apply(txn, data, TXN_RENAME)
    db.commit()
    db.refresh(txn)
    return txn


@router.delete("/transactions/{txn_id}", status_code=204)
def delete_transaction(txn_id: int, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    db.delete(get_owned(db, BankTransaction, txn_id, ctx))
    db.commit()


# ── Intercompany transfers ──────────────────────────────────────────────────

@router.get("/intercompany")
def intercompany_pairs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"pairs": intercompany.find_pairs(db, user.practice_id)}


@router.post("/intercompany/confirm")
def intercompany_confirm(out_id: int = Body(..., embed=True), in_id: int = Body(..., embed=True),
                         user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        intercompany.confirm_pair(db, user.practice_id, out_id, in_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": "Both sides booked as an intercompany transfer."}


# ── ERPNext connection (one per organisation) ───────────────────────────────

@router.get("/erpnext-configs", response_model=list[ERPNextConfigOut])
def list_configs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(ERPNextConfig).where(ERPNextConfig.practice_id == user.practice_id)
                      .order_by(ERPNextConfig.name)).all()


@router.post("/erpnext-configs", response_model=ERPNextConfigOut, status_code=201)
def create_config(body: ERPNextConfigIn, user: User = Depends(owner), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_none=True)
    missing = [f for f in ("name", "base_url", "api_key", "api_secret") if not data.get(f)]
    if missing:
        raise HTTPException(400, {f: ["This field is required."] for f in missing})
    for other in db.scalars(select(ERPNextConfig).where(ERPNextConfig.practice_id == user.practice_id)):
        other.is_active = False
    cfg = ERPNextConfig(practice_id=user.practice_id, is_active=True)
    apply(cfg, {**data, "api_key": seal(data["api_key"]), "api_secret": seal(data["api_secret"]), "is_active": True})
    db.add(cfg)
    db.commit()
    return cfg


@router.patch("/erpnext-configs/{cfg_id}", response_model=ERPNextConfigOut)
def update_config(cfg_id: int, body: ERPNextConfigIn, user: User = Depends(owner), db: Session = Depends(get_db)):
    cfg = get_owned(db, ERPNextConfig, cfg_id, user)
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    for k in ("api_key", "api_secret"):  # blank on edit = keep current
        if data.get(k):
            data[k] = seal(data[k])
        else:
            data.pop(k, None)
    if data.get("is_active"):
        for other in db.scalars(select(ERPNextConfig).where(ERPNextConfig.practice_id == user.practice_id)):
            other.is_active = False
    apply(cfg, data)
    db.commit()
    return cfg


@router.delete("/erpnext-configs/{cfg_id}", status_code=204)
def delete_config(cfg_id: int, user: User = Depends(owner), db: Session = Depends(get_db)):
    db.delete(get_owned(db, ERPNextConfig, cfg_id, user))
    db.commit()


@router.get("/erpnext-sync-logs", response_model=list[SyncLogOut])
def list_sync_logs(limit: int = 100, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    return db.scalars(select(ERPNextSyncLog).where(ERPNextSyncLog.client_id == ctx.client_id)
                      .order_by(ERPNextSyncLog.sync_date.desc()).limit(min(limit, 2000))).all()


# ── Jobs ────────────────────────────────────────────────────────────────────

@router.get("/pdf-jobs", response_model=list[PDFJobOut])
def list_pdf_jobs(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    return db.scalars(select(PDFImportJob).where(PDFImportJob.client_id == ctx.client_id)
                      .order_by(PDFImportJob.created_at.desc()).limit(50)).all()


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, Job, job_id, user)
