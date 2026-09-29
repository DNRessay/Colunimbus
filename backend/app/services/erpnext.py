import json
import logging

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BankTransaction, ERPNextConfig, ERPNextSyncLog, utcnow
from .categorize import junk_ids

log = logging.getLogger(__name__)


def qualified(account):
    """ERPNext account names are 'Account Name - ABBR'."""
    return " - " in (account or "")


class ERPNextClient:
    def __init__(self, config: ERPNextConfig):
        self.config = config
        self.base = config.base_url.rstrip("/")
        self._company = None

    @property
    def headers(self):
        return {
            "Authorization": f"token {self.config.api_key}:{self.config.api_secret}",
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
            return self._get("Company", ["name", "company_name", "abbr", "default_currency"], limit=200, timeout=15)
        except Exception as e:
            log.error("Failed to fetch companies: %s", e)
            return []

    def resolve_company(self, value=None):
        """Accepts a full company name, its abbreviation, or a partial name."""
        if self._company and value is None:
            return self._company
        stored = (value if value is not None else self.config.default_company or "").strip()
        if not stored:
            raise ValueError("No company configured. Set the default company in your ERPNext config.")
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

    def get_chart_of_accounts(self):
        accounts, start = [], 0
        while True:
            try:
                batch = self._get("Account", ["name", "account_name", "account_type", "root_type", "is_group", "company"], start=start)
            except Exception as e:
                log.error("Failed to fetch accounts (offset=%s): %s", start, e)
                break
            accounts.extend(batch)
            if len(batch) < 500:
                break
            start += 500

        company = (self.config.default_company or "").strip()
        if company and accounts:
            try:
                company = self.resolve_company()
            except ValueError:
                pass
            filtered = [a for a in accounts if a.get("company") == company]
            accounts = filtered or accounts
        return accounts

    def get_cost_centers(self):
        try:
            return self._get("Cost Center", ["name", "cost_center_name", "company"])
        except Exception as e:
            log.error("Failed to fetch cost centers: %s", e)
            return []

    def resolve_account(self, term):
        if not term or qualified(term):
            return term
        try:
            data = self._get("Account", filters=[["name", "like", f"%{term}%"]], limit=1, timeout=10)
            if data:
                return data[0]["name"]
        except Exception as e:
            log.error("Account resolution failed for %r: %s", term, e)
        return term

    def fetch_journal_entries(self, from_date, to_date):
        return self._get(
            "Journal Entry",
            ["name", "posting_date", "total_debit", "total_credit", "remark", "cheque_no", "user_remark", "docstatus"],
            [["posting_date", ">=", from_date], ["posting_date", "<=", to_date]],
        )

    def fetch_invoices(self, doctype, from_date, to_date):
        if doctype == "Sales Invoice":
            party = ["customer", "customer_name"]
        else:
            party = ["supplier", "supplier_name", "bill_no", "bill_date"]
        fields = ["name", *party, "posting_date", "due_date", "grand_total", "outstanding_amount", "status", "currency", "docstatus"]
        return self._get(doctype, fields, [["posting_date", ">=", from_date], ["posting_date", "<=", to_date], ["docstatus", "!=", "2"]])

    # ── Journal entries ─────────────────────────────────────────────────────

    def bank_account_for(self, txn: BankTransaction):
        if txn.bank_account and (txn.bank_account.erpnext_account or "").strip():
            return txn.bank_account.erpnext_account.strip()
        return (self.config.bank_account or "").strip()

    def create_journal_entry(self, db: Session, txn: BankTransaction):
        try:
            if not txn.category:
                raise ValueError("Transaction must be categorized before syncing")
            expense = (txn.category.erpnext_account or "").strip()
            if not expense:
                raise ValueError(f"Category '{txn.category.name}' has no ERPNext account configured")
            amount = float(txn.value)
            if not amount:
                raise ValueError(f"Transaction {txn.id} has zero amount")
            bank = self.bank_account_for(txn)
            if not bank:
                raise ValueError(
                    f"No ERPNext bank account configured for transaction {txn.id}. "
                    "Set it on the bank account or in Sync Preflight."
                )

            bank, expense = self.resolve_account(bank), self.resolve_account(expense)
            cost_center = self.config.default_cost_center or None
            if txn.direction == "debit":
                rows = [self._row(bank, 0, amount), self._row(expense, amount, 0, cost_center)]
            else:
                rows = [self._row(bank, amount, 0), self._row(expense, 0, amount, cost_center)]

            posting = txn.date.isoformat()
            doc = {
                "doctype": "Journal Entry",
                "voucher_type": "Journal Entry",
                "company": self.resolve_company(),
                "posting_date": posting,
                "accounts": rows,
                "user_remark": txn.description or "",
            }
            if txn.reference_number:
                doc["cheque_no"] = txn.reference_number
                doc["cheque_date"] = posting

            r = requests.post(f"{self.base}/api/resource/Journal Entry", headers=self.headers, json=doc, timeout=30)
            if r.status_code >= 400:
                try:
                    body = r.json().get("exception") or r.text[:500]
                except ValueError:
                    body = r.text[:500]
                raise RuntimeError(f"HTTP {r.status_code}: {body}")
            name = r.json().get("data", {}).get("name", "")
        except Exception as e:
            txn.erpnext_error = str(e)
            db.add(ERPNextSyncLog(config_id=self.config.id, record_type="bank_transaction", record_id=txn.id,
                                  status="failed", error_message=str(e)))
            db.commit()
            raise

        txn.erpnext_synced = True
        txn.erpnext_journal_entry = name
        txn.erpnext_sync_date = utcnow()
        txn.erpnext_error = ""
        db.add(ERPNextSyncLog(config_id=self.config.id, record_type="bank_transaction", record_id=txn.id,
                              erpnext_doctype="Journal Entry", erpnext_doc_name=name, status="success"))
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


# ── Queries shared by the preflight + bulk sync ─────────────────────────────

def syncable(db: Session, user_id: int):
    junk = junk_ids(db)
    q = select(BankTransaction).where(
        BankTransaction.user_id == user_id,
        BankTransaction.category_id.is_not(None),
        BankTransaction.erpnext_synced.is_(False),
    ).order_by(BankTransaction.date, BankTransaction.id)
    if junk:
        q = q.where(BankTransaction.category_id.not_in(junk))
    return list(db.scalars(q).unique())


def categories_missing_account(db: Session, user_id: int):
    seen = {}
    for t in syncable(db, user_id):
        if not qualified(t.category.erpnext_account):
            seen[t.category.id] = t.category
    return sorted(seen.values(), key=lambda c: c.name)


def bank_accounts_missing_account(db: Session, user_id: int):
    seen = {}
    for t in syncable(db, user_id):
        if t.bank_account and not qualified(t.bank_account.erpnext_account):
            seen[t.bank_account.id] = t.bank_account
    return sorted(seen.values(), key=lambda b: b.account_name)


def ready_count(db: Session, user_id: int):
    return sum(
        1 for t in syncable(db, user_id)
        if qualified(t.category.erpnext_account) and (t.bank_account is None or qualified(t.bank_account.erpnext_account))
    )


def sync_all_ready(db: Session, config: ERPNextConfig):
    """Quick inline sync: everything categorized whose category has an ERPNext account."""
    client = ERPNextClient(config)
    txns = syncable(db, config.user_id)
    ready = [t for t in txns if t.category.erpnext_account]
    ok = failed = 0
    for t in ready:
        try:
            client.create_journal_entry(db, t)
            ok += 1
        except Exception as e:
            failed += 1
            log.error("Sync failed for %s: %s", t.id, e)
    return ok, failed, len(txns)


def full_sync(db: Session, config: ERPNextConfig, dry_run=False, limit=0):
    """Worker-side sync: auto-resolves short account names first, then syncs. Returns a summary dict."""
    client = ERPNextClient(config)
    ok, msg = client.test_connection()
    if not ok:
        raise RuntimeError(f"ERPNext connection failed: {msg}")
    client.resolve_company()
    notes = []

    txns = [t for t in syncable(db, config.user_id) if (t.category.erpnext_account or "").strip()]
    if limit:
        txns = txns[:limit]

    if any(t.bank_account is None for t in txns):
        fallback = (config.bank_account or "").strip()
        if not fallback:
            raise RuntimeError("Some transactions have no bank account and the ERPNext config has no default bank account.")
        if not qualified(fallback):
            resolved = client.resolve_account(fallback)
            if not qualified(resolved):
                raise RuntimeError(f"Could not resolve config bank account '{fallback}' in ERPNext.")
            notes.append(f"Config bank account '{fallback}' -> '{resolved}'")
            config.bank_account = resolved

    bad_banks, bad_cats = set(), set()
    for ba in {t.bank_account.id: t.bank_account for t in txns if t.bank_account}.values():
        if qualified(ba.erpnext_account):
            continue
        resolved = client.resolve_account((ba.erpnext_account or "").strip())
        if qualified(resolved):
            notes.append(f"Bank account '{ba.account_name}' -> '{resolved}'")
            ba.erpnext_account = resolved
        else:
            bad_banks.add(ba.id)
            notes.append(f"Skipped bank account '{ba.account_name}': no valid ERPNext account")

    for cat in {t.category.id: t.category for t in txns}.values():
        if qualified(cat.erpnext_account):
            continue
        resolved = client.resolve_account(cat.erpnext_account.strip())
        if qualified(resolved):
            notes.append(f"Category '{cat.name}' -> '{resolved}'")
            cat.erpnext_account = resolved
        else:
            bad_cats.add(cat.id)
            notes.append(f"Skipped category '{cat.name}': '{cat.erpnext_account}' not resolvable")

    if dry_run:
        db.rollback()
    else:
        db.commit()

    txns = [t for t in txns if t.category_id not in bad_cats and t.bank_account_id not in bad_banks]
    synced = failed = skipped = 0
    for t in txns:
        if not t.value:
            skipped += 1
            continue
        if dry_run:
            synced += 1
            continue
        try:
            client.create_journal_entry(db, t)
            synced += 1
        except Exception as e:
            failed += 1
            notes.append(f"#{t.id}: {e}")
    return {"synced": synced, "failed": failed, "skipped": skipped, "total": len(txns), "notes": notes[:50]}


def missing_payload(db: Session, user_id: int):
    return categories_missing_account(db, user_id), bank_accounts_missing_account(db, user_id)


def apply_preflight(db: Session, config: ERPNextConfig, data: dict):
    """Saves config defaults + ERPNext accounts typed in on the preflight form."""
    for field, key in (("default_company", "config_company"), ("bank_account", "config_bank_account"),
                       ("default_cost_center", "config_cost_center")):
        val = str(data.get(key, "") or "").strip()
        if val:
            setattr(config, field, val)
    cats, banks = missing_payload(db, config.user_id)
    updated_banks = updated_cats = 0
    for ba in banks:
        val = str(data.get(f"bank_erpnext_{ba.id}", "") or "").strip()
        if val:
            ba.erpnext_account = val
            updated_banks += 1
    for cat in cats:
        val = str(data.get(f"account_{cat.id}", "") or "").strip()
        if val:
            cat.erpnext_account = val
            updated_cats += 1
    db.commit()
    return updated_banks, updated_cats

