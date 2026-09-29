# Table and column names match the previous Django schema so an existing
# database can be reused as-is. Only `lsuite_jobs` is new.
from calendar import month_name
import datetime as dt
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

BigId = BigInteger().with_variant(Integer, "sqlite")
Money = Numeric(15, 2)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def pk():
    return mapped_column(BigId, primary_key=True, autoincrement=True)


def fk(target, ondelete="CASCADE", nullable=False, **kw):
    return mapped_column(BigId, ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True, **kw)


def text(length=None, default=""):
    return mapped_column(String(length) if length else Text, default=default, nullable=False)


def created():
    return mapped_column(DateTime, default=utcnow, nullable=False)


def updated():
    return mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class User(Base):
    __tablename__ = "auth_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    password: Mapped[str] = text(128)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    first_name: Mapped[str] = text(150)
    last_name: Mapped[str] = text(150)
    email: Mapped[str] = text(254)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    date_joined: Mapped[datetime] = created()

    profile: Mapped[Optional["UserProfile"]] = relationship(back_populates="user", uselist=False)


def user_fk():
    return mapped_column(Integer, ForeignKey("auth_user.id", ondelete="CASCADE"), nullable=False, index=True)


class UserProfile(Base):
    __tablename__ = "authusers_userprofile"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("auth_user.id", ondelete="CASCADE"), unique=True, nullable=False)
    phone: Mapped[str] = text(30)
    date_of_birth: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    id_number: Mapped[str] = text(20)
    city: Mapped[str] = text(100)
    province: Mapped[str] = text(100)
    country: Mapped[str] = text(100)
    occupation: Mapped[str] = text(100)
    years_experience: Mapped[str] = text(10)
    industry: Mapped[str] = text(100)
    linkedin_url: Mapped[str] = text(200)
    github_url: Mapped[str] = text(200)
    portfolio_url: Mapped[str] = text(200)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()

    user: Mapped[User] = relationship(back_populates="profile")


PLATFORM_ICONS = {
    "linkedin": "🔗", "github": "🐙", "twitter": "🐦", "instagram": "📷",
    "facebook": "📘", "youtube": "▶️", "tiktok": "🎵", "behance": "🎨",
    "dribbble": "🏀", "stackoverflow": "📚", "kaggle": "📊", "medium": "✍️",
    "substack": "📬", "portfolio": "🌐",
}


class SocialLink(Base):
    __tablename__ = "authusers_sociallink"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    platform: Mapped[str] = text(80)
    url: Mapped[str] = text(500)
    icon: Mapped[str] = text(10)
    created_at: Mapped[datetime] = created()

    @property
    def display_icon(self):
        if self.icon:
            return self.icon
        key = self.platform.lower().split("/")[0].replace(" ", "").strip()
        return next((v for k, v in PLATFORM_ICONS.items() if k in key), "🔗")


class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    account_name: Mapped[str] = text(200)
    account_number: Mapped[str] = text(100)
    bank_name: Mapped[str] = text(100)
    account_type: Mapped[str] = text(50)
    currency: Mapped[str] = text(3, "ZAR")
    balance: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    erpnext_account: Mapped[str] = text(200)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


def _csv_list(value):
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


class TransactionCategory(Base):
    __tablename__ = "transaction_categories"
    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    erpnext_account: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    transaction_type: Mapped[str] = text(20)
    keywords: Mapped[str] = text()
    tags: Mapped[str] = text()
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    color: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = created()

    @property
    def keywords_list(self):
        return _csv_list(self.keywords)

    @property
    def tags_list(self):
        return _csv_list(self.tags)

    def add_keyword(self, word):
        word = (word or "").strip().lower()
        existing = self.keywords_list
        if word and word not in existing:
            self.keywords = ",".join(existing + [word])

    def add_tag(self, word):
        word = (word or "").strip().lower()
        existing = self.tags_list
        if word and word not in existing:
            self.tags = ",".join(existing + [word])

    def match(self, description):
        """Returns the keyword/tag that matched, or None."""
        desc = (description or "").lower()
        if not desc:
            return None
        return next((w for w in self.keywords_list + self.tags_list if w in desc), None)


