import logging
import uuid
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import BankTransaction, Client, EmailStatement, PDFImportJob, utcnow
from ..security import unseal
from . import categorize
from .parsers import parse_pdf

log = logging.getLogger(__name__)


def is_duplicate(db: Session, client_id, day, description, value):
    return db.scalar(
        select(BankTransaction.id).where(
            BankTransaction.client_id == client_id,
            BankTransaction.date == day,
            BankTransaction.description == description,
            or_(BankTransaction.amount == value, BankTransaction.withdrawal == value, BankTransaction.deposit == value),
        ).limit(1)
    ) is not None


def new_txn(client_id, day, description, value, kind, **extra):
    value = Decimal(str(value))
    return BankTransaction(
        client_id=client_id, date=day, description=description[:500], amount=value, transaction_type=kind,
        deposit=value if kind == "credit" else None, withdrawal=value if kind == "debit" else None, **extra,
    )


def save_csv_rows(db: Session, client_id, rows, statement_id=None, bank_account_id=None):
    imported = skipped = 0
    for r in rows:
        value = r["debits"] or r["credits"]
        if not value or is_duplicate(db, client_id, r["transaction_date"], r["description"], value):
            skipped += 1
            continue
        db.add(new_txn(
            client_id, r["transaction_date"], r["description"], value, "debit" if r["debits"] else "credit",
            statement_id=statement_id, bank_account_id=bank_account_id, posting_date=r["posting_date"],
            balance=r["balance"], reference_number=r["reference"][:100],
        ))
        db.flush()
        imported += 1
    return imported, skipped


def save_pdf_rows(db: Session, client_id, rows, statement_id=None, bank_account_id=None):
    saved = skipped = 0
    for t in rows:
        if is_duplicate(db, client_id, t["date"], t["description"], Decimal(str(t["amount"]))):
            skipped += 1
            continue
        balance = t.get("balance")
        db.add(new_txn(
            client_id, t["date"], t["description"], t["amount"], t["type"],
            statement_id=statement_id, bank_account_id=bank_account_id, reference_number=t["reference"][:100],
            balance=Decimal(str(balance)) if balance is not None else None,
            fee=Decimal(str(t["fee"])) if t.get("fee") else None,
            tags=(t.get("category") or "")[:500],  # the bank's own label, kept as a hint
        ))
        db.flush()
        saved += 1
    return saved, skipped


def upload_statement(client: Client, subject, **kw):
    return EmailStatement(
        practice_id=client.practice_id, client_id=client.id, source="upload", gmail_id=f"upload-{uuid.uuid4().hex}",
        subject=subject[:500], sender="Upload", received_date=utcnow(), **kw,
    )


def run_pdf_job(db: Session, job_id: int, files):
    """files: list of (pdf_bytes, filename). Updates the PDFImportJob row as it goes."""
    job = db.get(PDFImportJob, job_id)
    if not job:
        log.error("PDF job %s not found", job_id)
        return
    client = db.get(Client, job.client_id)
    try:
        job.status = "processing"
        job.total_files = len(files)
        db.commit()
        found = saved_total = skipped_total = 0
        password = unseal(job.pdf_password) or None

        for idx, (pdf_bytes, filename) in enumerate(files, start=1):
            st = upload_statement(client, filename, bank_name=job.bank_name, bank_account_id=job.bank_account_id,
                                  has_attachment=True, pdf_password=job.pdf_password)
            db.add(st)
            db.flush()
            if idx == 1:
                job.statement_id = st.id
            db.commit()
            try:
                rows = parse_pdf(pdf_bytes, job.bank_name, password)
                found += len(rows)
                saved, skipped = save_pdf_rows(db, client.id, rows, st.id, job.bank_account_id)
                st.transaction_count, st.state, st.processed_date = saved, "parsed", utcnow()
                saved_total += saved
                skipped_total += skipped
            except Exception as e:
                db.rollback()
                st.state, st.error_message = "error", str(e)
                job.error_message = f"{filename}: {e}"
                log.error("Error parsing %s: %s", filename, e)

            job.processed_files = idx
            job.progress = int(idx / len(files) * 100)
            job.transactions_found, job.transactions_saved, job.transactions_skipped = found, saved_total, skipped_total
            db.commit()

        categorize.auto_categorize(db, client)
        job.status, job.progress = "done", 100
        db.commit()
    except Exception as e:
        db.rollback()
        job = db.get(PDFImportJob, job_id)
        job.status, job.error_message = "failed", str(e)
        db.commit()
        log.exception("PDF job %s failed", job_id)
