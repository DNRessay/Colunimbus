import base64
import logging
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import requests
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    BankAccount, BankTransaction, Client, EmailStatement, PayShapNotice, StatementPassword, UserGmailToken, utcnow,
)
from ..security import unseal
from . import payshap, yoco
from .imports import save_csv_rows, save_pdf_rows
from .parsers import parse_csv, parse_pdf

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# Sender domain -> bank key. Capitec, TymeBank and GoTyme have their own parsers; the rest use the generic one.
BANKS = {
    "capitecbank.co.za": "capitec", "tymebank.co.za": "tymebank", "gotyme.co.za": "gotyme", "fnb.co.za": "fnb",
    "absa.co.za": "absa", "standardbank.co.za": "standardbank", "nedbank.co.za": "nedbank", "discovery.co.za": "discovery",
    "investec.co.za": "investec", "investec.com": "investec", "africanbank.co.za": "africanbank", "bankzero.co.za": "bankzero",
}
SEARCHES = [
    "has:attachment filename:pdf (statement OR statements) from:(" + " OR ".join(BANKS) + ")",
    'subject:"bank statement" has:attachment',
    "from:yoco has:attachment filename:csv",
    "has:attachment filename:csv statement",
]


def bank_for(sender: str, text: str = "") -> str:
    low = sender.lower()
    if "yoco" in low:
        return "yoco"
    for domain, key in BANKS.items():
        if domain in low:
            return key
    body = text.lower()
    return next((key for key in dict.fromkeys(BANKS.values()) if key in body), "other")


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

    def search(self, q, limit=50):
        try:
            return [m["id"] for m in self._get("messages", q=q, maxResults=limit).get("messages", [])]
        except requests.HTTPError as e:
            log.error("Gmail search %r failed: %s", q, e)
            return []

    def fetch_statements(self, practice_id):
        """New statement emails into the inbox, every readable one read straight away, plus PayShap notices."""
        ids = list(dict.fromkeys(i for q in SEARCHES for i in self.search(q)))
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
        read = self.auto_read(practice_id)
        notices = self.fetch_payshap(practice_id)
        return {"imported": imported, "skipped": skipped, **read, "payshap": notices}

    def passwords(self, st: EmailStatement):
        saved = [unseal(p.secret) for p in self.db.scalars(
            select(StatementPassword).where(StatementPassword.practice_id == st.practice_id))]
        return [p for p in (unseal(st.pdf_password), *saved) if p]

    def auto_read(self, practice_id):
        """Read every assigned statement that hasn't been read yet. Wrong/missing password -> 'locked'."""
        from . import categorize

        parsed = locked = failed = rows = 0
        touched = set()
        for st in list(self.db.scalars(select(EmailStatement).where(
                EmailStatement.practice_id == practice_id, EmailStatement.source == "gmail",
                EmailStatement.client_id.is_not(None), EmailStatement.has_attachment.is_(True),
                EmailStatement.state.in_(("new", "locked"))))):
            try:
                if st.bank_name == "yoco":
                    count = self.parse_yoco_statement(st)
                else:
                    try:
                        count = self.parse_pdf_statement(st)
                    except ValueError as e:
                        if "No PDF attachment" not in str(e):
                            raise
                        count, _ = self.parse_csv_statement(st)
                parsed += 1
                rows += count
                touched.add(st.client_id)
            except ValueError as e:
                st = self.db.merge(st)
                if "password" in str(e).lower():
                    st.state, st.error_message = "locked", "Needs the statement password: add it under Import > Statement passwords."
                    locked += 1
                else:
                    failed += 1
                self.db.commit()
            except Exception as e:
                log.error("Auto-read of statement %s failed: %s", st.id, e)
                failed += 1
        for cid in touched:
            client = self.db.get(Client, cid)
            if client:
                categorize.auto_categorize(self.db, client)
        payshap.match(self.db, practice_id)
        return {"parsed": parsed, "locked": locked, "failed": failed, "transactions": rows}

    def fetch_payshap(self, practice_id):
        new = 0
        for msg_id in self.search(payshap.QUERY, 100):
            if self.db.scalar(select(PayShapNotice.id).where(PayShapNotice.gmail_id == msg_id)):
                continue
            try:
                msg = self.message(msg_id)
                headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
                body = message_text(msg.get("payload", {}))
                info = payshap.parse_notice(headers.get("subject", ""), body)
                if not info:
                    continue
                try:
                    when = parsedate_to_datetime(headers.get("date", "")).astimezone(timezone.utc).replace(tzinfo=None)
                except (TypeError, ValueError):
                    when = utcnow()
                n = PayShapNotice(practice_id=practice_id, gmail_id=msg_id, received_at=when,
                                  subject=headers.get("subject", "")[:500], **info)
                n.client_id = client_for_text(self.db, practice_id, f"{n.subject} {body}")
                self.db.add(n)
                self.db.commit()
                new += 1
            except Exception as e:
                self.db.rollback()
                log.error("PayShap notice %s failed: %s", msg_id, e)
        payshap.match(self.db, practice_id)
        return new

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

        bank = bank_for(sender, f"{headers.get('subject', '')} {body_text[:2000]}")
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
            rows = parse_pdf(pdf, st.bank_name, password, self.passwords(st))
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

    def parse_yoco_statement(self, st: EmailStatement):
        data, _ = self.find_attachment(st.gmail_id, ".csv")
        if not data:
            raise ValueError("No CSV attachment found")
        saved, _ = yoco.import_rows(self.db, self.db.get(Client, st.client_id), yoco.parse_csv(data), st.id)
        self._mark_parsed(st, saved)
        return saved

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


