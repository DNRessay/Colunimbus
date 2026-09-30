from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Client, ERPNextInvoice
from .erpnext import ERPNextClient, active_config


def _date(v):
    return date.fromisoformat(str(v)) if v else None


def sync_period(db: Session, client: Client, year: int, month: int):
    config = active_config(db, client.practice_id)
    if not config:
        raise ValueError("No active ERPNext connection.")
    if not client.erpnext_company:
        raise ValueError("Set this company's ERPNext company first.")
    api = ERPNextClient(config, client)
    start, end = f"{year}-{month:02d}-01", f"{year}-{month:02d}-{monthrange(year, month)[1]:02d}"

    result = {}
    for kind, doctype, party in (("sales", "Sales Invoice", "customer"), ("purchase", "Purchase Invoice", "supplier")):
        rows = api.fetch_invoices(doctype, start, end)
        created = updated = 0
        for d in rows:
            inv = db.scalar(select(ERPNextInvoice).where(ERPNextInvoice.client_id == client.id, ERPNextInvoice.erp_name == d["name"]))
            if inv:
                updated += 1
            else:
                inv = ERPNextInvoice(client_id=client.id, erp_name=d["name"])
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
