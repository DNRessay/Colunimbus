import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import Ctx, client_ctx, current_user, get_db, get_owned
from ..jobs import start_job
from ..models import BankTransaction, ERPNextConfig, Job, User
from ..schemas import BankAccountOut, CategoryOut
from ..services import erpnext
from ..services.erpnext import ERPNextClient

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/erpnext", tags=["erpnext"])


def config_for(db, practice_id):
    cfg = erpnext.active_config(db, practice_id)
    if not cfg:
        raise HTTPException(400, "No ERPNext connection yet. Add one under Settings.")
    return cfg


def api_for(db, ctx: Ctx):
    return ERPNextClient(config_for(db, ctx.client.practice_id), ctx.client)


@router.post("/configs/{cfg_id}/test")
def test_config(cfg_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ok, msg = ERPNextClient(get_owned(db, ERPNextConfig, cfg_id, user)).test_connection()
    return {"success": ok, "message": msg}


@router.get("/companies")
def companies(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ERPNext companies, for linking each company in the app to one in ERPNext."""
    return {"companies": ERPNextClient(config_for(db, user.practice_id)).get_companies()}


@router.get("/accounts")
def accounts(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    raw = api_for(db, ctx).get_chart_of_accounts()
    if not raw:
        raise HTTPException(502, "No accounts returned from ERPNext. Check the company link.")
    out = sorted(
        ({"name": a["name"], "account_name": a.get("account_name") or a["name"],
          "account_type": a.get("account_type") or "", "root_type": a.get("root_type") or "",
          "is_group": bool(a.get("is_group"))} for a in raw),
        key=lambda a: (a["root_type"], a["name"]),
    )
    return {"accounts": out, "count": len(out)}


@router.get("/cost-centers")
def cost_centers(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    return {"cost_centers": api_for(db, ctx).get_cost_centers()}


@router.post("/transactions/{txn_id}/sync")
def sync_transaction(txn_id: int, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    txn = get_owned(db, BankTransaction, txn_id, ctx)
    if not txn.category_id:
        raise HTTPException(400, "Transaction must be categorized first")
    try:
        name = api_for(db, ctx).create_journal_entry(db, txn)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"message": f"Synced: {name}", "journal_entry": name}


@router.get("/sync-preflight")
def preflight(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    config_for(db, ctx.client.practice_id)
    p = erpnext.preflight(db, ctx.client)
    return {
        "company": ctx.client.erpnext_company,
        "company_set": p["company_set"],
        "default_bank_account": ctx.client.erpnext_bank_account,
        "cost_center": ctx.client.erpnext_cost_center,
        "needs_default_bank": p["needs_default_bank"],
        "missing_categories": [CategoryOut.model_validate(c).model_copy(update={"erpnext_account": p["accounts"].get(c.id, "")})
                               for c in p["missing_categories"]],
        "missing_bank_accounts": [BankAccountOut.model_validate(b) for b in p["missing_bank_accounts"]],
        "ready_count": p["ready_count"],
        "pending_count": p["pending_count"],
    }


@router.post("/sync-preflight", status_code=202)
def submit_preflight(data: dict = Body(default_factory=dict), ctx: Ctx = Depends(client_ctx),
                     db: Session = Depends(get_db)):
    """Saves the account mappings from the preflight form, then starts the sync job."""
    config_for(db, ctx.client.practice_id)
    if not ctx.client.erpnext_company:
        raise HTTPException(400, "Link this company to an ERPNext company first.")
    updated = erpnext.apply_preflight(db, ctx.client, data)
    job = start_job(db, ctx.user, ctx.client, "erpnext_sync")
    return {"message": f"Saved {updated} account mapping(s). Sync started.", "job_id": job.id}


@router.get("/sync-job-status")
def sync_job_status(ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    job = db.scalar(select(Job).where(Job.client_id == ctx.client_id, Job.kind == "erpnext_sync")
                    .order_by(Job.id.desc()).limit(1))
    if not job:
        return {"status": "no_runs"}
    return {"status": job.status, "conclusion": job.conclusion or None, "job_id": job.id, "message": job.message,
            "result": job.result, "created_at": job.created_at, "updated_at": job.updated_at}
