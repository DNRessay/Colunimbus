# Practice (the organisation using the app, e.g. a group of companies) -> Client (one business entity
# whose books are kept; shown as "Company" in the UI). Users belong to a practice. Bank data,
# reconciliation and ERPNext sync belong to a client, and each client maps to one ERPNext Company.
import datetime as dt
from calendar import month_name
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint,
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


def flag(default):
    return mapped_column(Boolean, default=default, nullable=False)


def created():
    return mapped_column(DateTime, default=utcnow, nullable=False)


def updated():
    return mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Practice(Base):
    __tablename__ = "practices"
    id: Mapped[int] = pk()
    name: Mapped[str] = text(200)
    created_at: Mapped[datetime] = created()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    role: Mapped[str] = text(20, "bookkeeper")  # owner | bookkeeper
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    email: Mapped[str] = text(254)
    first_name: Mapped[str] = text(150)
    last_name: Mapped[str] = text(150)
    password: Mapped[str] = text(128)
    is_active: Mapped[bool] = flag(True)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    date_joined: Mapped[datetime] = created()

    @property
    def is_owner(self):
        return self.role == "owner"


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    name: Mapped[str] = text(200)
    registration_number: Mapped[str] = text(50)
    vat_number: Mapped[str] = text(20)
    vat_registered: Mapped[bool] = flag(False)
    year_end_month: Mapped[int] = mapped_column(Integer, default=2, nullable=False)  # SA default: February
    contact_email: Mapped[str] = text(254)
    notes: Mapped[str] = text()
    erpnext_company: Mapped[str] = text(200)
    erpnext_bank_account: Mapped[str] = text(200)  # fallback when a bank account has no mapping
    erpnext_cost_center: Mapped[str] = text(200)
    is_active: Mapped[bool] = flag(True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


def client_fk():
    return fk("clients.id")


class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id: Mapped[int] = pk()
    client_id: Mapped[int] = client_fk()
    account_name: Mapped[str] = text(200)
    account_number: Mapped[str] = text(100)
    bank_name: Mapped[str] = text(100)
    account_type: Mapped[str] = text(50)
    currency: Mapped[str] = text(3, "ZAR")
    erpnext_account: Mapped[str] = text(200)
    is_active: Mapped[bool] = flag(True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


def _csv_list(value):
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


class TransactionCategory(Base):
    """Shared across a practice's clients so merchant keywords learned on one client help the others.
    The ERPNext account differs per client (company), so it lives in CategoryAccount."""

    __tablename__ = "transaction_categories"
    __table_args__ = (UniqueConstraint("practice_id", "name"),)
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    name: Mapped[str] = text(100)
    transaction_type: Mapped[str] = text(20, "debit")  # debit | credit | any
    keywords: Mapped[str] = text()
    tags: Mapped[str] = text()
    active: Mapped[bool] = flag(True)
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
        if word and word not in self.keywords_list:
            self.keywords = ",".join(self.keywords_list + [word])

    def add_tag(self, word):
        word = (word or "").strip().lower()
        if word and word not in self.tags_list:
            self.tags = ",".join(self.tags_list + [word])

    def match(self, description):
        desc = (description or "").lower()
        if not desc:
            return None
        return next((w for w in self.keywords_list + self.tags_list if w in desc), None)


class CategoryAccount(Base):
    """Which ERPNext account a category posts to for one client."""

    __tablename__ = "category_accounts"
    __table_args__ = (UniqueConstraint("client_id", "category_id"),)
    id: Mapped[int] = pk()
    client_id: Mapped[int] = client_fk()
    category_id: Mapped[int] = fk("transaction_categories.id")
    erpnext_account: Mapped[str] = text(200)


class EmailStatement(Base):
    __tablename__ = "statements"
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    client_id: Mapped[Optional[int]] = fk("clients.id", "CASCADE", True)  # null = unassigned inbox item
    bank_account_id: Mapped[Optional[int]] = fk("bank_accounts.id", "SET NULL", True)
    source: Mapped[str] = text(20, "upload")  # gmail | upload
    gmail_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    subject: Mapped[str] = text(500)
    sender: Mapped[str] = text(255)
    received_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    bank_name: Mapped[str] = text(100)
    has_attachment: Mapped[bool] = flag(False)
    pdf_password: Mapped[str] = text(100)
    state: Mapped[str] = text(50, "new")  # new | parsed | error
    processed_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    transaction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    body_text: Mapped[str] = text()
    error_message: Mapped[str] = text()
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    id: Mapped[int] = pk()
    client_id: Mapped[int] = client_fk()
    bank_account_id: Mapped[Optional[int]] = fk("bank_accounts.id", "SET NULL", True)
    statement_id: Mapped[Optional[int]] = fk("statements.id", "SET NULL", True)
    category_id: Mapped[Optional[int]] = fk("transaction_categories.id", "SET NULL", True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    posting_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    transaction_type: Mapped[str] = text(20)  # debit | credit
    description: Mapped[str] = text(500)
    reference_number: Mapped[str] = text(100)
    amount: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    deposit: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    withdrawal: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    fee: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    balance: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    currency: Mapped[str] = text(3, "ZAR")
    tags: Mapped[str] = text(500)  # e.g. the bank's own category label
    notes: Mapped[str] = text()
    recon_status: Mapped[str] = text(20, "unreconciled")
    erpnext_synced: Mapped[bool] = flag(False)
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
    """The practice's ERPNext site. One active per practice; companies inside it map to clients."""

    __tablename__ = "erpnext_configs"
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    name: Mapped[str] = text(100)
    base_url: Mapped[str] = text(255)
    api_key: Mapped[str] = text(255)
    api_secret: Mapped[str] = text(255)
    is_active: Mapped[bool] = flag(True)
    last_sync: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class ERPNextSyncLog(Base):
    __tablename__ = "erpnext_sync_logs"
    id: Mapped[int] = pk()
    client_id: Mapped[int] = client_fk()
    config_id: Mapped[Optional[int]] = fk("erpnext_configs.id", "SET NULL", True)
    record_type: Mapped[str] = text(50)
    record_id: Mapped[int] = mapped_column(BigId, nullable=False)
    erpnext_doctype: Mapped[str] = text(100)
    erpnext_doc_name: Mapped[str] = text(200)
    status: Mapped[str] = text(20)
    error_message: Mapped[str] = text()
    sync_date: Mapped[datetime] = created()


class PDFImportJob(Base):
    __tablename__ = "pdf_import_jobs"
    id: Mapped[int] = pk()
    client_id: Mapped[int] = client_fk()
    bank_account_id: Mapped[Optional[int]] = fk("bank_accounts.id", "SET NULL", True)
    filename: Mapped[str] = text(255)
    bank_name: Mapped[str] = text(100, "capitec")
    pdf_password: Mapped[str] = text(100)
    status: Mapped[str] = text(20, "pending")  # pending | processing | done | failed
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_files: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    processed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transactions_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transactions_saved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transactions_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str] = text()
    statement_id: Mapped[Optional[int]] = fk("statements.id", "SET NULL", True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class UserGmailToken(Base):
    __tablename__ = "user_gmail_tokens"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(BigId, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    access_token: Mapped[str] = text()
    refresh_token: Mapped[str] = text()
    token_expiry: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_connected: Mapped[bool] = flag(True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class ERPNextInvoice(Base):
    __tablename__ = "erpnext_invoices"
    __table_args__ = (UniqueConstraint("client_id", "erp_name"),)
    id: Mapped[int] = pk()
    client_id: Mapped[int] = client_fk()
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
    __tablename__ = "erpnext_journal_entries"
    __table_args__ = (UniqueConstraint("client_id", "je_name"),)
    id: Mapped[int] = pk()
    client_id: Mapped[int] = client_fk()
    je_name: Mapped[str] = text(100)
    posting_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    account: Mapped[str] = text(255)
    reference_number: Mapped[str] = text(255)
    remark: Mapped[str] = text()
    fetched_at: Mapped[datetime] = created()


class ReconciliationMatch(Base):
    __tablename__ = "reconciliation_matches"
    id: Mapped[int] = pk()
    client_id: Mapped[int] = client_fk()
    transaction_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("bank_transactions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    journal_entry_id: Mapped[Optional[int]] = fk("erpnext_journal_entries.id", "SET NULL", True)
    status: Mapped[str] = text(20, "matched")  # matched | flagged | manual
    flag_reason: Mapped[str] = text()
    matched_at: Mapped[datetime] = created()
    matched_by: Mapped[str] = text(20, "auto")

    transaction: Mapped[BankTransaction] = relationship(back_populates="recon_match")
    journal_entry: Mapped[Optional[ERPNextJournalEntry]] = relationship(lazy="joined")


class ReconciliationPeriod(Base):
    __tablename__ = "reconciliation_periods"
    __table_args__ = (UniqueConstraint("client_id", "year", "month"),)
    id: Mapped[int] = pk()
    client_id: Mapped[int] = client_fk()
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

    __tablename__ = "jobs"
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    client_id: Mapped[Optional[int]] = fk("clients.id", "CASCADE", True)
    user_id: Mapped[Optional[int]] = fk("users.id", "SET NULL", True)
    kind: Mapped[str] = text(40)
    status: Mapped[str] = text(20, "queued")  # queued | in_progress | completed
    conclusion: Mapped[str] = text(20)  # success | failure
    message: Mapped[str] = text()
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class AccessRequest(Base):
    """Someone whose email domain isn't registered asking to be let in (emailed to the admin)."""

    __tablename__ = "access_requests"
    id: Mapped[int] = pk()
    email: Mapped[str] = text(254)
    name: Mapped[str] = text(200)
    company: Mapped[str] = text(200)
    message: Mapped[str] = text()
    created_at: Mapped[datetime] = created()


class StatementPassword(Base):
    """Passwords that open the practice's statement PDFs (sealed). Tried on every Gmail statement, so nobody types them."""

    __tablename__ = "statement_passwords"
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    label: Mapped[str] = text(100)
    secret: Mapped[str] = text()
    created_at: Mapped[datetime] = created()


class PayShapNotice(Base):
    """A PayShap payment caught from the bank's notification email, before (and matched to) the statement line."""

    __tablename__ = "payshap_notices"
    id: Mapped[int] = pk()
    practice_id: Mapped[int] = fk("practices.id")
    client_id: Mapped[Optional[int]] = fk("clients.id", "CASCADE", True)
    gmail_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    direction: Mapped[str] = text(10)  # in | out
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    counterparty: Mapped[str] = text(200)
    reference: Mapped[str] = text(200)
    subject: Mapped[str] = text(500)
    transaction_id: Mapped[Optional[int]] = fk("bank_transactions.id", "SET NULL", True)


class McpKey(Base):
    """A key another app (e.g. SEMBLANCE) uses to read this user's books over MCP. Only the hash is stored."""

    __tablename__ = "mcp_keys"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = fk("users.id")
    name: Mapped[str] = text(100)
    prefix: Mapped[str] = text(16)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = created()
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
