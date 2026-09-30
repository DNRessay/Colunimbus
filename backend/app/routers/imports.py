import logging
from datetime import timedelta
from typing import Optional

import jwt
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import Ctx, client_ctx, current_user, get_db, get_owned
from ..jobs import start_pdf_import
from ..models import BankAccount, EmailStatement, PDFImportJob, User, UserGmailToken
from ..security import make_token, read_token, seal
from ..services import gmail
from ..services.imports import save_csv_rows, upload_statement
from ..services.parsers import CSV_TEMPLATE, parse_csv

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/imports", tags=["imports"])
callback_router = APIRouter(tags=["imports"])

MAX_UPLOAD = 20 * 1024 * 1024


def connected_token(db, user):
    token = db.scalar(select(UserGmailToken).where(UserGmailToken.user_id == user.id, UserGmailToken.is_connected.is_(True)))
    if not token:
        raise HTTPException(400, "Gmail not connected.")
    return token


def frontend(path):
    return RedirectResponse(f"{settings.frontend_url}{path}", status_code=302)


def bank_account(db, ctx: Ctx, bank_account_id: Optional[int]):
    return get_owned(db, BankAccount, bank_account_id, ctx) if bank_account_id else None


# ── Gmail (per user; statements land in the organisation inbox) ────────────

@router.get("/gmail/status")
def status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    token = db.scalar(select(UserGmailToken).where(UserGmailToken.user_id == user.id))
    return {"connected": bool(token and token.is_connected), "configured": bool(settings.google_client_id),
            "connected_since": token.created_at if token else None}


@router.get("/gmail/connect")
def connect(request: Request, user: User = Depends(current_user)):
    if not settings.google_client_id:
        raise HTTPException(400, "Google OAuth is not configured on the server.")
    state = make_token("gmail", user.id, timedelta(minutes=10))
    return {"auth_url": gmail.auth_url(state, str(request.base_url))}


@router.post("/gmail/disconnect")
def disconnect(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(delete(UserGmailToken).where(UserGmailToken.user_id == user.id))
    db.commit()
    return {"message": "Gmail disconnected."}


@callback_router.get("/api/gmail/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    if not code:
        return frontend("/imports.html?error=oauth_failed")
    try:
        user_id = int(read_token(state, "gmail")["sub"])
    except (jwt.PyJWTError, ValueError):
        return frontend("/imports.html?error=invalid_state")
    ok = gmail.exchange_code(db, user_id, code, str(request.base_url))
    return frontend("/imports.html" + ("?connected=1" if ok else "?error=token_exchange"))


@router.post("/gmail/fetch")
def fetch_from_gmail(user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        imported, skipped = gmail.Gmail(db, connected_token(db, user)).fetch_statements(user.practice_id)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Gmail fetch failed")
        raise HTTPException(502, f"Import failed: {e}")
    return {"message": f"Found {imported} new statement email(s) ({skipped} already imported).",
            "imported": imported, "skipped": skipped}


@router.post("/statements/{st_id}/parse")
def parse_statement(st_id: int, pdf_password: str = Body("", embed=True), save_password: bool = Body(False, embed=True),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    st = get_owned(db, EmailStatement, st_id, user)
    if not st.client_id:
        raise HTTPException(400, "Assign this statement to a company first.")
    if st.source != "gmail":
        raise HTTPException(400, "Only Gmail statements can be re-parsed.")
    client = gmail.Gmail(db, connected_token(db, user))
    pdf_password = pdf_password.strip()
    if save_password and pdf_password:
        st.pdf_password = seal(pdf_password)
        db.commit()
    try:
        try:
            count = client.parse_pdf_statement(st, pdf_password or None)
        except ValueError as e:
            if "No PDF attachment" not in str(e):
                raise
            count, _ = client.parse_csv_statement(st)  # CSV-only emails
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("Parse failed for statement %s", st_id)
        raise HTTPException(502, f"Parse failed: {e}")
    return {"message": f"Extracted {count} transactions.", "count": count}


# ── Uploads (into the selected company) ─────────────────────────────────────

async def _read(upload: UploadFile, ext: str):
    if not (upload.filename or "").lower().endswith(ext):
        raise HTTPException(400, f"Please upload a {ext} file.")
    data = await upload.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{upload.filename} is larger than 20 MB.")
    return data


@router.post("/csv")
async def upload_csv(csv_files: list[UploadFile] = File(...), bank_account_id: Optional[int] = Form(None),
                     ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    ba = bank_account(db, ctx, bank_account_id)
    total_imported = total_skipped = 0
    errors = []
    for f in csv_files:
        try:
            rows = parse_csv(await _read(f, ".csv"))
            if not rows:
                raise ValueError("no valid transactions found")
            st = upload_statement(ctx.client, f"CSV: {f.filename}", bank_name=ba.bank_name if ba else "",
                                  bank_account_id=ba.id if ba else None, has_attachment=True, state="parsed")
            db.add(st)
            db.flush()
            imported, skipped = save_csv_rows(db, ctx.client_id, rows, st.id, st.bank_account_id)
            st.transaction_count = imported
            db.commit()
            total_imported += imported
            total_skipped += skipped
        except Exception as e:
            db.rollback()
            errors.append(f"{f.filename}: {getattr(e, 'detail', e)}")
    if errors and not total_imported:
        raise HTTPException(400, "; ".join(errors))
    return {"message": f"Imported {total_imported} transactions ({total_skipped} duplicates skipped).",
            "imported": total_imported, "skipped": total_skipped, "errors": errors}


@router.get("/csv-template")
def csv_template(user: User = Depends(current_user)):
    return Response(CSV_TEMPLATE, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=transaction_template.csv"})


@router.post("/pdf", status_code=202)
async def upload_pdf(pdf_files: list[UploadFile] = File(...), bank_name: str = Form("capitec"),
                     pdf_password: str = Form(""), bank_account_id: Optional[int] = Form(None),
                     ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    ba = bank_account(db, ctx, bank_account_id)
    files = [(await _read(f, ".pdf"), f.filename) for f in pdf_files]
    job = PDFImportJob(client_id=ctx.client_id, bank_account_id=ba.id if ba else None,
                       filename=", ".join(n for _, n in files)[:255], bank_name=(ba.bank_name if ba and ba.bank_name else bank_name),
                       pdf_password=seal(pdf_password), status="pending", total_files=len(files))
    db.add(job)
    db.commit()
    start_pdf_import(job.id, files)
    return {"job_id": job.id}


@router.get("/pdf/{job_id}")
def pdf_status(job_id: int, ctx: Ctx = Depends(client_ctx), db: Session = Depends(get_db)):
    job = get_owned(db, PDFImportJob, job_id, ctx)
    return {
        "status": job.status, "progress": job.progress, "total_files": job.total_files,
        "processed_files": job.processed_files, "transactions_found": job.transactions_found,
        "transactions_saved": job.transactions_saved, "transactions_skipped": job.transactions_skipped,
        "error_message": job.error_message, "statement_id": job.statement_id,
    }
