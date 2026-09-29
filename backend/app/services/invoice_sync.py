from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ERPNextConfig, ERPNextInvoice
from .erpnext import ERPNextClient


def _date(v):
    return date.fromisoformat(str(v)) if v else None


def sync_period(db: Session, user_id: int, year: int, month: int):
    config = db.scalar(select(ERPNextConfig).where(ERPNextConfig.user_id == user_id, ERPNextConfig.is_active.is_(True)))
    if not config:
        raise ValueError("No active ERPNext config found.")
    client = ERPNextClient(config)
    start, end = f"{year}-{month:02d}-01", f"{year}-{month:02d}-{monthrange(year, month)[1]:02d}"

    result = {}
    for kind, doctype, party in (("sales", "Sales Invoice", "customer"), ("purchase", "Purchase Invoice", "supplier")):
        rows = client.fetch_invoices(doctype, start, end)
        created = updated = 0
        for d in rows:
            inv = db.scalar(select(ERPNextInvoice).where(ERPNextInvoice.user_id == user_id, ERPNextInvoice.erp_name == d["name"]))
            if inv:
                updated += 1
            else:
                inv = ERPNextInvoice(user_id=user_id, erp_name=d["name"])
                db.add(inv)
                created += 1
            inv.invoice_type = kind
            inv.erp_status = d.get("status") or "Unpaid"
            inv.party_id = d.get(party) or ""
            inv.party_name = d.get(f"{party}_name") or d.get(party) or ""
            inv.currency = d.get("currency") or "ZAR"
            inv.grand_total = Decimal(str(d.get("grand_total") or 0))
            inv.outstanding_amount = Decimal(str(d.get("outstanding_amount") or 0))
            inv.posting_date = _date(d["posting_date"])
            inv.due_date = _date(d.get("due_date"))
            inv.bill_no = d.get("bill_no") or ""
            inv.bill_date = _date(d.get("bill_date"))
            inv.raw_data = d
        result.update({f"{kind}_fetched": len(rows), f"{kind}_created": created, f"{kind}_updated": updated})
    db.commit()
    return result
