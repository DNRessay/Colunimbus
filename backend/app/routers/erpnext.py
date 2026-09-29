import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import current_user, get_db, get_owned
from ..jobs import start_job
from ..models import BankTransaction, ERPNextConfig, Job, User
from ..schemas import BankAccountOut, CategoryOut, ERPNextConfigOut
from ..services import erpnext
from ..services.erpnext import ERPNextClient

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/erpnext", tags=["erpnext"])


def active_config(db, user, required=True):
    cfg = db.scalar(select(ERPNextConfig).where(ERPNextConfig.user_id == user.id, ERPNextConfig.is_active.is_(True)))
    if not cfg and required:
        raise HTTPException(400, "No active ERPNext configuration.")
    return cfg


def pick_config(db, user, config_id: Optional[int]):
    if config_id:
        cfg = db.get(ERPNextConfig, config_id)
        if cfg and cfg.user_id == user.id:
            return cfg
    return active_config(db, user)


@router.post("/configs/{cfg_id}/test")
def test_config(cfg_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ok, msg = ERPNextClient(get_owned(db, ERPNextConfig, cfg_id, user)).test_connection()
    return {"success": ok, "message": msg}


@router.post("/configs/{cfg_id}/activate")
def activate_config(cfg_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = get_owned(db, ERPNextConfig, cfg_id, user)
    for other in db.scalars(select(ERPNextConfig).where(ERPNextConfig.user_id == user.id)):
        other.is_active = other.id == cfg.id
    db.commit()
    return {"message": f'"{cfg.name}" is now active.'}


@router.post("/transactions/{txn_id}/sync")
def sync_transaction(txn_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, user)
    if not txn.category_id:
        raise HTTPException(400, "Transaction must be categorized first")
    try:
        name = ERPNextClient(active_config(db, user)).create_journal_entry(db, txn)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"message": f"Synced: {name}", "journal_entry": name}


@router.get("/fetch-accounts")
def fetch_accounts(config_id: Optional[int] = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    raw = ERPNextClient(pick_config(db, user, config_id)).get_chart_of_accounts()
    if not raw:
        raise HTTPException(502, "No accounts returned from ERPNext.")
    accounts = sorted(
        ({"name": a["name"], "account_name": a.get("account_name") or a["name"],
          "account_type": a.get("account_type") or "", "root_type": a.get("root_type") or "",
          "company": a.get("company") or "", "is_group": bool(a.get("is_group"))} for a in raw),
        key=lambda a: (a["root_type"], a["name"]),
    )
    return {"accounts": accounts, "count": len(accounts)}


@router.get("/fetch-cost-centers")
def fetch_cost_centers(config_id: Optional[int] = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"cost_centers": ERPNextClient(pick_config(db, user, config_id)).get_cost_centers()}


@router.get("/fetch-companies")
def fetch_companies(config_id: Optional[int] = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"companies": ERPNextClient(pick_config(db, user, config_id)).get_companies()}


@router.post("/update-config-defaults")
def update_defaults(company: str = Body("", embed=True), bank_account: str = Body("", embed=True),
                    cost_center: str = Body("", embed=True), user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    cfg = active_config(db, user)
    if not company.strip():
        raise HTTPException(400, "Company is required")
    resolved = ERPNextClient(cfg).resolve_company(company.strip())
    cfg.default_company = resolved
    cfg.bank_account = bank_account.strip()
    cfg.default_cost_center = cost_center.strip()
    db.commit()
    note = f" (resolved from '{company}')" if resolved != company.strip() else ""
    return {"message": f"Defaults saved. Company: {resolved}{note}", "resolved_company": resolved}


@router.get("/sync-preflight")
def preflight(user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = active_config(db, user)
    cats, banks = erpnext.missing_payload(db, user.id)
    return {
        "config": ERPNextConfigOut.model_validate(cfg),
        "missing_categories": [CategoryOut.model_validate(c) for c in cats],
        "missing_bank_accounts": [BankAccountOut.model_validate(b) for b in banks],
        "ready_count": erpnext.ready_count(db, user.id),
    }


@router.post("/sync-preflight", status_code=202)
def submit_preflight(data: dict = Body(default_factory=dict), user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    """Saves the preflight form (config defaults + account mappings), then starts the sync job."""
    cfg = active_config(db, user)
    banks, cats = erpnext.apply_preflight(db, cfg, data)
    job = start_job(db, user.id, "erpnext_sync", config_id=cfg.id)
    return {"message": f"Saved ({banks} bank account(s), {cats} categories updated). Sync job started.",
            "job_id": job.id}


@router.post("/sync-now")
def sync_now(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ok, failed, total = erpnext.sync_all_ready(db, active_config(db, user))
    return {"success": ok, "failed": failed, "total": total}


@router.get("/sync-job-status")
def sync_job_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = db.scalar(select(Job).where(Job.user_id == user.id, Job.kind == "erpnext_sync").order_by(Job.id.desc()).limit(1))
    if not job:
        return {"status": "no_runs"}
    return {"status": job.status, "conclusion": job.conclusion or None, "job_id": job.id, "message": job.message,
            "result": job.result, "created_at": job.created_at, "updated_at": job.updated_at}