def client_for_text(db: Session, practice_id: int, text: str):
    """The company whose bank account number (last 4 digits) appears in the text; else the only company."""
    flat = (text or "").replace(" ", "")
    rows = db.execute(select(BankAccount, Client).join(Client, BankAccount.client_id == Client.id)
                      .where(Client.practice_id == practice_id, BankAccount.account_number != ""))
    for ba, client in rows:
        digits = ba.account_number.replace(" ", "")
        if len(digits) >= 4 and digits[-4:] in flat:
            return client.id
    only = list(db.scalars(select(Client.id).where(Client.practice_id == practice_id, Client.is_active.is_(True)).limit(2)))
    return only[0] if len(only) == 1 else None


def guess_client(db: Session, st: EmailStatement):
    """Assign a Gmail statement to a client when a known account number appears in the email (or there's one client)."""
    text = f"{st.subject} {st.body_text}".replace(" ", "")
    rows = db.execute(select(BankAccount, Client).join(Client, BankAccount.client_id == Client.id)
                      .where(Client.practice_id == st.practice_id, BankAccount.account_number != ""))
    for ba, client in rows:
        digits = ba.account_number.replace(" ", "")
        if len(digits) >= 4 and digits[-4:] in text:
            st.client_id, st.bank_account_id = client.id, ba.id
            if ba.bank_name and st.bank_name != "yoco":
                st.bank_name = ba.bank_name
            return True
    st.client_id = client_for_text(db, st.practice_id, "")
    return bool(st.client_id)


def message_text(payload):
    """Plain text of an email (HTML stripped when there's no text part)."""
    import html
    import re

    plain = htm = ""
    for part in walk_parts(payload):
        data = part.get("body", {}).get("data")
        if not data or part.get("filename"):
            continue
        decoded = base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="ignore")
        if part.get("mimeType") == "text/plain" and not plain:
            plain = decoded
        elif part.get("mimeType") == "text/html" and not htm:
            htm = decoded
    if plain:
        return plain[:20000]
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", htm, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))[:20000]


def walk_parts(part):
    yield part
    for child in part.get("parts", []) or []:
        yield from walk_parts(child)
