import datetime as dt
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Auth / practice ─────────────────────────────────────────────────────────

class UserOut(Out):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    role: str
    practice_id: int


class RegisterIn(BaseModel):
    practice_name: str = ""
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: str
    username: str = Field(min_length=1, max_length=150)
    password: str


class MemberIn(BaseModel):
    first_name: str = Field(min_length=1)
    last_name: str = ""
    email: str
    username: str = Field(min_length=1, max_length=150)
    password: str
    role: str = "bookkeeper"


class MeIn(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None


class LoginIn(BaseModel):
    username: str
    password: str


class RefreshIn(BaseModel):
    refresh: str


class ChangePasswordIn(BaseModel):
    old_password: str = ""
    new_password1: str = ""
    new_password2: str = ""


class ResetRequestIn(BaseModel):
    email: str = ""


class ResetConfirmIn(BaseModel):
    token: str
    new_password: str


class PracticeOut(Out):
    id: int
    name: str
    created_at: datetime


class PracticeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


# ── Clients ─────────────────────────────────────────────────────────────────

class ClientIn(BaseModel):
    name: Optional[str] = None
    registration_number: Optional[str] = None
    vat_number: Optional[str] = None
    vat_registered: Optional[bool] = None
    year_end_month: Optional[int] = Field(None, ge=1, le=12)
    contact_email: Optional[str] = None
    notes: Optional[str] = None
    erpnext_company: Optional[str] = None
    erpnext_bank_account: Optional[str] = None
    erpnext_cost_center: Optional[str] = None
    is_active: Optional[bool] = None


class ClientOut(Out):
    id: int
    name: str
    registration_number: str
    vat_number: str
    vat_registered: bool
    year_end_month: int
    contact_email: str
    notes: str
    erpnext_company: str
    erpnext_bank_account: str
    erpnext_cost_center: str
    is_active: bool
    created_at: datetime


# ── Bank data ───────────────────────────────────────────────────────────────

class BankAccountIn(BaseModel):
    account_name: Optional[str] = None
    account_number: Optional[str] = None
    bank_name: Optional[str] = None
    account_type: Optional[str] = None
    currency: Optional[str] = None
    erpnext_account: Optional[str] = None
    is_active: Optional[bool] = None


class BankAccountOut(Out):
    id: int
    account_name: str
    account_number: str
    bank_name: str
    account_type: str
    currency: str
    erpnext_account: str
    is_active: bool
    created_at: datetime


class CategoryIn(BaseModel):
    name: Optional[str] = None
    transaction_type: Optional[str] = None
    keywords: Optional[str] = None
    tags: Optional[str] = None
    active: Optional[bool] = None
    color: Optional[int] = None
    erpnext_account: Optional[str] = None  # saved for the selected client


class CategoryOut(Out):
    id: int
    name: str
    transaction_type: str
    keywords: str
    tags: str
    active: bool
    color: Optional[int]
    erpnext_account: str = ""  # for the selected client, filled in by the router
    created_at: datetime


class StatementIn(BaseModel):
    client_id: Optional[int] = None
    bank_account_id: Optional[int] = None


class StatementOut(Out):
    id: int
    client_id: Optional[int]
    bank_account_id: Optional[int]
    source: str
    gmail_id: str
    subject: str
    sender: str
    received_date: Optional[datetime]
    bank_name: str
    has_attachment: bool
    state: str
    processed_date: Optional[datetime]
    transaction_count: int
    error_message: str
    created_at: datetime


class TransactionIn(BaseModel):
    bank_account: Optional[int] = None
    date: Optional[dt.date] = None
    transaction_type: Optional[str] = None
    posting_date: Optional[dt.date] = None
    description: Optional[str] = None
    reference_number: Optional[str] = None
    deposit: Optional[Decimal] = None
    withdrawal: Optional[Decimal] = None
    balance: Optional[Decimal] = None
    category: Optional[int] = None
    notes: Optional[str] = None


class TransactionOut(Out):
    id: int
    bank_account: Optional[int] = Field(validation_alias="bank_account_id")
    statement: Optional[int] = Field(validation_alias="statement_id")
    date: dt.date
    posting_date: Optional[dt.date]
    transaction_type: str
    description: str
    reference_number: str
    amount: Optional[Decimal]
    deposit: Optional[Decimal]
    withdrawal: Optional[Decimal]
    fee: Optional[Decimal]
    balance: Optional[Decimal]
    currency: str
    category: Optional[int] = Field(validation_alias="category_id")
    category_name: Optional[str]
    tags: str
    notes: str
    recon_status: str
    erpnext_synced: bool
    erpnext_journal_entry: str
    erpnext_error: str
    created_at: datetime


class ERPNextConfigIn(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    is_active: Optional[bool] = None


class ERPNextConfigOut(Out):
    # api_key / api_secret are write-only
    id: int
    name: str
    base_url: str
    is_active: bool
    last_sync: Optional[datetime]
    created_at: datetime


class SyncLogOut(Out):
    id: int
    record_type: str
    record_id: int
    erpnext_doctype: str
    erpnext_doc_name: str
    status: str
    error_message: str
    sync_date: datetime


class PDFJobOut(Out):
    id: int
    filename: str
    bank_name: str
    status: str
    progress: int
    total_files: int
    processed_files: int
    transactions_found: int
    transactions_saved: int
    transactions_skipped: int
    error_message: str
    statement_id: Optional[int]
    created_at: datetime


class JobOut(Out):
    id: int
    kind: str
    status: str
    conclusion: str
    message: str
    result: dict
    created_at: datetime
    updated_at: datetime


# ── ERPNext invoices / reconciliation ───────────────────────────────────────

class ERPInvoiceOut(Out):
    id: int
    invoice_type: str
    erp_name: str
    erp_status: str
    party_id: str
    party_name: str
    currency: str
    grand_total: Decimal
    outstanding_amount: Decimal
    posting_date: dt.date
    due_date: Optional[dt.date]
    bill_no: str
    bill_date: Optional[dt.date]
    fetched_at: datetime
    is_paid: bool
    is_overdue: bool
    amount_paid: Decimal


class JournalEntryOut(Out):
    id: int
    je_name: str
    posting_date: dt.date
    amount: Decimal
    account: str
    reference_number: str
    remark: str


class MatchOut(Out):
    id: int
    transaction: int = Field(validation_alias="transaction_id")
    journal_entry: Optional[int] = Field(validation_alias="journal_entry_id")
    status: str
    flag_reason: str
    matched_at: datetime
    matched_by: str


class PeriodOut(Out):
    id: int
    year: int
    month: int
    status: str
    closed_at: Optional[datetime]
    total_transactions: int
    matched_count: int
    flagged_count: int
    unreconciled_count: int
    label: str
    can_close: bool
