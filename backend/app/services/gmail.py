import base64
import logging
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import requests
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import BankAccount, BankTransaction, Client, EmailStatement, UserGmailToken, utcnow
from ..security import unseal
from .imports import save_csv_rows, save_pdf_rows
from .parsers import parse_csv, parse_pdf

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SEARCHES = [
    "from:@tymebank.co.za subject:Statement",
    "from:@capitecbank.co.za subject:Statement",
    'subject:"bank statement"',
    "has:attachment filename:csv",
]


def redirect_uri(request_base: str):
    if settings.google_redirect_uri:
        return settings.google_redirect_uri
    base = settings.api_url or request_base.rstrip("/")
    if "localhost" not in base and "127.0.0.1" not in base:
        base = base.replace("http://", "https://", 1)
    return f"{base}/api/gmail/oauth/callback"


def auth_url(state: str, request_base: str):
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(request_base),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


def exchange_code(db: Session, user_id: int, code: str, request_base: str):
    r = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri(request_base),
        "grant_type": "authorization_code",
    }, timeout=20)
    if r.status_code != 200:
        log.error("Gmail token exchange failed: %s %s", r.status_code, r.text[:300])
        return False
    data = r.json()
    token = db.scalar(select(UserGmailToken).where(UserGmailToken.user_id == user_id)) or UserGmailToken(user_id=user_id)
    token.access_token = data.get("access_token", "")
    token.refresh_token = data.get("refresh_token") or token.refresh_token or ""
    token.token_expiry = utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))
    token.is_connected = True
    db.add(token)
    db.commit()
    return True


class Gmail:
    def __init__(self, db: Session, token: UserGmailToken):
        self.db = db
        self.token = token

    def _access_token(self):
        t = self.token
        if t.token_expiry and t.token_expiry > utcnow() + timedelta(seconds=60):
            return t.access_token
        if not t.refresh_token:
            return t.access_token
        r = requests.post(TOKEN_URL, data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": t.refresh_token,
            "grant_type": "refresh_token",
        }, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Gmail token refresh failed ({r.status_code}). Reconnect Gmail.")
        data = r.json()
        t.access_token = data["access_token"]
        t.token_expiry = utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))
        self.db.commit()
        return t.access_token

    def _get(self, path, **params):
        r = requests.get(f"{API}/{path}", headers={"Authorization": f"Bearer {self._access_token()}"},
                         params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def message(self, msg_id):
        return self._get(f"messages/{msg_id}", format="full")

    def attachment(self, msg_id, att_id):
        data = self._get(f"messages/{msg_id}/attachments/{att_id}")["data"]
        return base64.urlsafe_b64decode(data.encode())

    def find_attachment(self, msg_id, extension):
        for part in walk_parts(self.message(msg_id).get("payload", {})):
            if (part.get("filename") or "").lower().endswith(extension):
                if att_id := part.get("body", {}).get("attachmentId"):
                    return self.attachment(msg_id, att_id), part["filename"]
                if data := part.get("body", {}).get("data"):
                    return base64.urlsafe_b64decode(data.encode()), part["filename"]
        return None, None

    def fetch_statements(self, practice_id):
        ids = []
        for q in SEARCHES:
            try:
                for m in self._get("messages", q=q, maxResults=50).get("messages", []):
                    if m["id"] not in ids:
                        ids.append(m["id"])
            except requests.HTTPError as e:
                log.error("Gmail search %r failed: %s", q, e)

        imported = skipped = 0
        for msg_id in ids:
            if self.db.scalar(select(EmailStatement.id).where(EmailStatement.gmail_id == msg_id)):
                skipped += 1
                continue
            try:
                st = self._statement_from(practice_id, self.message(msg_id))
                guess_client(self.db, st)
                self.db.add(st)
                self.db.commit()
                imported += 1
            except Exception as e:
                self.db.rollback()
                log.error("Error importing message %s: %s", msg_id, e)
        return imported, skipped

    def _statement_from(self, practice_id, msg):
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        sender = headers.get("from", "Unknown")
        try:
            received = parsedate_to_datetime(headers.get("date", "")).astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            received = utcnow()

        body_text = ""
        has_attachment = False
        for part in walk_parts(payload):
            name = (part.get("filename") or "").lower()
            if name.endswith((".pdf", ".csv")) or part.get("mimeType") == "application/pdf":
                has_attachment = True
            data = part.get("body", {}).get("data")
            if not data or name:
                continue
            decoded = base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="ignore")
            if part.get("mimeType") == "text/plain":
                body_text = decoded

        low = sender.lower()
        bank = "tymebank" if "tymebank" in low else "capitec" if "capitec" in low else "other"
        return EmailStatement(
            practice_id=practice_id, source="gmail", gmail_id=msg["id"],
            subject=headers.get("subject", "No Subject")[:500], sender=sender[:255], received_date=received,
            bank_name=bank, body_text=body_text[:20000], has_attachment=has_attachment, state="new",
        )

    def parse_pdf_statement(self, st: EmailStatement, password=None):
        self.db.execute(delete(BankTransaction).where(BankTransaction.statement_id == st.id))
        try:
            pdf, _ = self.find_attachment(st.gmail_id, ".pdf")
            if not pdf:
                raise ValueError("No PDF attachment found")
            rows = parse_pdf(pdf, st.bank_name, password or unseal(st.pdf_password) or None)
            saved, _ = save_pdf_rows(self.db, st.client_id, rows, st.id, st.bank_account_id)
            self._mark_parsed(st, saved)
            return saved
        except Exception as e:
            self._mark_error(st, e)
            raise

    def parse_csv_statement(self, st: EmailStatement):
        self.db.execute(delete(BankTransaction).where(BankTransaction.statement_id == st.id))
        try:
            data, _ = self.find_attachment(st.gmail_id, ".csv")
            if not data:
                raise ValueError("No CSV attachment found")
            rows = parse_csv(data)
            if not rows:
                raise ValueError("CSV parsed but no transactions found")
            imported, skipped = save_csv_rows(self.db, st.client_id, rows, st.id, st.bank_account_id)
            self._mark_parsed(st, imported)
            return imported, skipped
        except Exception as e:
            self._mark_error(st, e)
            raise

    def _mark_parsed(self, st, count):
        st.state = "parsed"
        st.processed_date = utcnow()
        st.transaction_count = count
        st.error_message = ""
        self.db.commit()

    def _mark_error(self, st, err):
        self.db.rollback()
        st = self.db.merge(st)
        st.state = "error"
        st.error_message = str(err)
        self.db.commit()


def guess_client(db: Session, st: EmailStatement):
    """Assign a Gmail statement to a client when a known account number appears in the email."""
    text = f"{st.subject} {st.body_text}".replace(" ", "")
    rows = db.execute(select(BankAccount, Client).join(Client, BankAccount.client_id == Client.id)
                      .where(Client.practice_id == st.practice_id, BankAccount.account_number != ""))
    for ba, client in rows:
        digits = ba.account_number.replace(" ", "")
        if len(digits) >= 4 and digits[-4:] in text:
            st.client_id, st.bank_account_id = client.id, ba.id
            if ba.bank_name:
                st.bank_name = ba.bank_name
            return True
    return False


def walk_parts(part):
    yield part
    for child in part.get("parts", []) or []:
        yield from walk_parts(child)
