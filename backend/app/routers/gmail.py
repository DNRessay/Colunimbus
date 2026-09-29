import logging
from datetime import timedelta

import jwt
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import current_user, get_db, get_owned
from ..jobs import start_pdf_import
from ..models import EmailStatement, PDFImportJob, User, UserGmailToken
from ..security import make_token, read_token
from ..services import gmail
from ..services.imports import save_csv_rows, upload_statement
from ..services.parsers import CSV_TEMPLATE, parse_csv

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gmail", tags=["gmail"])

MAX_UPLOAD = 20 * 1024 * 1024


def connected_token(db, user):
    token = db.scalar(select(UserGmailToken).where(UserGmailToken.user_id == user.id, UserGmailToken.is_connected.is_(True)))
    if not token:
        raise HTTPException(400, "Gmail not connected.")
    return token


def frontend(path):
    return RedirectResponse(f"{settings.frontend_url}{path}", status_code=302)


@router.get("/status")
def status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    token = db.scalar(select(UserGmailToken).where(UserGmailToken.user_id == user.id))
    if not token:
        return {"connected": False, "configured": bool(settings.google_client_id)}
    return {"connected": token.is_connected, "connected_since": token.created_at, "token_expiry": token.token_expiry,
            "configured": True}


@router.get("/connect")
def connect(request: Request, user: User = Depends(current_user)):
    if not settings.google_client_id:
        raise HTTPException(400, "Google OAuth is not configured on the server.")
    # Google sends the bare browser back to /oauth/callback, so the user rides along in a signed state.
    state = make_token("gmail", user.id, timedelta(minutes=10))
    return {"auth_url": gmail.auth_url(state, str(request.base_url))}


@router.post("/disconnect")
def disconnect(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(delete(UserGmailToken).where(UserGmailToken.user_id == user.id))
    db.commit()
    return {"message": "Gmail disconnected."}


@router.get("/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    if not code:
        return frontend("/gmail.html?error=oauth_failed")
    try:
        user_id = int(read_token(state, "gmail")["sub"])
    except (jwt.PyJWTError, ValueError):
        return frontend("/gmail.html?error=invalid_state")
    ok = gmail.exchange_code(db, user_id, code, str(request.base_url))
    return frontend("/gmail.html" + ("?connected=1" if ok else "?error=token_exchange"))


@router.post("/statements/import")
def import_statements(user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        imported, skipped = gmail.Gmail(db, connected_token(db, user)).fetch_statements(user.id)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Statement import failed")
        raise HTTPException(502, f"Import failed: {e}")
    return {"message": f"Imported {imported} statements ({skipped} already existed).", "imported": imported,
            "skipped": skipped}


@router.post("/statements/{st_id}/parse")
def parse_statement(st_id: int, pdf_password: str = Body("", embed=True), save_password: bool = Body(False, embed=True),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    st = get_owned(db, EmailStatement, st_id, user)
    client = gmail.Gmail(db, connected_token(db, user))
    pdf_password = pdf_password.strip()
    if save_password and pdf_password:
        st.pdf_password = pdf_password
        db.commit()
    try:
        count = client.parse_pdf_statement(st, pdf_password or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("Parse failed for statement %s", st_id)
        raise HTTPException(502, f"Parse failed: {e}")
    return {"message": f"Extracted {count} transactions.", "count": count}


@router.post("/statements/{st_id}/parse-csv")
def parse_csv_statement(st_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    st = get_owned(db, EmailStatement, st_id, user)
    try:
        imported, skipped = gmail.Gmail(db, connected_token(db, user)).parse_csv_statement(st)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("CSV parse failed for statement %s", st_id)
        raise HTTPException(502, f"CSV parse failed: {e}")
    return {"message": f"Imported {imported} transactions ({skipped} skipped)."}


async def _read(upload: UploadFile, ext: str):
    if not (upload.filename or "").lower().endswith(ext):
        raise HTTPException(400, f"Please upload a {ext} file.")
    data = await upload.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{upload.filename} is larger than 20 MB.")
    return data


@router.post("/upload-csv")
async def upload_csv(csv_file: UploadFile = File(...), create_statement: str = Form(""),
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = parse_csv(await _read(csv_file, ".csv"))
    if not rows:
        raise HTTPException(400, "No valid transactions found in CSV.")
    st_id = None
    if create_statement.lower() in ("on", "true", "1", "yes"):
        st = upload_statement(user.id, f"CSV Import: {csv_file.filename}", "CSV Upload", bank_name="capitec",
                              is_processed=True, state="parsed")
        db.add(st)
        db.flush()
        st_id = st.id
    imported, skipped = save_csv_rows(db, user.id, rows, st_id)
    if st_id:
        st.transaction_count = imported
    db.commit()
    return {"message": f"Imported {imported} transactions ({skipped} skipped).", "imported": imported, "skipped": skipped}


@router.get("/download-csv-template")
def csv_template(user: User = Depends(current_user)):
    return Response(CSV_TEMPLATE, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=transaction_template.csv"})


@router.post("/bulk-csv-import")
async def bulk_csv(csv_files: list[UploadFile] = File(...), user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    total_imported = total_skipped = processed = 0
    errors = []
    for f in csv_files:
        try:
            rows = parse_csv(await _read(f, ".csv"))
            st = upload_statement(user.id, f"CSV Import: {f.filename}", "Bulk CSV Upload", bank_name="capitec",
                                  is_processed=True, state="parsed")
            db.add(st)
            db.flush()
            imported, skipped = save_csv_rows(db, user.id, rows, st.id)
            st.transaction_count = imported
            db.commit()
            total_imported += imported
            total_skipped += skipped
            processed += 1
        except Exception as e:
            db.rollback()
            errors.append(f"{f.filename}: {getattr(e, 'detail', e)}")
    return {"message": f"Processed {processed} files: {total_imported} imported, {total_skipped} skipped.",
            "errors": errors}


@router.post("/upload-pdf", status_code=202)
async def upload_pdf(pdf_files: list[UploadFile] = File(...), bank_name: str = Form("capitec"),
                     pdf_password: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    files = [(await _read(f, ".pdf"), f.filename) for f in pdf_files]
    names = [n for _, n in files]
    job = PDFImportJob(user_id=user.id, filename=", ".join(names)[:255], bank_name=bank_name,
                       pdf_password=pdf_password, status="pending", total_files=len(files))
    db.add(job)
    db.commit()
    start_pdf_import(job.id, files)
    return {"job_id": job.id}


@router.get("/pdf-jobs/{job_id}/status")
def pdf_status(job_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = get_owned(db, PDFImportJob, job_id, user)
    return {
        "status": job.status, "progress": job.progress, "total_files": job.total_files,
        "processed_files": job.processed_files, "transactions_found": job.transactions_found,
        "transactions_saved": job.transactions_saved, "transactions_skipped": job.transactions_skipped,
        "error_message": job.error_message, "statement_id": job.statement_id,
    }
