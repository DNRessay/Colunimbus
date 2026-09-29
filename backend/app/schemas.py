import datetime as dt
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Auth ────────────────────────────────────────────────────────────────────

class UserOut(Out):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str


PROFILE_FIELDS = [
    "phone", "date_of_birth", "id_number", "city", "province", "country", "occupation",
    "years_experience", "industry", "linkedin_url", "github_url", "portfolio_url",
]


class ProfileIn(BaseModel):
    phone: Optional[str] = None
    date_of_birth: Optional[date] = None
    id_number: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    country: Optional[str] = None
    occupation: Optional[str] = None
    years_experience: Optional[str] = None
    industry: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def blank_date(cls, v):
        return v or None


class ProfileOut(Out):
    phone: str
    date_of_birth: Optional[date]
    id_number: str
    city: str
    province: str
    country: str
    occupation: str
    years_experience: str
    industry: str
    linkedin_url: str
    github_url: str
    portfolio_url: str


class RegisterIn(ProfileIn):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: str
    username: str = Field(min_length=1, max_length=150)
    password: str


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


class SocialLinkIn(BaseModel):
    platform: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=500)

    @field_validator("url")
    @classmethod
    def add_scheme(cls, v):
        v = v.strip()
        return v if v.startswith(("http://", "https://")) else "https://" + v


class SocialLinkOut(Out):
    id: int
    platform: str
    url: str
    icon: str = Field(validation_alias="display_icon")


# ── Core resources ──────────────────────────────────────────────────────────

class BankAccountIn(BaseModel):
    account_name: Optional[str] = None
    account_number: Optional[str] = None
    bank_name: Optional[str] = None
    account_type: Optional[str] = None
    currency: Optional[str] = None
    balance: Optional[Decimal] = None
    erpnext_account: Optional[str] = None
    is_active: Optional[bool] = None


class BankAccountOut(Out):
    id: int
    account_name: str
    account_number: str
    bank_name: str
    account_type: str
    currency: str
    balance: Decimal
    erpnext_account: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CategoryIn(BaseModel):
    name: Optional[str] = None
    erpnext_account: Optional[str] = None
    transaction_type: Optional[str] = None
    keywords: Optional[str] = None
    tags: Optional[str] = None
    active: Optional[bool] = None
    color: Optional[int] = None


class CategoryOut(Out):
    id: int
    name: str
    erpnext_account: Optional[str]
    transaction_type: str
    keywords: str
    tags: str
    active: bool
    color: Optional[int]
    created_at: datetime


class StatementOut(Out):
    id: int
    gmail_id: str
    subject: str
    sender: str
    received_date: Optional[datetime]
    statement_date: Optional[date]
    bank_name: str
    account_number: str
    has_pdf: bool
    state: str
    is_processed: bool
    processed_date: Optional[datetime]
    transaction_count: int
    error_message: str
    created_at: datetime


class InvoiceItemIn(BaseModel):
    item_code: str = ""
    description: str
    quantity: Decimal = Decimal("1")
    unit_price: Decimal
    notes: str = ""


class InvoiceItemOut(Out):
    id: int
    item_code: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    total: Decimal
    notes: str


class InvoiceIn(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    customer_address: Optional[str] = None
    tax_rate: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    paid_amount: Optional[Decimal] = None
    currency: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    items: Optional[list[InvoiceItemIn]] = None

    @field_validator("due_date", mode="before")
    @classmethod
    def blank_date(cls, v):
        return v or None


class InvoiceOut(Out):
    id: int
    invoice_number: str
    invoice_date: date
    due_date: Optional[date]
    customer_name: str
    customer_email: str
    customer_address: str
    subtotal: Decimal
    tax_amount: Decimal
    tax_rate: Decimal
    discount_amount: Decimal
    total_amount: Decimal
    paid_amount: Decimal
    outstanding_amount: Decimal
    currency: str
    status: str
    erpnext_id: str
    erpnext_synced: bool
    notes: str
    terms: str
    is_paid: bool
    is_overdue: bool
    items: list[InvoiceItemOut]
    created_at: datetime
    updated_at: datetime


class TransactionIn(BaseModel):
    bank_account: Optional[int] = None
    date: Optional[dt.date] = None
    transaction_type: Optional[str] = None
    amount: Optional[Decimal] = None
    fee: Optional[Decimal] = None
    posting_date: Optional[dt.date] = None
    description: Optional[str] = None
    reference_number: Optional[str] = None
    deposit: Optional[Decimal] = None
    withdrawal: Optional[Decimal] = None
    balance: Optional[Decimal] = None
    currency: Optional[str] = None
    category: Optional[int] = None
    tags: Optional[str] = None
    notes: Optional[str] = None
    recon_status: Optional[str] = None


class TransactionOut(Out):
    id: int
    bank_account: Optional[int] = Field(validation_alias="bank_account_id")
    date: date
    transaction_type: str
    amount: Optional[Decimal]
    fee: Optional[Decimal]
    posting_date: Optional[date]
    description: str
    reference_number: str
    deposit: Optional[Decimal]
    withdrawal: Optional[Decimal]
    balance: Optional[Decimal]
    currency: str
    unallocated_amount: Optional[Decimal]
    category: Optional[int] = Field(validation_alias="category_id")
    category_name: Optional[str]
    tags: str
    notes: str
    recon_status: str
    erpnext_id: str
    erpnext_synced: bool
    erpnext_journal_entry: str
    erpnext_error: str
    created_at: datetime
    updated_at: datetime


class ERPNextConfigIn(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    default_company: Optional[str] = None
    bank_account: Optional[str] = None
    default_cost_center: Optional[str] = None
    is_active: Optional[bool] = None


class ERPNextConfigOut(Out):
    # api_key / api_secret are write-only
    id: int
    name: str
    base_url: str
    default_company: str
    bank_account: str
    default_cost_center: str
    is_active: bool
    last_sync: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class SyncLogOut(Out):
    id: int
    config: int = Field(validation_alias="config_id")
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
    statement: Optional[int] = Field(validation_alias="statement_id")
    created_at: datetime
    updated_at: datetime


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
    posting_date: date
    due_date: Optional[date]
    bill_no: str
    bill_date: Optional[date]
    fetched_at: datetime
    is_paid: bool
    is_overdue: bool
    amount_paid: Decimal


class JournalEntryOut(Out):
    id: int
    je_name: str
    posting_date: date
    amount: Decimal
    account: str
    reference_number: str
    remark: str
    fetched_at: datetime


class MatchIn(BaseModel):
    transaction: int
    journal_entry: Optional[int] = None
    status: str = "manual"
    flag_reason: str = ""


class MatchOut(Out):
    id: int
    transaction: int = Field(validation_alias="transaction_id")
    journal_entry: Optional[int] = Field(validation_alias="journal_entry_id")
    status: str
    flag_reason: str
    matched_at: datetime
    matched_by: str


class PeriodIn(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


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
