from datetime import date

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..deps import current_user, get_db, get_owned
from ..models import ERPNextInvoice, User
from ..schemas import ERPInvoiceOut
from ..services.invoice_sync import sync_period

router = APIRouter(prefix="/api/erp-invoices", tags=["erpnext invoices"])


@router.get("", response_model=list[ERPInvoiceOut])
def list_invoices(invoice_type: str = "", status: str = "", q: str = "", user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    stmt = select(ERPNextInvoice).where(ERPNextInvoice.user_id == user.id)
    if invoice_type:
        stmt = stmt.where(ERPNextInvoice.invoice_type == invoice_type)
    if status:
        stmt = stmt.where(ERPNextInvoice.erp_status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(ERPNextInvoice.party_name.ilike(like), ERPNextInvoice.erp_name.ilike(like),
                              ERPNextInvoice.bill_no.ilike(like)))
    return db.scalars(stmt.order_by(ERPNextInvoice.posting_date.desc())).all()


@router.get("/counts")
def counts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    def n(*where):
        return db.scalar(select(func.count()).select_from(ERPNextInvoice).where(ERPNextInvoice.user_id == user.id, *where))

    return {
        "all": n(), "sales": n(ERPNextInvoice.invoice_type == "sales"),
        "purchase": n(ERPNextInvoice.invoice_type == "purchase"),
        "unpaid": n(ERPNextInvoice.erp_status == "Unpaid"), "overdue": n(ERPNextInvoice.erp_status == "Overdue"),
    }


@router.get("/search")
def search(q: str = "", type: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(ERPNextInvoice).where(ERPNextInvoice.user_id == user.id,
                                        ERPNextInvoice.erp_status.in_(["Unpaid", "Partly Paid", "Overdue"]))
    if q:
        stmt = stmt.where(or_(ERPNextInvoice.party_name.ilike(f"%{q}%"), ERPNextInvoice.erp_name.ilike(f"%{q}%")))
    if type:
        stmt = stmt.where(ERPNextInvoice.invoice_type == type)
    return {"results": [ERPInvoiceOut.model_validate(i) for i in db.scalars(stmt.limit(20))]}


@router.post("/sync")
def sync(year: int = Body(None, embed=True), month: int = Body(None, embed=True), user: User = Depends(current_user),
         db: Session = Depends(get_db)):
    today = date.today()
    year, month = year or today.year, month or today.month
    if not (1 <= month <= 12):
        raise HTTPException(400, "Invalid month.")
    try:
        r = sync_period(db, user.id, year, month)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Sync failed: {e}")
    return {
        "message": (f"Sync complete — {r['sales_created']} sales created, {r['sales_updated']} updated; "
                    f"{r['purchase_created']} purchase created, {r['purchase_updated']} updated."),
        **r,
    }


@router.get("/{inv_id}", response_model=ERPInvoiceOut)
def get_invoice(inv_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, ERPNextInvoice, inv_id, user)