class EmailStatement(Base):
    __tablename__ = "email_statements"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    gmail_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    thread_id: Mapped[str] = text(255)
    subject: Mapped[str] = text(500)
    sender: Mapped[str] = text(255)
    received_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    statement_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    bank_name: Mapped[str] = text(100)
    account_number: Mapped[str] = text(100)
    has_pdf: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pdf_password: Mapped[str] = text(100)
    state: Mapped[str] = text(50, "new")
    is_processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processed_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    transaction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    body_text: Mapped[str] = text()
    body_html: Mapped[str] = text()
    error_message: Mapped[str] = text()
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    invoice_number: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    invoice_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    due_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    customer_name: Mapped[str] = text(200)
    customer_email: Mapped[str] = text(254)
    customer_address: Mapped[str] = text()
    subtotal: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    paid_amount: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    outstanding_amount: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    currency: Mapped[str] = text(3, "ZAR")
    status: Mapped[str] = text(50, "draft")
    erpnext_id: Mapped[str] = text(100)
    erpnext_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    erpnext_sync_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = text()
    terms: Mapped[str] = text()
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()

    items: Mapped[list["InvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceItem.id"
    )

    @property
    def is_paid(self):
        return (self.outstanding_amount or 0) <= 0

    @property
    def is_overdue(self):
        return bool(self.due_date and self.status not in ("paid", "cancelled") and date.today() > self.due_date)

    def calculate_totals(self):
        cents = Decimal("0.01")
        for item in self.items:
            item.total = (Decimal(item.quantity or 0) * Decimal(item.unit_price or 0)).quantize(cents)
        self.subtotal = sum((i.total for i in self.items), Decimal("0")).quantize(cents)
        self.tax_amount = (self.subtotal * Decimal(self.tax_rate or 0) / 100).quantize(cents)
        self.total_amount = (self.subtotal + self.tax_amount - Decimal(self.discount_amount or 0)).quantize(cents)
        self.outstanding_amount = (self.total_amount - Decimal(self.paid_amount or 0)).quantize(cents)


class InvoiceItem(Base):
    __tablename__ = "invoice_items"
    id: Mapped[int] = pk()
    invoice_id: Mapped[int] = fk("invoices.id")
    item_code: Mapped[str] = text(100)
    description: Mapped[str] = text(500)
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=1, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    notes: Mapped[str] = text(500)
    created_at: Mapped[datetime] = created()

    invoice: Mapped[Invoice] = relationship(back_populates="items")


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    bank_account_id: Mapped[Optional[int]] = fk("bank_accounts.id", "SET NULL", True)
    statement_id: Mapped[Optional[int]] = fk("email_statements.id", "SET NULL", True)
    invoice_id: Mapped[Optional[int]] = fk("invoices.id", "SET NULL", True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    transaction_type: Mapped[str] = text(100)
    amount: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    fee: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    posting_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    description: Mapped[str] = text(500)
    reference_number: Mapped[str] = text(100)
    deposit: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    withdrawal: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    balance: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    currency: Mapped[str] = text(3, "ZAR")
    unallocated_amount: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    category_id: Mapped[Optional[int]] = fk("transaction_categories.id", "SET NULL", True)
    tags: Mapped[str] = text(500)
    notes: Mapped[str] = text()
    is_categorized: Mapped[str] = text()
    is_reconciled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reconciled_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    recon_status: Mapped[str] = text(20, "unreconciled")
    erpnext_id: Mapped[str] = text(100)
    erpnext_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    erpnext_sync_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    erpnext_journal_entry: Mapped[str] = text(100)
    erpnext_error: Mapped[str] = text()
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()

    category: Mapped[Optional[TransactionCategory]] = relationship(lazy="joined")
    bank_account: Mapped[Optional[BankAccount]] = relationship()
    recon_match: Mapped[Optional["ReconciliationMatch"]] = relationship(
        back_populates="transaction", uselist=False, passive_deletes=True
    )

    @property
    def category_name(self):
        return self.category.name if self.category else None

    @property
    def direction(self):
        """'credit' or 'debit' regardless of which importer created the row."""
        if self.transaction_type in ("credit", "debit"):
            return self.transaction_type
        return "credit" if (self.deposit or 0) > 0 else "debit"

    @property
    def value(self):
        for v in (self.withdrawal, self.deposit, self.amount):
            if v:
                return abs(Decimal(v))
        return Decimal("0")


class ERPNextConfig(Base):
    __tablename__ = "erpnext_configs"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    name: Mapped[str] = text(100)
    base_url: Mapped[str] = text(255)
    api_key: Mapped[str] = text(255)
    api_secret: Mapped[str] = text(255)
    default_company: Mapped[str] = text(200)
    bank_account: Mapped[str] = text(200)
    default_cost_center: Mapped[str] = text(200)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_sync: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class ERPNextSyncLog(Base):
    __tablename__ = "erpnext_sync_logs"
    id: Mapped[int] = pk()
    config_id: Mapped[int] = fk("erpnext_configs.id")
    record_type: Mapped[str] = text(50)
    record_id: Mapped[int] = mapped_column(Integer, nullable=False)
    erpnext_doctype: Mapped[str] = text(100)
    erpnext_doc_name: Mapped[str] = text(200)
    status: Mapped[str] = text(20)
    error_message: Mapped[str] = text()
    sync_date: Mapped[datetime] = created()


class PDFImportJob(Base):
    __tablename__ = "pdf_import_jobs"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    filename: Mapped[str] = text(255)
    bank_name: Mapped[str] = text(100, "capitec")
    pdf_password: Mapped[str] = text(100)
    status: Mapped[str] = text(20, "pending")
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_files: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    processed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transactions_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transactions_saved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transactions_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str] = text()
    statement_id: Mapped[Optional[int]] = fk("email_statements.id", "SET NULL", True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class UserGmailToken(Base):
    __tablename__ = "user_gmail_tokens"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("auth_user.id", ondelete="CASCADE"), unique=True, nullable=False)
    access_token: Mapped[str] = text()
    refresh_token: Mapped[str] = text()
    token_expiry: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_connected: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class ERPNextInvoice(Base):
    __tablename__ = "invoices_erpnextinvoice"
    __table_args__ = (UniqueConstraint("user_id", "erp_name"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    invoice_type: Mapped[str] = text(20)
    erp_name: Mapped[str] = text(100)
    erp_status: Mapped[str] = text(50, "Unpaid")
    party_id: Mapped[str] = text(200)
    party_name: Mapped[str] = text(255)
    currency: Mapped[str] = text(10, "ZAR")
    grand_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    outstanding_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    posting_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    due_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    bill_no: Mapped[str] = text(100)
    bill_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    fetched_at: Mapped[datetime] = updated()
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    @property
    def is_paid(self):
        return self.erp_status == "Paid"

    @property
    def is_overdue(self):
        return bool(self.erp_status in ("Unpaid", "Partly Paid") and self.due_date and self.due_date < date.today())

    @property
    def amount_paid(self):
        return Decimal(self.grand_total or 0) - Decimal(self.outstanding_amount or 0)


class ERPNextJournalEntry(Base):
    __tablename__ = "reconciliation_erpnextjournalentry"
    __table_args__ = (UniqueConstraint("user_id", "je_name"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    je_name: Mapped[str] = text(100)
    posting_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    account: Mapped[str] = text(255)
    reference_number: Mapped[str] = text(255)
    remark: Mapped[str] = text()
    fetched_at: Mapped[datetime] = created()


class ReconciliationMatch(Base):
    __tablename__ = "reconciliation_reconciliationmatch"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    transaction_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("bank_transactions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    journal_entry_id: Mapped[Optional[int]] = fk("reconciliation_erpnextjournalentry.id", "SET NULL", True)
    status: Mapped[str] = text(20, "matched")
    flag_reason: Mapped[str] = text()
    matched_at: Mapped[datetime] = created()
    matched_by: Mapped[str] = text(20, "auto")

    transaction: Mapped[BankTransaction] = relationship(back_populates="recon_match")
    journal_entry: Mapped[Optional[ERPNextJournalEntry]] = relationship(lazy="joined")


class ReconciliationPeriod(Base):
    __tablename__ = "reconciliation_reconciliationperiod"
    __table_args__ = (UniqueConstraint("user_id", "year", "month"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = text(10, "open")
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    total_transactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flagged_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unreconciled_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def label(self):
        return f"{month_name[self.month]} {self.year}"

    @property
    def can_close(self):
        return self.unreconciled_count == 0 and self.flagged_count == 0


class Job(Base):
    """Background work (AI categorization, ERPNext bulk sync) run by the worker Lambda."""

    __tablename__ = "lsuite_jobs"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    kind: Mapped[str] = text(40)
    status: Mapped[str] = text(20, "queued")  # queued | in_progress | completed
    conclusion: Mapped[str] = text(20)  # success | failure
    message: Mapped[str] = text()
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
