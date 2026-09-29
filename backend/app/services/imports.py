import logging
import uuid
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import BankTransaction, EmailStatement, PDFImportJob, TransactionCategory, utcnow
from . import categorize
from .parsers import parse_pdf

log = logging.getLogger(__name__)


def is_duplicate(db: Session, user_id, day, description, value):
    return db.scalar(
        select(BankTransaction.id).where(
            BankTransaction.user_id == user_id,
            BankTransaction.date == day,
            BankTransaction.description == description,
            or_(BankTransaction.amount == value, BankTransaction.withdrawal == value, BankTransaction.deposit == value),
        ).limit(1)
    ) is not None


def new_txn(user_id, statement_id, day, description, value, kind, **extra):
    value = Decimal(str(value))
    return BankTransaction(
        user_id=user_id, statement_id=statement_id, date=day, description=description[:500],
        amount=value, transaction_type=kind,
        deposit=value if kind == "credit" else None,
        withdrawal=value if kind == "debit" else None,
        **extra,
    )


def save_csv_rows(db: Session, user_id, rows, statement_id=None):
    imported = skipped = 0
    for r in rows:
        value = r["debits"] or r["credits"]
        if not value:
            skipped += 1
            continue
        kind = "debit" if r["debits"] else "credit"
        if is_duplicate(db, user_id, r["transaction_date"], r["description"], value):
            skipped += 1
            continue
        db.add(new_txn(
            user_id, statement_id, r["transaction_date"], r["description"], value, kind,
            posting_date=r["posting_date"], balance=r["balance"], reference_number=r["reference"][:100],
        ))
        db.flush()
        imported += 1
    return imported, skipped


def save_pdf_rows(db: Session, user_id, rows, statement_id):
    saved = skipped = 0
    for t in rows:
        if is_duplicate(db, user_id, t["date"], t["description"], Decimal(str(t["amount"]))):
            skipped += 1
            continue
        category = None
        if t.get("category"):
            # Capitec prints its own category next to each line; keep it as a starting point.
            category = db.scalar(select(TransactionCategory).where(TransactionCategory.name == t["category"]))
            if not category:
                category = TransactionCategory(name=t["category"], transaction_type=t["type"], active=True)
                db.add(category)
                db.flush()
        balance = t.get("balance")
        db.add(new_txn(
            user_id, statement_id, t["date"], t["description"], t["amount"], t["type"],
            reference_number=t["reference"][:100],
            balance=Decimal(str(balance)) if balance is not None else None,
            fee=Decimal(str(t["fee"])) if t.get("fee") else None,
            category_id=category.id if category else None,
        ))
        db.flush()
        saved += 1
    return saved, skipped


def upload_statement(user_id, subject, sender, **kw):
    return EmailStatement(
        user_id=user_id, gmail_id=f"upload-{uuid.uuid4().hex}", subject=subject[:500], sender=sender,
        received_date=utcnow(), **kw,
    )


def run_pdf_job(db: Session, job_id: int, files):
    """files: list of (pdf_bytes, filename). Updates the PDFImportJob row as it goes."""
    job = db.get(PDFImportJob, job_id)
    if not job:
        log.error("PDF job %s not found", job_id)
        return
    try:
        job.status = "processing"
        job.total_files = len(files)
        db.commit()
        found = saved_total = skipped_total = 0

        for idx, (pdf_bytes, filename) in enumerate(files, start=1):
            st = upload_statement(job.user_id, filename, "PDF Upload", bank_name=job.bank_name,
                                  has_pdf=True, pdf_password=job.pdf_password, state="imported")
            db.add(st)
            db.flush()
            if idx == 1:
                job.statement_id = st.id
            db.commit()
            try:
                rows = parse_pdf(pdf_bytes, job.bank_name, job.pdf_password or None)
                found += len(rows)
                saved, skipped = save_pdf_rows(db, job.user_id, rows, st.id)
                st.transaction_count = saved
                st.state = "parsed"
                st.is_processed = True
                st.processed_date = utcnow()
                saved_total += saved
                skipped_total += skipped
            except Exception as e:
                db.rollback()
                st.state = "error"
                st.error_message = str(e)
                job.error_message = f"{filename}: {e}"
                log.error("Error parsing %s: %s", filename, e)

            job.processed_files = idx
            job.progress = int(idx / len(files) * 100)
            job.transactions_found = found
            job.transactions_saved = saved_total
            job.transactions_skipped = skipped_total
            db.commit()

        categorize.auto_categorize(db, job.user_id)
        job.status = "done"
        job.progress = 100
        db.commit()
    except Exception as e:
        db.rollback()
        job = db.get(PDFImportJob, job_id)
        job.status = "failed"
        job.error_message = str(e)
        db.commit()
        log.exception("PDF job %s failed", job_id)
