"""Money moving between two companies in the same group shows up as a debit in one company's bank
and a credit in the other's. Those must be booked as intercompany loans, not income/expense."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BankTransaction, Client, TransactionCategory

CATEGORY = "Intercompany Transfer"
MAX_DAYS = 3


def intercompany_category(db: Session, practice_id: int):
    cat = db.scalar(select(TransactionCategory).where(TransactionCategory.practice_id == practice_id,
                                                      TransactionCategory.name == CATEGORY))
    if not cat:
        cat = TransactionCategory(practice_id=practice_id, name=CATEGORY, transaction_type="any", color=18)
        db.add(cat)
        db.flush()
    return cat


def find_pairs(db: Session, practice_id: int):
    """Debits in one company matched to same-amount credits in another within a few days."""
    cat = intercompany_category(db, practice_id)
    names = dict(db.execute(select(Client.id, Client.name).where(Client.practice_id == practice_id)).all())
    if len(names) < 2:
        return []
    txns = list(db.scalars(select(BankTransaction).where(
        BankTransaction.client_id.in_(names),
        BankTransaction.erpnext_synced.is_(False),
        (BankTransaction.category_id.is_(None)) | (BankTransaction.category_id != cat.id),
    ).order_by(BankTransaction.date)).unique())
    credits = [t for t in txns if t.direction == "credit"]
    used, pairs = set(), []
    for out in (t for t in txns if t.direction == "debit"):
        match = next((c for c in credits if c.id not in used and c.client_id != out.client_id
                      and c.value == out.value and abs((c.date - out.date).days) <= MAX_DAYS), None)
        if match:
            used.add(match.id)
            pairs.append({
                "amount": str(out.value),
                "out": {"id": out.id, "company": names[out.client_id], "date": out.date.isoformat(),
                        "description": out.description, "category": out.category_name},
                "in": {"id": match.id, "company": names[match.client_id], "date": match.date.isoformat(),
                       "description": match.description, "category": match.category_name},
            })
    db.commit()
    return pairs


def confirm_pair(db: Session, practice_id: int, out_id: int, in_id: int):
    cat = intercompany_category(db, practice_id)
    names = dict(db.execute(select(Client.id, Client.name).where(Client.practice_id == practice_id)).all())
    out, inc = db.get(BankTransaction, out_id), db.get(BankTransaction, in_id)
    if not out or not inc or out.client_id not in names or inc.client_id not in names or out.client_id == inc.client_id:
        raise ValueError("Those transactions are not an intercompany pair.")
    for t, other in ((out, inc), (inc, out)):
        if t.erpnext_synced:
            raise ValueError(f"Transaction {t.id} is already synced to ERPNext.")
        t.category = cat
        t.notes = f"Intercompany with {names[other.client_id]} (txn {other.id})"
    db.commit()
