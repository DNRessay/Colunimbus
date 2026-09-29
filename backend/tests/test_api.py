import os
import time
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///./test_lsuite.db"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["GROQ_API_KEYS"] = ""

if os.path.exists("test_lsuite.db"):
    os.remove("test_lsuite.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.security import hash_password, verify_password  # noqa: E402
from app.services.parsers.capitec import CapitecParser  # noqa: E402
from app.services.parsers.csv_parser import parse_csv  # noqa: E402
from app.services.parsers.tymebank import TymeBankLegacyParser  # noqa: E402

client = TestClient(app)

CSV = (
    "Transaction Date,Posting Date,Description,Debits,Credits,Balance,Bank account\n"
    "2025/09/23,2025/09/23,Salary ACME,,1000.00,5000.00,Capitec Savings\n"
    "2025/09/24,2025/09/24,Checkers Sandton,500.00,,4500.00,Capitec Savings\n"
    "2025/09/25,2025/09/25,Zzzq Unknown Merchant,20.00,,4480.00,Capitec Savings\n"
)


@pytest.fixture(scope="module")
def auth():
    r = client.post("/api/authusers/register", json={
        "first_name": "Le", "last_name": "Roy", "email": "LR@example.com", "username": "leroy",
        "password": "Str0ng-pass!", "city": "Pretoria",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["email"] == "lr@example.com"
    return {"Authorization": f"Bearer {body['access']}"}, body["refresh"]


def test_django_hash_compat():
    # hash produced by Django's PBKDF2PasswordHasher for "hunter22" with salt "abc", 1000 iterations
    django_hash = "pbkdf2_sha256$1000$abc$" + __import__("base64").b64encode(
        __import__("hashlib").pbkdf2_hmac("sha256", b"hunter22", b"abc", 1000)).decode()
    assert verify_password("hunter22", django_hash)
    assert not verify_password("wrong", django_hash)
    assert verify_password("x-pass-123", hash_password("x-pass-123"))


def test_health():
    assert client.get("/api/health").json() == {"status": "ok", "database": True}


def test_auth_flow(auth):
    headers, refresh = auth
    assert client.get("/api/authusers/me/", headers=headers).json()["username"] == "leroy"
    assert client.get("/api/authusers/me").status_code == 401
    r = client.post("/api/authusers/login", json={"username": "lr@example.com", "password": "Str0ng-pass!"})
    assert r.status_code == 200
    assert client.post("/api/authusers/login", json={"username": "leroy", "password": "nope"}).status_code == 401
    assert "access" in client.post("/api/token/refresh", json={"refresh": refresh}).json()
    dup = client.post("/api/authusers/register", json={
        "first_name": "a", "last_name": "b", "email": "lr@example.com", "username": "leroy", "password": "123"})
    assert dup.status_code == 400 and {"email", "username", "password"} <= set(dup.json()["detail"])


def test_profile_and_links(auth):
    headers, _ = auth
    assert client.get("/api/authusers/profile", headers=headers).json()["city"] == "Pretoria"
    r = client.patch("/api/authusers/profile", headers=headers, json={"occupation": "Dev", "date_of_birth": ""})
    assert r.json()["occupation"] == "Dev"
    link = client.post("/api/authusers/links", headers=headers, json={"platform": "GitHub", "url": "github.com/x"}).json()
    assert link["url"] == "https://github.com/x" and link["icon"] == "🐙"
    assert client.delete(f"/api/authusers/links/{link['id']}", headers=headers).status_code == 204


def test_password_reset(auth, monkeypatch):
    sent = {}
    monkeypatch.setattr("app.routers.auth.send_mail", lambda to, s, body: sent.update(body=body))
    client.post("/api/authusers/password-reset", json={"email": "lr@example.com"})
    token = sent["body"].split("token=")[1].split()[0]
    r = client.post("/api/authusers/password-reset/confirm", json={"token": token, "new_password": "N3w-pass-word"})
    assert r.status_code == 200
    # token is single-use: password hash changed
    r = client.post("/api/authusers/password-reset/confirm", json={"token": token, "new_password": "An0ther-pass"})
    assert r.status_code == 400
    r = client.post("/api/authusers/login", json={"username": "leroy", "password": "N3w-pass-word"})
    assert r.status_code == 200


def test_csv_import_categorize_dashboard(auth):
    headers, _ = auth
    client.post("/api/categories", headers=headers, json={"name": "Groceries", "transaction_type": "debit",
                                                          "keywords": "checkers"})
    r = client.post("/api/gmail/upload-csv", headers=headers, files={"csv_file": ("s.csv", CSV, "text/csv")},
                    data={"create_statement": "on"})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 3
    # re-upload is deduplicated
    assert client.post("/api/gmail/upload-csv", headers=headers,
                       files={"csv_file": ("s.csv", CSV, "text/csv")}).json()["skipped"] == 3

    preview = client.post("/api/bridge/bulk-operations/preview-categorization", headers=headers).json()
    assert preview["total_uncategorized"] == 3 and preview["will_be_categorized"] == 2

    r = client.post("/api/bridge/bulk-operations/auto-categorize", headers=headers).json()
    assert r == {"categorized": 2, "total": 3}

    txns = client.get("/api/transactions", headers=headers).json()
    by_desc = {t["description"]: t for t in txns}
    assert by_desc["Checkers Sandton"]["category_name"] == "Groceries"
    assert by_desc["Salary ACME"]["category_name"] == "Income"
    assert by_desc["Salary ACME"]["deposit"] == "1000.00"

    uncategorized = client.get("/api/transactions?uncategorized=1", headers=headers).json()
    assert [t["description"] for t in uncategorized] == ["Zzzq Unknown Merchant"]

    cats = client.get("/api/categories", headers=headers).json()
    groceries = next(c for c in cats if c["name"] == "Groceries")
    tid = uncategorized[0]["id"]
    assert client.post(f"/api/bridge/transactions/{tid}/categorize", headers=headers,
                       json={"category_id": groceries["id"]}).status_code == 200
    assert "zzzq" in client.get(f"/api/categories/{groceries['id']}", headers=headers).json()["tags"]

    stats = client.get("/api/bridge/categories", headers=headers).json()["categories"]
    assert next(s for s in stats if s["category"]["name"] == "Groceries")["total"] == 2

    d = client.get("/api/dashboard", headers=headers).json()
    assert d["stats"]["transactions"] == 3 and d["stats"]["categorized"] == 3 and d["stats"]["statements"] == 1


def test_invoices(auth):
    headers, _ = auth
    r = client.post("/api/invoices", headers=headers, json={
        "invoice_number": "INV-1", "invoice_date": "2025-09-01", "customer_name": "ACME", "tax_rate": "15",
        "items": [{"description": "Work", "quantity": "2", "unit_price": "100"}],
    })
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["subtotal"] == "200.00" and inv["tax_amount"] == "30.00" and inv["total_amount"] == "230.00"
    inv = client.patch(f"/api/invoices/{inv['id']}", headers=headers, json={"paid_amount": "230"}).json()
    assert inv["is_paid"] is True


def test_erpnext_config_secrets_hidden(auth):
    headers, _ = auth
    cfg = client.post("/api/erpnext-configs", headers=headers, json={
        "name": "Main", "base_url": "https://erp.example.com", "api_key": "k", "api_secret": "s"}).json()
    assert "api_key" not in cfg and cfg["is_active"] is True
    pre = client.get("/api/erpnext/sync-preflight", headers=headers).json()
    assert pre["ready_count"] == 0 and len(pre["missing_categories"]) == 2


def test_reconciliation(auth):
    headers, _ = auth
    d = client.get("/api/reconciliation/month/2025/9", headers=headers).json()
    assert d["period"]["total_transactions"] == 3 and d["period"]["unreconciled_count"] == 3
    r = client.post("/api/reconciliation/month/2025/9/match", headers=headers).json()
    assert r["flagged"] == 3
    assert client.post("/api/reconciliation/month/2025/9/close", headers=headers).status_code == 400
    csv_out = client.get("/api/reconciliation/month/2025/9/export", headers=headers).text
    assert "No journal entries found" in csv_out
    tid = d["transactions"][0]["id"]
    client.post(f"/api/reconciliation/transactions/{tid}/unmatch", headers=headers)
    assert client.get(f"/api/transactions/{tid}", headers=headers).json()["recon_status"] == "unreconciled"


def test_pdf_upload_job(auth):
    headers, _ = auth
    from reportlab.pdfgen import canvas
    import io

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(50, 800, "Transaction History")
    c.drawString(50, 780, "01/10/2025 Payment Received ACME Income 250.00 750.00")
    c.drawString(50, 760, "02/10/2025 Spar Menlyn Groceries -120.50 629.50")
    c.save()
    r = client.post("/api/gmail/upload-pdf", headers=headers, data={"bank_name": "capitec"},
                    files=[("pdf_files", ("stmt.pdf", buf.getvalue(), "application/pdf"))])
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    for _ in range(50):
        s = client.get(f"/api/gmail/pdf-jobs/{job_id}/status", headers=headers).json()
        if s["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert s["status"] == "done", s
    assert s["transactions_saved"] == 2


def test_other_users_cannot_see_data(auth):
    r = client.post("/api/authusers/register", json={
        "first_name": "Eve", "last_name": "X", "email": "eve@example.com", "username": "eve", "password": "Ev3-pass-word"})
    other = {"Authorization": f"Bearer {r.json()['access']}"}
    assert client.get("/api/transactions", headers=other).json() == []
    assert client.get("/api/transactions/1", headers=other).status_code == 404


def test_parsers():
    text = ("Transaction History\n"
            "01/09/2025 Payment Received J Smith Other Income 1,500.00 2,500.00\n"
            "03/09/2025 Checkers Sandton Fees -250.00 -1.00 2,249.00\n")
    rows = CapitecParser().parse(text)
    assert [(r["type"], r["amount"], r["category"]) for r in rows] == [("credit", 1500.0, "Other Income"),
                                                                       ("debit", 250.0, "Sandton Fees")]
    assert rows[1]["fee"] == 1.0 and rows[0]["date"] == date(2025, 9, 1)

    tyme = TymeBankLegacyParser().parse("05 Sep 2025 Woolworths Food\n- 99.90 - 400.10\n06 Sep 2025 Salary - - 5,000.00 5,400.10\n")
    assert [(r["type"], r["amount"]) for r in tyme] == [("debit", 99.9), ("credit", 5000.0)]

    rows = parse_csv(CSV)
    assert rows[1]["debits"] == 500 and rows[0]["credits"] == 1000 and rows[0]["reference"] == "Salary-20250923"


def test_ai_categorize_with_mocked_groq(auth, monkeypatch):
    headers, _ = auth
    from app.db import SessionLocal
    from app.services import categorize

    client.post("/api/transactions", headers=headers, json={
        "date": "2025-10-05", "description": "QWERTY Streaming Co", "withdrawal": "99", "transaction_type": "debit"})

    def fake(system, user):
        assert "QWERTY Streaming Co" in user
        return {"results": [{"i": 0, "category": "Entertainment", "confidence": 0.9, "keyword": "qwerty"}]}

    monkeypatch.setattr(categorize, "groq_json", fake)
    client.post("/api/categories", headers=headers, json={"name": "Entertainment", "transaction_type": "debit"})
    db = SessionLocal()
    user_id = client.get("/api/authusers/me", headers=headers).json()["id"]
    r = categorize.ai_categorize(db, user_id)
    db.close()
    assert r["ai"] == 1
    ent = next(c for c in client.get("/api/categories", headers=headers).json() if c["name"] == "Entertainment")
    assert "qwerty" in ent["keywords"]


def test_erpnext_journal_entry_mocked(auth, monkeypatch):
    headers, _ = auth
    import app.services.erpnext as erp

    class Resp:
        def __init__(self, data, status=200):
            self._d, self.status_code, self.text, self.ok = data, status, "", status < 400

        def json(self):
            return self._d

        def raise_for_status(self):
            pass

    posted = []
    monkeypatch.setattr(erp.requests, "get", lambda url, **kw: Resp({"data": [{"name": "Test Co", "abbr": "TC"}]}))
    monkeypatch.setattr(erp.requests, "post", lambda url, json=None, **kw: posted.append(json) or Resp({"data": {"name": "JV-0001"}}))

    cats = client.get("/api/categories", headers=headers).json()
    groceries = next(c for c in cats if c["name"] == "Groceries")
    client.patch(f"/api/categories/{groceries['id']}", headers=headers, json={"erpnext_account": "Groceries - TC"})
    cfg = client.get("/api/erpnext-configs", headers=headers).json()[0]
    client.patch(f"/api/erpnext-configs/{cfg['id']}", headers=headers,
                 json={"default_company": "TC", "bank_account": "Bank - TC"})
    txn = next(t for t in client.get("/api/transactions", headers=headers).json() if t["description"] == "Checkers Sandton")
    r = client.post(f"/api/erpnext/transactions/{txn['id']}/sync", headers=headers)
    assert r.status_code == 200, r.text
    je = posted[-1]
    assert je["company"] == "Test Co"
    bank_row, exp_row = je["accounts"]
    assert bank_row["account"] == "Bank - TC" and bank_row["credit_in_account_currency"] == 500.0
    assert exp_row["debit_in_account_currency"] == 500.0
    assert client.get(f"/api/transactions/{txn['id']}", headers=headers).json()["erpnext_journal_entry"] == "JV-0001"
    logs = client.get("/api/erpnext-sync-logs", headers=headers).json()
    assert logs[0]["status"] == "success"
