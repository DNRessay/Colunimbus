import json
import logging
from typing import Optional

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BankAccount, BankTransaction, CategoryAccount, Client, ERPNextConfig, ERPNextSyncLog, utcnow
from ..security import unseal

log = logging.getLogger(__name__)


def qualified(account):
    """ERPNext account names look like 'Account Name - ABBR'."""
    return " - " in (account or "")


def active_config(db: Session, practice_id: int) -> Optional[ERPNextConfig]:
    return db.scalar(select(ERPNextConfig).where(ERPNextConfig.practice_id == practice_id,
                                                 ERPNextConfig.is_active.is_(True)))


def account_map(db: Session, client_id: int) -> dict:
    return {ca.category_id: ca.erpnext_account for ca in
            db.scalars(select(CategoryAccount).where(CategoryAccount.client_id == client_id))}


def set_category_account(db: Session, client_id: int, category_id: int, account: str):
    row = db.scalar(select(CategoryAccount).where(CategoryAccount.client_id == client_id,
                                                  CategoryAccount.category_id == category_id))
    account = (account or "").strip()
    if not account:
        if row:
            db.delete(row)
        return
    if not row:
        row = CategoryAccount(client_id=client_id, category_id=category_id)
        db.add(row)
    row.erpnext_account = account


class ERPNextClient:
    def __init__(self, config: ERPNextConfig, client: Optional[Client] = None):
        self.config = config
        self.client = client
        self.base = config.base_url.rstrip("/")
        self._company = None

    @property
    def headers(self):
        return {
            "Authorization": f"token {unseal(self.config.api_key)}:{unseal(self.config.api_secret)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get(self, doctype, fields=None, filters=None, limit=500, start=0, timeout=30):
        params = {"limit_page_length": limit, "limit_start": start}
        if fields:
            params["fields"] = json.dumps(fields)
        if filters:
            params["filters"] = json.dumps(filters)
        r = requests.get(f"{self.base}/api/resource/{doctype}", headers=self.headers, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json().get("data", [])

    def test_connection(self):
        try:
            r = requests.get(f"{self.base}/api/method/frappe.auth.get_logged_user", headers=self.headers, timeout=10)
            r.raise_for_status()
            return True, f"Connected as: {r.json().get('message', 'Unknown')}"
        except requests.ConnectionError:
            return False, "Cannot connect to ERPNext. Check URL."
        except requests.Timeout:
            return False, "Connection timeout."
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                return False, "Authentication failed. Check API credentials."
            return False, f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        except Exception as e:
            return False, str(e)

    def get_companies(self):
        try:
            return self._get("Company", ["name", "company_name", "abbr", "default_currency"], limit=500, timeout=15)
        except Exception as e:
            log.error("Failed to fetch companies: %s", e)
            return []

    def resolve_company(self, value=None):
        """Accepts a full company name, its abbreviation, or a partial name."""
        if value is None and self._company:
            return self._company
        stored = (value if value is not None else (self.client.erpnext_company if self.client else "")).strip()
        if not stored:
            raise ValueError("This client has no ERPNext company set. Set it on the Clients page.")
        companies = self.get_companies()
        resolved = (
            next((c["name"] for c in companies if c.get("name") == stored), None)
            or next((c["name"] for c in companies if (c.get("abbr") or "").strip().upper() == stored.upper()), None)
            or next((c["name"] for c in companies if stored.lower() in (c.get("name") or "").lower()), None)
            or stored
        )
        if value is None:
            self._company = resolved
        return resolved

    def _company_filter(self):
        if self.client and self.client.erpnext_company:
            return [["company", "=", self.resolve_company()]]
        return []

    def get_chart_of_accounts(self):
        accounts, start = [], 0
        fields = ["name", "account_name", "account_type", "root_type", "is_group", "company"]
        while True:
            try:
                batch = self._get("Account", fields, self._company_filter() or None, start=start)
            except Exception as e:
                log.error("Failed to fetch accounts (offset=%s): %s", start, e)
                break
            accounts.extend(batch)
            if len(batch) < 500:
                break
            start += 500
        return accounts

    def get_cost_centers(self):
        try:
            return self._get("Cost Center", ["name", "cost_center_name", "company"], self._company_filter() or None)
        except Exception as e:
            log.error("Failed to fetch cost centers: %s", e)
            return []

    def resolve_account(self, term):
        if not term or qualified(term):
            return term
        filters = [["name", "like", f"%{term}%"], *self._company_filter()]
        try:
            data = self._get("Account", filters=filters, limit=1, timeout=10)
            if data:
                return data[0]["name"]
        except Exception as e:
            log.error("Account resolution failed for %r: %s", term, e)
        return term

    def fetch_journal_entries(self, from_date, to_date):
        return self._get(
            "Journal Entry",
            ["name", "posting_date", "total_debit", "total_credit", "remark", "cheque_no", "user_remark", "docstatus"],
            [["posting_date", ">=", from_date], ["posting_date", "<=", to_date], ["docstatus", "!=", 2],
             *self._company_filter()],
        )

    def fetch_invoices(self, doctype, from_date, to_date):
        party = ["customer", "customer_name"] if doctype == "Sales Invoice" else ["supplier", "supplier_name", "bill_no", "bill_date"]
        fields = ["name", *party, "posting_date", "due_date", "grand_total", "outstanding_amount", "status", "currency", "docstatus"]
        return self._get(doctype, fields, [["posting_date", ">=", from_date], ["posting_date", "<=", to_date],
                                           ["docstatus", "!=", 2], *self._company_filter()])

    # ── Journal entries ─────────────────────────────────────────────────────

    def bank_account_for(self, txn: BankTransaction):
        if txn.bank_account and (txn.bank_account.erpnext_account or "").strip():
            return txn.bank_account.erpnext_account.strip()
        return (self.client.erpnext_bank_account or "").strip()

    def create_journal_entry(self, db: Session, txn: BankTransaction, accounts: Optional[dict] = None):
        accounts = accounts if accounts is not None else account_map(db, txn.client_id)
        try:
            if not txn.category:
                raise ValueError("Transaction must be categorized before syncing")
            expense = (accounts.get(txn.category_id) or "").strip()
            if not expense:
                raise ValueError(f"Category '{txn.category.name}' has no ERPNext account for this client")
            amount = float(txn.value)
            if not amount:
                raise ValueError(f"Transaction {txn.id} has zero amount")
            bank = self.bank_account_for(txn)
            if not bank:
                raise ValueError("No ERPNext bank account set on the bank account or the client.")

            bank, expense = self.resolve_account(bank), self.resolve_account(expense)
            cost_center = self.client.erpnext_cost_center or None
            if txn.direction == "debit":
                rows = [self._row(bank, 0, amount), self._row(expense, amount, 0, cost_center)]
            else:
                rows = [self._row(bank, amount, 0), self._row(expense, 0, amount, cost_center)]

            posting = txn.date.isoformat()
            doc = {
                "doctype": "Journal Entry",
                "voucher_type": "Bank Entry",
                "company": self.resolve_company(),
                "posting_date": posting,
                "accounts": rows,
                "user_remark": txn.description or "",
                # cheque_no carries our reference so reconciliation can match the JE back to the line
                "cheque_no": txn.reference_number or f"COL-{txn.id}",
                "cheque_date": posting,
            }
            r = requests.post(f"{self.base}/api/resource/Journal Entry", headers=self.headers, json=doc, timeout=30)
            if r.status_code >= 400:
                try:
                    body = r.json().get("exception") or r.text[:500]
                except ValueError:
                    body = r.text[:500]
                raise RuntimeError(f"HTTP {r.status_code}: {body}")
            name = r.json().get("data", {}).get("name", "")
        except Exception as e:
            txn.erpnext_error = str(e)[:2000]
            db.add(ERPNextSyncLog(client_id=txn.client_id, config_id=self.config.id, record_type="bank_transaction",
                                  record_id=txn.id, status="failed", error_message=str(e)[:2000]))
            db.commit()
            raise

        txn.erpnext_synced = True
        txn.erpnext_journal_entry = name
        txn.erpnext_sync_date = utcnow()
        txn.erpnext_error = ""
        db.add(ERPNextSyncLog(client_id=txn.client_id, config_id=self.config.id, record_type="bank_transaction",
                              record_id=txn.id, erpnext_doctype="Journal Entry", erpnext_doc_name=name, status="success"))
        self.config.last_sync = utcnow()
        db.commit()
        return name

    @staticmethod
    def _row(account, debit, credit, cost_center=None):
        row = {"doctype": "Journal Entry Account", "account": account,
               "debit_in_account_currency": debit, "credit_in_account_currency": credit}
        if cost_center:
            row["cost_center"] = cost_center
        return row


# ── Preflight + bulk sync (per client) ──────────────────────────────────────

def syncable(db: Session, client_id: int):
    return list(db.scalars(select(BankTransaction).where(
        BankTransaction.client_id == client_id,
        BankTransaction.category_id.is_not(None),
        BankTransaction.erpnext_synced.is_(False),
    ).order_by(BankTransaction.date, BankTransaction.id)).unique())


def preflight(db: Session, client: Client):
    txns = syncable(db, client.id)
    accounts = account_map(db, client.id)
    cats, banks = {}, {}
    for t in txns:
        if not qualified(accounts.get(t.category_id)):
            cats[t.category_id] = t.category
        if t.bank_account and not qualified(t.bank_account.erpnext_account):
            banks[t.bank_account.id] = t.bank_account
    fallback_ok = qualified(client.erpnext_bank_account)
    ready = [t for t in txns if qualified(accounts.get(t.category_id))
             and (qualified(t.bank_account.erpnext_account) if t.bank_account else fallback_ok)]
    return {
        "company_set": bool(client.erpnext_company),
        "missing_categories": sorted(cats.values(), key=lambda c: c.name),
        "missing_bank_accounts": sorted(banks.values(), key=lambda b: b.account_name),
        "needs_default_bank": any(t.bank_account is None for t in txns) and not fallback_ok,
        "ready_count": len(ready),
        "pending_count": len(txns),
        "accounts": accounts,
    }


def apply_preflight(db: Session, client: Client, data: dict):
    """Saves the account mappings typed into the preflight form."""
    if val := str(data.get("erpnext_bank_account", "") or "").strip():
        client.erpnext_bank_account = val
    if val := str(data.get("erpnext_cost_center", "") or "").strip():
        client.erpnext_cost_center = val
    updated = 0
    for key, val in data.items():
        val = str(val or "").strip()
        if not val:
            continue
        if key.startswith("account_") and key[8:].isdigit():
            set_category_account(db, client.id, int(key[8:]), val)
            updated += 1
        elif key.startswith("bank_") and key[5:].isdigit():
            ba = db.get(BankAccount, int(key[5:]))
            if ba and ba.client_id == client.id:
                ba.erpnext_account = val
                updated += 1
    db.commit()
    return updated


def sync_client(db: Session, config: ERPNextConfig, client: Client, dry_run=False, limit=0):
    """Resolves short account names, then posts every ready transaction. Returns a summary dict."""
    api = ERPNextClient(config, client)
    ok, msg = api.test_connection()
    if not ok:
        raise RuntimeError(f"ERPNext connection failed: {msg}")
    api.resolve_company()
    notes = []
    accounts = account_map(db, client.id)

    txns = [t for t in syncable(db, client.id) if accounts.get(t.category_id)]
    if limit:
        txns = txns[:limit]

    if any(t.bank_account is None for t in txns) and not qualified(client.erpnext_bank_account):
        resolved = api.resolve_account(client.erpnext_bank_account)
        if not qualified(resolved):
            raise RuntimeError("Some transactions have no bank account and the client has no default ERPNext bank account.")
        notes.append(f"Default bank account '{client.erpnext_bank_account}' -> '{resolved}'")
        client.erpnext_bank_account = resolved

    bad_banks, bad_cats = set(), set()
    for ba in {t.bank_account.id: t.bank_account for t in txns if t.bank_account}.values():
        if qualified(ba.erpnext_account):
            continue
        resolved = api.resolve_account(ba.erpnext_account)
        if qualified(resolved):
            notes.append(f"Bank account '{ba.account_name}' -> '{resolved}'")
            ba.erpnext_account = resolved
        else:
            bad_banks.add(ba.id)
            notes.append(f"Skipped bank account '{ba.account_name}': no valid ERPNext account")

    for cat_id, acct in list(accounts.items()):
        if qualified(acct) or cat_id not in {t.category_id for t in txns}:
            continue
        resolved = api.resolve_account(acct)
        if qualified(resolved):
            notes.append(f"Category account '{acct}' -> '{resolved}'")
            set_category_account(db, client.id, cat_id, resolved)
            accounts[cat_id] = resolved
        else:
            bad_cats.add(cat_id)
            notes.append(f"Skipped category account '{acct}': not found in ERPNext")

    if dry_run:
        db.rollback()
    else:
        db.commit()

    txns = [t for t in txns if t.category_id not in bad_cats and t.bank_account_id not in bad_banks]
    synced = failed = skipped = 0
    for t in txns:
        if not t.value:
            skipped += 1
        elif dry_run:
            synced += 1
        else:
            try:
                api.create_journal_entry(db, t, accounts)
                synced += 1
            except Exception as e:
                failed += 1
                notes.append(f"#{t.id}: {e}")
    return {"synced": synced, "failed": failed, "skipped": skipped, "total": len(txns), "notes": notes[:50]}
