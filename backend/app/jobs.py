"""Background work. On AWS the API invokes the worker Lambda asynchronously; locally a thread runs it."""
import json
import logging
import threading

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import Client, Job
from .services import categorize, erpnext
from .services.imports import run_pdf_job

log = logging.getLogger(__name__)


def _s3():
    import boto3

    return boto3.client("s3")


def dispatch(event: dict, local_files=None):
    if settings.worker_function:
        import boto3

        boto3.client("lambda").invoke(
            FunctionName=settings.worker_function, InvocationType="Event", Payload=json.dumps(event).encode()
        )
    else:
        threading.Thread(target=handle, args=(event, local_files), daemon=True).start()


def start_pdf_import(job_id: int, files):
    """files: list of (bytes, filename)."""
    if settings.worker_function and settings.uploads_bucket:
        s3 = _s3()
        spec = []
        for i, (data, name) in enumerate(files):
            key = f"pdf-jobs/{job_id}/{i}.pdf"
            s3.put_object(Bucket=settings.uploads_bucket, Key=key, Body=data)
            spec.append({"key": key, "filename": name})
        dispatch({"kind": "pdf_import", "job_id": job_id, "files": spec})
    elif settings.worker_function:
        # No bucket to hand the bytes over with; do it inline.
        db = SessionLocal()
        try:
            run_pdf_job(db, job_id, files)
        finally:
            db.close()
    else:
        dispatch({"kind": "pdf_import", "job_id": job_id}, local_files=files)


def start_job(db, user, client, kind: str, **payload) -> Job:
    job = Job(practice_id=user.practice_id, client_id=client.id, user_id=user.id, kind=kind, status="queued",
              message="Queued.")
    db.add(job)
    db.commit()
    dispatch({"kind": kind, "job_id": job.id, **payload})
    return job


def handle(event: dict, local_files=None):
    kind = event.get("kind")
    if event.get("source") == "aws.events":
        kind = "categorize_all"
    db = SessionLocal()
    try:
        if kind == "pdf_import":
            files = local_files if local_files is not None else _load_s3(event["files"])
            run_pdf_job(db, event["job_id"], files)
        elif kind in ("ai_categorize", "erpnext_sync"):
            _run_tracked(db, event)
        elif kind == "categorize_all":
            _gmail_all(db)
            _categorize_all(db)
            try:
                from .routers.tools import send_monthly

                log.info("Monthly reports sent: %s", send_monthly(db))
            except Exception:
                db.rollback()
                log.exception("Monthly report emails failed")
        else:
            log.error("Unknown job event: %s", event)
    finally:
        db.close()


def _load_s3(spec):
    s3 = _s3()
    files = []
    for f in spec:
        obj = s3.get_object(Bucket=settings.uploads_bucket, Key=f["key"])
        files.append((obj["Body"].read(), f["filename"]))
        s3.delete_object(Bucket=settings.uploads_bucket, Key=f["key"])
    return files


def _run_tracked(db, event):
    job = db.get(Job, event["job_id"])
    if not job:
        return
    job.status, job.message = "in_progress", "Running…"
    db.commit()
    try:
        client = db.get(Client, job.client_id)
        if job.kind == "ai_categorize":
            r = categorize.ai_categorize(db, client)
            job.message = f"{r['keyword'] + r['ai']} of {r['total']} categorized ({r['keyword']} keyword, {r['ai']} AI)."
        else:
            config = erpnext.active_config(db, client.practice_id)
            if not config:
                raise ValueError("No active ERPNext connection.")
            r = erpnext.sync_client(db, config, client)
            job.message = f"Synced {r['synced']}, failed {r['failed']}, skipped {r['skipped']} of {r['total']}."
        job.result = r
        job.conclusion = "failure" if r.get("failed") else "success"
    except Exception as e:
        db.rollback()
        log.exception("Job %s failed", job.id)
        job = db.get(Job, event["job_id"])
        job.conclusion, job.message = "failure", str(e)[:2000]
    job.status = "completed"
    db.commit()


def _gmail_all(db):
    """Nightly: every connected mailbox is searched, and new statements and PayShap notices are read."""
    from .models import User, UserGmailToken
    from .services.gmail import Gmail

    for token, user in db.execute(select(UserGmailToken, User).join(User, User.id == UserGmailToken.user_id)
                                  .where(UserGmailToken.is_connected.is_(True))).all():
        try:
            log.info("Gmail for user %s: %s", user.id, Gmail(db, token).fetch_statements(user.practice_id))
        except Exception:
            db.rollback()
            log.exception("Nightly Gmail read failed for user %s", user.id)


def _categorize_all(db):
    """Nightly schedule: AI categorization if Groq is configured, otherwise keyword-only."""
    for client in db.scalars(select(Client).where(Client.is_active.is_(True))).all():
        try:
            if settings.groq_api_keys:
                categorize.ai_categorize(db, client)
            else:
                categorize.auto_categorize(db, client)
        except Exception:
            db.rollback()
            log.exception("Scheduled categorize failed for client %s", client.id)

