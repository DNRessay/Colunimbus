import csv
import io
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

log = logging.getLogger(__name__)

DATE_FORMATS = ("%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y")
TEMPLATE = (
    "Transaction Date,Posting Date,Description,Debits,Credits,Balance,Bank account\n"
    "2025/09/23,2025/09/23,Sample Transaction,,1000.00,5000.00,Capitec Savings\n"
    "2025/09/24,2025/09/24,Sample Payment,500.00,,4500.00,Capitec Savings\n"
)


def _date(s):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime((s or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _amount(s):
    s = (s or "").replace("R", "").replace(",", "").replace(" ", "").strip()
    if not s or s == "-":
        return None
    try:
        return abs(Decimal(s))
    except InvalidOperation:
        return None


def parse_csv(data):
    """Capitec-style export: Transaction Date, Posting Date, Description, Debits, Credits, Balance, Bank account."""
    text = data.decode("utf-8-sig", errors="ignore") if isinstance(data, bytes) else data
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        day = _date(row.get("Transaction Date"))
        desc = row.get("Description", "")
        if not day or len(desc) < 2 or desc in ("Description", "Transaction Date"):
            continue
        balance = row.get("Balance", "").replace("R", "").replace(",", "").replace(" ", "")
        try:
            balance = Decimal(balance) if balance and balance != "-" else None
        except InvalidOperation:
            balance = None
        rows.append({
            "transaction_date": day,
            "posting_date": _date(row.get("Posting Date")),
            "description": desc,
            "debits": _amount(row.get("Debits")),
            "credits": _amount(row.get("Credits")),
            "balance": balance,
            "bank_account": row.get("Bank account", ""),
            "reference": f"{desc.split()[0][:10]}-{day:%Y%m%d}",
        })
    log.info("CSV: %d rows", len(rows))
    return rows
