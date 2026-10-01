# Yoco card machine sales. Yoco's export (Business Portal → Sales/Transactions → Export, or the CSV Yoco emails)
# becomes a "Yoco" account per company: each sale is income at its full (gross) amount and each fee is a cost.
# The money Yoco later pays into the bank is then only a transfer, so it isn't counted as income twice.
import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BankAccount, BankTransaction, Client, TransactionCategory
from .imports import is_duplicate, new_txn

YOCO_RE = re.compile(r"\byoco\b", re.I)
SALES, FEES, PAYOUT = "Card Sales (Yoco)", "Card Machine Fees", "Yoco Payout"
CATEGORIES = [(SALES, "credit", 11, "yoco card sale"), (FEES, "debit", 13, "yoco fee"), (PAYOUT, "credit", 16, "")]

# Yoco has renamed its export columns over time; match on any of these (lowercased, spaces/underscores removed).
COLS = {
    "date": ("date", "transactiondate", "createddate", "datetime", "created", "salesdate", "time"),
    "amount": ("amount", "grossamount", "total", "saleamount", "totalamount", "transactionamount", "amountzar"),
    "fee": ("fee", "fees", "yocofee", "transactionfee", "feeamount", "processingfee", "commission"),
    "tip": ("tip", "tips", "gratuity"),
    "status": ("status", "transactionstatus", "state"),
    "kind": ("type", "transactiontype", "paymenttype", "kind"),
    "ref": ("receiptnumber", "receipt", "reference", "transactionid", "id", "invoicenumber", "salenumber"),
    "card": ("cardtype", "card", "paymentmethod", "method", "brand"),
}
FAILED = re.compile(r"declin|fail|cancel|void|abandon|pending", re.I)
REFUND = re.compile(r"refund|reversal|chargeback", re.I)
PAYOUT_ROW = re.compile(r"payout|settlement|deposit", re.I)


def _key(h):
    return re.sub(r"[^a-z]", "", (h or "").lower())


def _money(v):
    s = re.sub(r"[^\d.,\-]", "", str(v or "")).replace(",", "")
    try:
        return Decimal(s) if s not in ("", "-", ".") else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def _date(v):
    s = str(v or "").strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    head = s.split(" ")[0] if re.match(r"\d", s) else s
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        for v in (head, s):
            try:
                return datetime.strptime(v, fmt).date()
            except ValueError:
                continue
    return None


def parse_csv(data: bytes):
    """Rows of {date, kind: sale|refund, gross, fee, ref, card}. Failed/declined attempts and payout lines are skipped."""
    text = data.decode("utf-8-sig", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    head_i = next((i for i, r in enumerate(rows[:15]) if {"date", "amount"} <= {f for f, names in COLS.items()
                                                                                 if any(_key(c) in names for c in r)}), None)
    if head_i is None:
        raise ValueError("This doesn't look like a Yoco export (no Date and Amount columns).")
    header = [_key(c) for c in rows[head_i]]
    col = {f: next((i for i, h in enumerate(header) if h in names), None) for f, names in COLS.items()}
    out = []
    for r in rows[head_i + 1:]:
        get = lambda f: r[col[f]] if col[f] is not None and col[f] < len(r) else ""
        day, gross = _date(get("date")), _money(get("amount"))
        if not day or not gross:
            continue
        label = f"{get('status')} {get('kind')}"
        if FAILED.search(label) or (PAYOUT_ROW.search(get("kind")) and not REFUND.search(label)):
            continue
        refund = bool(REFUND.search(label)) or gross < 0
        out.append({"date": day, "kind": "refund" if refund else "sale", "gross": abs(gross) + abs(_money(get("tip"))),
                    "fee": abs(_money(get("fee"))), "ref": get("ref").strip()[:60], "card": get("card").strip()[:30]})
    return out


def categories(db: Session, practice_id: int):
    have = {c.name: c for c in db.scalars(select(TransactionCategory).where(TransactionCategory.practice_id == practice_id))}
    for name, kind, color, keywords in CATEGORIES:
        if name not in have:
            have[name] = TransactionCategory(practice_id=practice_id, name=name, transaction_type=kind, color=color,
                                             keywords=keywords, tags="")
            db.add(have[name])
    db.flush()
    return have


def account(db: Session, client: Client):
    acc = db.scalar(select(BankAccount).where(BankAccount.client_id == client.id, BankAccount.bank_name == "Yoco"))
    if not acc:
        acc = BankAccount(client_id=client.id, account_name="Yoco card machine", bank_name="Yoco", account_type="card machine")
        db.add(acc)
        db.flush()
    return acc


def import_rows(db: Session, client: Client, rows, statement_id=None):
    cats = categories(db, client.practice_id)
    acc = account(db, client)
    saved = skipped = 0
    for r in rows:
        ref = f" {r['ref']}" if r["ref"] else ""
        card = f" ({r['card']})" if r["card"] else ""
        lines = [(f"Yoco card {'refund' if r['kind'] == 'refund' else 'sale'}{ref}{card}", r["gross"],
                  "debit" if r["kind"] == "refund" else "credit", cats[SALES])]
        if r["fee"]:
            lines.append((f"Yoco fee{ref}", r["fee"], "debit", cats[FEES]))
        for desc, value, kind, cat in lines:
            if is_duplicate(db, client.id, r["date"], desc, value):
                skipped += 1
                continue
            db.add(new_txn(client.id, r["date"], desc, value, kind, bank_account_id=acc.id, statement_id=statement_id,
                           category_id=cat.id, reference_number=r["ref"][:100], tags="yoco"))
            db.flush()
            saved += 1
    # From now on Yoco's payouts into this company's bank accounts are transfers, not new income.
    for t in db.scalars(select(BankTransaction).where(BankTransaction.client_id == client.id,
                                                      BankTransaction.transaction_type == "credit",
                                                      BankTransaction.bank_account_id != acc.id)):
        if YOCO_RE.search(t.description or "") and t.category_id != cats[PAYOUT].id:
            t.category_id = cats[PAYOUT].id
    db.commit()
    return saved, skipped


def clients_with_yoco(db: Session, client_ids):
    return set(db.scalars(select(BankAccount.client_id).where(BankAccount.client_id.in_(list(client_ids)),
                                                              BankAccount.bank_name == "Yoco")))


def is_payout(t, yoco_clients) -> bool:
    """A Yoco payout landing in the bank, for a company whose Yoco sales are already in the books."""
    if t.client_id not in yoco_clients or t.transaction_type != "credit":
        return False
    if t.bank_account and t.bank_account.bank_name == "Yoco":
        return False
    return bool(YOCO_RE.search(t.description or "")) or (t.category is not None and t.category.name == PAYOUT)
