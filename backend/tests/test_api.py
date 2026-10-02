import base64
import hashlib
import io
import json
import os
import time
from datetime import date
from decimal import Decimal

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.security import hash_password, unseal, verify_password  # noqa: E402
from app.services.parsers.capitec import CapitecParser  # noqa: E402
from app.services.parsers.csv_parser import parse_csv  # noqa: E402
from app.services.parsers.tymebank import TymeBankLegacyParser  # noqa: E402

api = TestClient(app)

SALES_CSV = (
    "Transaction Date,Posting Date,Description,Debits,Credits,Balance,Bank account\n"
    "2025/09/23,2025/09/23,Payment received Customer ACME,,12000.00,20000.00,FNB\n"
    "2025/09/24,2025/09/24,Checkers Sandton,500.00,,19500.00,FNB\n"
    "2025/09/25,2025/09/25,Transfer to Building Co,5000.00,,14500.00,FNB\n"
)
BUILD_CSV = (
    "Transaction Date,Posting Date,Description,Debits,Credits,Balance,Bank account\n"
    "2025/09/26,2025/09/26,Transfer from Sales Co,,5000.00,9000.00,Capitec\n"
    "2025/09/27,2025/09/27,Zzqx Timber Yard,3200.00,,5800.00,Capitec\n"
)


def upload(headers, csv_text, name="s.csv", **form):
    return api.post("/api/imports/csv", headers=headers, files=[("csv_files", (name, csv_text, "text/csv"))], data=form)


@pytest.fixture(scope="module")
def org():
    r = api.post("/api/auth/register", json={
        "practice_name": "Mokoena Group", "first_name": "Charlie", "last_name": "M", "email": "CM@example.com",
        "username": "charlie", "password": "Str0ng-pass!",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["role"] == "owner"
    auth = {"Authorization": f"Bearer {body['access']}"}
    sales = api.post("/api/clients", headers=auth, json={"name": "Sales Co", "vat_registered": True}).json()
    build = api.post("/api/clients", headers=auth, json={"name": "Building Co"}).json()
    return {
        "auth": auth, "refresh": body["refresh"],
        "sales": {**auth, "X-Client-Id": str(sales["id"])}, "sales_id": sales["id"],
        "build": {**auth, "X-Client-Id": str(build["id"])}, "build_id": build["id"],
    }


def test_django_hash_compat():
    django_hash = "pbkdf2_sha256$1000$abc$" + base64.b64encode(hashlib.pbkdf2_hmac("sha256", b"hunter22", b"abc", 1000)).decode()
    assert verify_password("hunter22", django_hash)
    assert not verify_password("wrong", django_hash)
    assert verify_password("x-pass-123", hash_password("x-pass-123"))


def test_health():
    assert api.get("/api/health").json() == {"status": "ok", "database": True}


def test_auth_and_team(org):
    assert api.get("/api/auth/me/", headers=org["auth"]).json()["username"] == "charlie"
    assert api.get("/api/auth/me").status_code == 401
    assert api.post("/api/auth/login", json={"username": "cm@example.com", "password": "Str0ng-pass!"}).status_code == 200
    assert api.post("/api/auth/login", json={"username": "charlie", "password": "nope"}).status_code == 401
    assert "access" in api.post("/api/auth/token/refresh", json={"refresh": org["refresh"]}).json()
    assert api.get("/api/practice", headers=org["auth"]).json()["name"] == "Mokoena Group"

    r = api.post("/api/practice/users", headers=org["auth"], json={
        "first_name": "Tli", "email": "tli@example.com", "username": "tli", "password": "B00kkeep-pass"})
    assert r.status_code == 201 and r.json()["role"] == "bookkeeper"
    member = api.post("/api/auth/login", json={"username": "tli", "password": "B00kkeep-pass"}).json()
    mh = {"Authorization": f"Bearer {member['access']}"}
    assert len(api.get("/api/clients", headers=mh).json()) == 2  # shares the organisation's companies
    assert api.post("/api/practice/users", headers=mh, json={
        "first_name": "x", "email": "x@example.com", "username": "x", "password": "Xx-pass-word1"}).status_code == 403
    assert len(api.get("/api/practice/users", headers=org["auth"]).json()) == 2


def test_default_categories_seeded(org):
    names = {c["name"] for c in api.get("/api/categories", headers=org["auth"]).json()}
    assert {"Groceries", "Income", "Intercompany Transfer"} <= names


def test_client_header_required(org):
    assert api.get("/api/transactions", headers=org["auth"]).status_code == 400
    assert api.get("/api/transactions", headers={**org["auth"], "X-Client-Id": "999"}).status_code == 404


def test_password_reset(org, monkeypatch):
    sent = {}
    monkeypatch.setattr("app.routers.auth.send_mail", lambda to, s, body: sent.update(body=body))
    api.post("/api/auth/password-reset", json={"email": "cm@example.com"})
    token = sent["body"].split("token=")[1].split()[0]
    assert api.post("/api/auth/password-reset/confirm", json={"token": token, "new_password": "N3w-pass-word"}).status_code == 200
    assert api.post("/api/auth/password-reset/confirm", json={"token": token, "new_password": "An0ther-pass"}).status_code == 400
    assert api.post("/api/auth/login", json={"username": "charlie", "password": "N3w-pass-word"}).status_code == 200


def test_csv_import_and_categorize(org):
    ba = api.post("/api/accounts", headers=org["sales"], json={"account_name": "FNB Cheque", "bank_name": "fnb",
                                                               "account_number": "62001234567"}).json()
    r = upload(org["sales"], SALES_CSV, bank_account_id=str(ba["id"]))
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 3
    assert upload(org["sales"], SALES_CSV).json()["skipped"] == 3  # deduplicated
    assert upload(org["build"], BUILD_CSV).json()["imported"] == 2

    r = api.post("/api/bridge/bulk-operations/auto-categorize", headers=org["sales"]).json()
    assert r == {"categorized": 3, "total": 3}  # income, groceries, "transfer to" -> Transfer Out
    txns = {t["description"]: t for t in api.get("/api/transactions", headers=org["sales"]).json()}
    assert txns["Checkers Sandton"]["category_name"] == "Groceries"
    assert txns["Payment received Customer ACME"]["category_name"] == "Income"
    assert txns["Checkers Sandton"]["bank_account"] == ba["id"]
    assert api.get("/api/transactions?uncategorized=1", headers=org["sales"]).json() == []

    unknown = api.get("/api/transactions?q=Zzqx", headers=org["build"]).json()[0]
    cats = {c["name"]: c for c in api.get("/api/categories", headers=org["auth"]).json()}
    api.post("/api/categories", headers=org["auth"], json={"name": "Materials", "transaction_type": "debit"})
    mat = next(c for c in api.get("/api/categories", headers=org["auth"]).json() if c["name"] == "Materials")
    assert api.post(f"/api/bridge/transactions/{unknown['id']}/categorize", headers=org["build"],
                    json={"category_id": mat["id"]}).status_code == 200
    assert "zzqx" in next(c for c in api.get("/api/categories", headers=org["auth"]).json() if c["name"] == "Materials")["tags"]
    assert cats["Groceries"]["erpnext_account"] == ""


def test_companies_are_isolated(org):
    sales_ids = {t["id"] for t in api.get("/api/transactions", headers=org["sales"]).json()}
    build_ids = {t["id"] for t in api.get("/api/transactions", headers=org["build"]).json()}
    assert sales_ids and build_ids and not sales_ids & build_ids
    assert api.get(f"/api/transactions/{next(iter(sales_ids))}", headers=org["build"]).status_code == 404

    other = api.post("/api/auth/register", json={
        "first_name": "Eve", "last_name": "X", "email": "eve@example.com", "username": "eve", "password": "Ev3-pass-word"}).json()
    eh = {"Authorization": f"Bearer {other['access']}"}
    assert api.get("/api/clients", headers=eh).json() == []
    assert api.get("/api/transactions", headers={**eh, "X-Client-Id": str(org["sales_id"])}).status_code == 404


def test_per_company_erpnext_accounts(org):
    groceries = next(c for c in api.get("/api/categories", headers=org["auth"]).json() if c["name"] == "Groceries")
    api.patch(f"/api/categories/{groceries['id']}", headers=org["sales"], json={"erpnext_account": "Groceries - SC"})
    api.patch(f"/api/categories/{groceries['id']}", headers=org["build"], json={"erpnext_account": "Staff Food - BC"})
    get = lambda h: next(c for c in api.get("/api/categories", headers=h).json() if c["id"] == groceries["id"])
    assert get(org["sales"])["erpnext_account"] == "Groceries - SC"
    assert get(org["build"])["erpnext_account"] == "Staff Food - BC"
    assert api.patch(f"/api/categories/{groceries['id']}", headers=org["auth"],
                     json={"erpnext_account": "x"}).status_code == 400


def test_intercompany(org):
    pairs = api.get("/api/intercompany", headers=org["auth"]).json()["pairs"]
    assert len(pairs) == 1
    p = pairs[0]
    assert p["amount"] == "5000.00" and p["out"]["company"] == "Sales Co" and p["in"]["company"] == "Building Co"
    r = api.post("/api/intercompany/confirm", headers=org["auth"], json={"out_id": p["out"]["id"], "in_id": p["in"]["id"]})
    assert r.status_code == 200
    assert api.get("/api/intercompany", headers=org["auth"]).json()["pairs"] == []
    t = api.get(f"/api/transactions/{p['in']['id']}", headers=org["build"]).json()
    assert t["category_name"] == "Intercompany Transfer"


def test_dashboard(org):
    d = api.get("/api/dashboard", headers=org["auth"]).json()
    rows = {r["client"]["name"]: r for r in d["companies"]}
    assert rows["Sales Co"]["transactions"] == 3 and rows["Building Co"]["uncategorized"] == 0


def test_statement_inbox_assignment(org):
    from app.db import SessionLocal
    from app.models import EmailStatement
    from app.services.gmail import guess_client

    db = SessionLocal()
    practice_id = api.get("/api/practice", headers=org["auth"]).json()["id"]
    st = EmailStatement(practice_id=practice_id, source="gmail", gmail_id="g-1", subject="Your FNB statement",
                        body_text="Account ending ...4567 statement attached")
    assert guess_client(db, st) and st.client_id == org["sales_id"]
    st2 = EmailStatement(practice_id=practice_id, source="gmail", gmail_id="g-2", subject="Statement", body_text="")
    assert not guess_client(db, st2)
    db.add(st2)
    db.commit()
    db.close()
    inbox = api.get("/api/statements?unassigned=1", headers=org["auth"]).json()
    assert [s["gmail_id"] for s in inbox] == ["g-2"]
    r = api.patch(f"/api/statements/{inbox[0]['id']}", headers=org["auth"], json={"client_id": org["build_id"]})
    assert r.json()["client_id"] == org["build_id"]
    assert api.get("/api/statements?unassigned=1", headers=org["auth"]).json() == []


def test_erpnext_config_secret_sealed(org):
    cfg = api.post("/api/erpnext-configs", headers=org["auth"], json={
        "name": "Group ERP", "base_url": "https://erp.example.com", "api_key": "k", "api_secret": "s"}).json()
    assert "api_secret" not in cfg and cfg["is_active"] is True
    from app.db import SessionLocal
    from app.models import ERPNextConfig

    db = SessionLocal()
    row = db.get(ERPNextConfig, cfg["id"])
    assert row.api_secret.startswith("enc:") and unseal(row.api_secret) == "s"
    db.close()
    pre = api.get("/api/erpnext/sync-preflight", headers=org["sales"]).json()
    assert pre["company_set"] is False and pre["pending_count"] == 3


def test_erpnext_journal_entry_mocked(org, monkeypatch):
    import app.services.erpnext as erp

    class Resp:
        def __init__(self, data, status=200):
            self._d, self.status_code, self.text, self.ok = data, status, "", status < 400

        def json(self):
            return self._d

        def raise_for_status(self):
            pass

    posted, got = [], []
    monkeypatch.setattr(erp.requests, "get", lambda url, **kw: got.append(kw.get("params")) or Resp({"data": [{"name": "Sales Co (Pty) Ltd", "abbr": "SC"}]}))
    monkeypatch.setattr(erp.requests, "post", lambda url, json=None, **kw: posted.append(json) or Resp({"data": {"name": "ACC-JV-0001"}}))

    api.patch(f"/api/clients/{org['sales_id']}", headers=org["auth"], json={"erpnext_company": "SC"})
    accts = api.get("/api/accounts", headers=org["sales"]).json()
    api.patch(f"/api/accounts/{accts[0]['id']}", headers=org["sales"], json={"erpnext_account": "FNB Cheque - SC"})
    txn = next(t for t in api.get("/api/transactions", headers=org["sales"]).json() if t["description"] == "Checkers Sandton")
    r = api.post(f"/api/erpnext/transactions/{txn['id']}/sync", headers=org["sales"])
    assert r.status_code == 200, r.text
    je = posted[-1]
    assert je["company"] == "Sales Co (Pty) Ltd" and je["voucher_type"] == "Bank Entry"
    bank_row, exp_row = je["accounts"]
    assert bank_row["account"] == "FNB Cheque - SC" and bank_row["credit_in_account_currency"] == 500.0
    assert exp_row["account"] == "Groceries - SC" and exp_row["debit_in_account_currency"] == 500.0
    assert api.get(f"/api/transactions/{txn['id']}", headers=org["sales"]).json()["erpnext_journal_entry"] == "ACC-JV-0001"
    assert api.get("/api/erpnext-sync-logs", headers=org["sales"]).json()[0]["status"] == "success"


def test_ai_categorize_with_mocked_groq(org, monkeypatch):
    from app.db import SessionLocal
    from app.models import Client
    from app.services import categorize

    api.post("/api/transactions", headers=org["build"], json={
        "date": "2025-10-05", "description": "QWERTY Plank Suppliers", "withdrawal": "990"})
    monkeypatch.setattr(categorize, "groq_json", lambda s, u: {"results": [
        {"i": 0, "category": "Materials", "confidence": 0.9, "keyword": "qwerty"}]})
    db = SessionLocal()
    r = categorize.ai_categorize(db, db.get(Client, org["build_id"]))
    db.close()
    assert r["ai"] == 1
    mat = next(c for c in api.get("/api/categories", headers=org["auth"]).json() if c["name"] == "Materials")
    assert "qwerty" in mat["keywords"]


def test_reconciliation(org):
    d = api.get("/api/reconciliation/month/2025/9", headers=org["build"]).json()
    assert d["period"]["total_transactions"] == 2
    assert api.post("/api/reconciliation/month/2025/9/match", headers=org["build"]).json()["flagged"] == 2
    assert api.post("/api/reconciliation/month/2025/9/close", headers=org["build"]).status_code == 400
    assert "No journal entries found" in api.get("/api/reconciliation/month/2025/9/export", headers=org["build"]).text
    tid = d["transactions"][0]["id"]
    api.post(f"/api/reconciliation/transactions/{tid}/unmatch", headers=org["build"])
    assert api.get(f"/api/transactions/{tid}", headers=org["build"]).json()["recon_status"] == "unreconciled"


def test_pdf_upload_job(org):
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(50, 800, "Transaction History")
    c.drawString(50, 780, "01/10/2025 Payment Received ACME Other Income 250.00 750.00")
    c.drawString(50, 760, "02/10/2025 Spar Menlyn Groceries -120.50 629.50")
    c.save()
    r = api.post("/api/imports/pdf", headers=org["build"], data={"bank_name": "capitec", "pdf_password": "pw"},
                 files=[("pdf_files", ("stmt.pdf", buf.getvalue(), "application/pdf"))])
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    for _ in range(50):
        s = api.get(f"/api/imports/pdf/{job_id}", headers=org["build"]).json()
        if s["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert s["status"] == "done", s
    assert s["transactions_saved"] == 2
    spar = api.get("/api/transactions?q=Spar", headers=org["build"]).json()[0]
    assert spar["category_name"] == "Groceries"  # matched by keyword after import
    acme = api.get("/api/transactions?q=ACME", headers=org["build"]).json()[0]
    assert acme["tags"] == "Other Income"  # Capitec's own label kept as a hint, no junk category created
    assert "Other Income" not in {c["name"] for c in api.get("/api/categories", headers=org["auth"]).json()}


def test_parsers():
    text = ("Transaction History\n"
            "01/09/2025 Payment Received J Smith Other Income 1,500.00 2,500.00\n"
            "03/09/2025 Checkers Sandton Fees -250.00 -1.00 2,249.00\n")
    rows = CapitecParser().parse(text)
    assert [(r["type"], r["amount"]) for r in rows] == [("credit", 1500.0), ("debit", 250.0)]
    assert rows[1]["fee"] == 1.0 and rows[0]["date"] == date(2025, 9, 1)

    tyme = TymeBankLegacyParser().parse("05 Sep 2025 Woolworths Food\n- 99.90 - 400.10\n06 Sep 2025 Salary - - 5,000.00 5,400.10\n")
    assert [(r["type"], r["amount"]) for r in tyme] == [("debit", 99.9), ("credit", 5000.0)]

    rows = parse_csv(SALES_CSV)
    assert rows[1]["debits"] == 500 and rows[0]["credits"] == 12000


def test_cors_allows_pages_previews(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("FRONTEND_URL", "https://colunimbus.pages.dev")
    rx = Settings().cors_origin_regex
    import re as _re
    assert _re.fullmatch(rx, "https://colunimbus.pages.dev")
    assert _re.fullmatch(rx, "https://7f3a9ccb.colunimbus.pages.dev")
    assert not _re.fullmatch(rx, "https://evil.pages.dev")
    assert not _re.fullmatch(rx, "https://colunimbus.pages.dev.evil.com")


def test_insights_dashboard_ai_and_report(org, monkeypatch):
    from app.config import settings
    from app.services import categorize

    o = api.get("/api/insights/overview", headers=org["auth"]).json()
    assert o["scope"] == "All companies" and len(o["months"]) == 12
    assert {"in_12m", "out_12m", "net_12m", "uncategorized", "bank_fees_12m"} <= o["kpis"].keys()
    assert any(c["name"] == "Sales Co" for c in o["companies"])
    sales_id = next(c["id"] for c in o["companies"] if c["name"] == "Sales Co")
    one = api.get(f"/api/insights/overview?client_id={sales_id}", headers=org["auth"]).json()
    assert one["scope"] == "Sales Co" and all(c["name"] == "Sales Co" for c in one["companies"])
    assert api.get("/api/insights/overview?client_id=99999", headers=org["auth"]).status_code == 404

    assert api.get("/api/insights/suggestions", headers=org["auth"]).json()["items"] == []  # nothing saved, no call
    monkeypatch.setattr(settings, "groq_api_keys", ["g"])
    calls = []
    monkeypatch.setattr(categorize, "groq_json", lambda system, user: calls.append(user) or {"suggestions": [
        {"title": "Cut bank charges", "detail": "R120 a month in fees.", "kind": "save", "impact": "high", "rand_per_year": 1440},
        {"detail": "no title: dropped"}]})
    r = api.get("/api/insights/suggestions?refresh=true", headers=org["auth"]).json()
    assert r["items"] == [{"title": "Cut bank charges", "detail": "R120 a month in fees.", "kind": "save", "impact": "high", "value": 1440}]
    assert api.get("/api/insights/suggestions?refresh=true", headers=org["auth"]).json()["cached"] is True and len(calls) == 1
    assert "Sales Co" in calls[0]  # sends totals per company, no account numbers
    assert "62001234567" not in calls[0]

    monkeypatch.setattr(categorize, "groq_chat", lambda messages: "**Fine.**\nFOLLOWUPS: Where are costs? | How is cash?")
    c = api.post("/api/insights/chat", headers=org["auth"], json={"messages": [{"role": "user", "content": "How are we doing?"}]}).json()
    assert c == {"reply": "**Fine.**", "followups": ["Where are costs?", "How is cash?"]}

    rep = api.get("/api/insights/report?start=2020-01-01&end=2030-12-31", headers=org["auth"]).json()
    assert rep["total_income"] >= 0 and rep["net"] == round(rep["total_income"] - rep["total_expenses"], 2)
    assert api.get("/api/insights/report?start=x&end=y", headers=org["auth"]).status_code == 400


def test_vat_payroll_ageing_review_and_monthly_email(org, monkeypatch):
    import datetime as dt
    from decimal import Decimal

    from app.config import settings
    from app.db import SessionLocal
    from app.models import ERPNextInvoice, User
    from app.routers import tools
    from app.services import categorize

    assert tools.treatment("Groceries") == "standard" and tools.treatment("Interest Income") == "exempt"
    assert tools.treatment("Intercompany Transfer") == "none" and tools.treatment("Salaries & Wages") == "none"

    sales_id = org["sales"]["X-Client-Id"]
    v = api.get(f"/api/tools/vat?client_id={sales_id}&start=2025-09-01&end=2025-10-31", headers=org["auth"]).json()
    f = v["fields"]
    assert f["4_output_tax"] == round(f["1_standard_rated_supplies"] * 15 / 115, 2)
    assert f["20_vat_payable"] == round(f["4_output_tax"] - f["19_total_input_tax"], 2)
    assert api.get("/api/tools/vat?client_id=99999&start=2025-09-01&end=2025-10-31", headers=org["auth"]).status_code == 404

    p = api.get("/api/tools/payroll?start=2025-01-01&end=2025-12-31", headers=org["auth"]).json()
    assert p["total"] == 0 and p["notes"]  # no payroll lines in the sample data

    db = SessionLocal()
    today = dt.date.today()
    db.add_all([
        ERPNextInvoice(client_id=int(sales_id), invoice_type="Sales Invoice", erp_name="SINV-1", party_name="ACME", grand_total=Decimal("1000"),
                       outstanding_amount=Decimal("1000"), posting_date=today - dt.timedelta(days=80), due_date=today - dt.timedelta(days=50)),
        ERPNextInvoice(client_id=int(sales_id), invoice_type="Sales Invoice", erp_name="SINV-2", party_name="Beta", grand_total=Decimal("400"),
                       outstanding_amount=Decimal("400"), posting_date=today, due_date=today + dt.timedelta(days=30)),
        ERPNextInvoice(client_id=int(sales_id), invoice_type="Purchase Invoice", erp_name="PINV-1", party_name="Supplier", grand_total=Decimal("300"),
                       outstanding_amount=Decimal("300"), posting_date=today - dt.timedelta(days=100), due_date=today - dt.timedelta(days=95))])
    db.commit()
    a = api.get("/api/tools/ageing", headers=org["auth"]).json()
    assert a["debtors"]["buckets"]["31-60"] == 1000 and a["debtors"]["buckets"]["current"] == 400
    assert a["debtors"]["overdue"] == 1000 and a["debtors"]["parties"][0]["name"] == "ACME"
    assert a["creditors"]["buckets"]["90+"] == 300

    monkeypatch.setattr(settings, "groq_api_keys", ["g"])
    calls = []
    monkeypatch.setattr(categorize, "groq_json", lambda s, u: calls.append(u) or {"points": [{"title": "Chase ACME", "detail": "R1,000 is 50 days late.", "level": "high"}]})
    r = api.post("/api/tools/review", headers=org["auth"], json={"topic": "ageing"}).json()
    assert r["items"][0]["title"] == "Chase ACME" and not r["cached"]
    assert api.post("/api/tools/review", headers=org["auth"], json={"topic": "ageing"}).json()["cached"] and len(calls) == 1
    assert api.post("/api/tools/review", headers=org["auth"], json={"topic": "nope"}).status_code == 400
    period = {"start": f"{today.year - 1}-01-01", "end": today.isoformat()}
    r = api.post("/api/tools/review", headers=org["auth"], json={"topic": "report", **period}).json()
    assert r["items"] and '"total_income"' in calls[-1] and "generated_at" not in calls[-1]
    assert api.post("/api/tools/review", headers=org["auth"], json={"topic": "report", **period}).json()["cached"]

    sent = []
    import app.services.mailer as mailer
    monkeypatch.setattr(mailer, "send_mail", lambda to, subject, body, html=None: sent.append((to, subject, html)))
    assert api.get("/api/tools/monthly-email", headers=org["auth"]).json()["enabled"] is False
    assert api.put("/api/tools/monthly-email", headers=org["auth"], json={"enabled": True}).json()["enabled"] is True
    now = api.post("/api/tools/monthly-email/send-now", headers=org["auth"]).json()
    assert now["sent_to"] == "cm@example.com" and "Profit &amp; loss by company" in sent[0][2] and "Sales Co" in sent[0][2]
    assert tools.send_monthly(db) == 1 and tools.send_monthly(db) == 0  # once per month
    db.query(ERPNextInvoice).delete()
    db.commit()
    db.close()


def test_signup_lock_only_verified_ses_identities(monkeypatch):
    from app.config import settings
    from app.services import signup_lock

    monkeypatch.setattr(settings, "signup_lock", True)
    monkeypatch.setattr(signup_lock, "verified_identities", lambda: {"cthai.co.za", "solo@gmail.com"})
    body = {"practice_name": "Locked", "first_name": "L", "last_name": "K", "password": "Str0ng-pass!"}
    r = api.post("/api/auth/register", json={**body, "email": "x@other.co.za", "username": "lockx"})
    assert r.status_code == 400 and "other.co.za" in r.json()["detail"]["email"][0]
    r = api.post("/api/auth/register", json={**body, "email": "someone@gmail.com", "username": "lockg"})
    assert r.status_code == 400
    assert api.post("/api/auth/register", json={**body, "email": "Boss@CTHAI.co.za", "username": "lockc"}).status_code == 201
    assert api.post("/api/auth/register", json={**body, "email": "solo@gmail.com", "username": "locks"}).status_code == 201

    def down():
        raise RuntimeError("no aws")
    monkeypatch.setattr(signup_lock, "verified_identities", down)
    r = api.post("/api/auth/register", json={**body, "email": "y@cthai.co.za", "username": "locky"})
    assert r.status_code == 400 and "right now" in r.json()["detail"]["email"][0]


def test_request_access_emails_admin(monkeypatch):
    from app.config import settings
    from app.routers import auth
    from app.services import signup_lock

    sent = []
    monkeypatch.setattr(settings, "signup_lock", True)
    monkeypatch.setattr(settings, "admin_email", "admin@cthai.co.za")
    monkeypatch.setattr(signup_lock, "verified_identities", lambda: {"cthai.co.za"})
    monkeypatch.setattr(auth, "send_mail", lambda to, subject, body, html=None: sent.append((to, subject, body, html)))
    body = {"name": "Thabo <b>N</b>", "email": "thabo@newco.co.za", "company": "NewCo", "message": "Please add us"}
    r = api.post("/api/auth/request-access", json=body)
    assert r.status_code == 202 and "Request sent" in r.json()["message"]
    to, subject, text_body, html = sent[0]
    assert to == "admin@cthai.co.za" and "newco.co.za" in subject and "Please add us" in text_body
    assert "&lt;b&gt;" in html
    assert "already have" in api.post("/api/auth/request-access", json=body).json()["message"] and len(sent) == 1
    r = api.post("/api/auth/request-access", json={**body, "email": "boss@cthai.co.za"})
    assert "already sign up" in r.json()["message"] and len(sent) == 1


YOCO_CSV = """Yoco transactions export
Date,Receipt Number,Type,Status,Amount,Tip,Fee,Card Type
2026-03-02 10:15:00,R-1001,Sale,Approved,"1,150.00",0.00,26.45,Visa
2026-03-02 11:00:00,R-1002,Sale,Declined,300.00,0.00,0.00,Mastercard
2026-03-03 09:30:00,R-1003,Refund,Approved,-150.00,0.00,0.00,Visa
2026-03-04 08:00:00,P-77,Payout,Paid,973.55,0.00,0.00,
"""


def test_yoco_import_and_payout_not_double_counted(org):
    from app.services import yoco

    rows = yoco.parse_csv(YOCO_CSV.encode())
    assert [(r["kind"], float(r["gross"]), float(r["fee"])) for r in rows] == [("sale", 1150.0, 26.45), ("refund", 150.0, 0.0)]
    with pytest.raises(ValueError):
        yoco.parse_csv(b"name,colour\na,b\n")
    # The bank already shows Yoco's payout as money in.
    upload(org["build"], "Transaction Date,Description,Debits,Credits,Balance\n05/03/2026,YOCO PAYOUT 77,,973.55,\n")
    period = {"start": "2026-03-01", "end": "2026-03-31", "client_id": org["build_id"]}
    before = api.get("/api/insights/report", headers=org["auth"], params=period).json()
    assert before["total_income"] == 973.55
    r = api.post("/api/imports/yoco", headers=org["build"], files=[("csv_files", ("yoco.csv", YOCO_CSV, "text/csv"))])
    assert r.status_code == 200 and r.json()["imported"] == 3  # sale, its fee, the refund
    assert api.post("/api/imports/yoco", headers=org["build"],
                    files=[("csv_files", ("yoco.csv", YOCO_CSV, "text/csv"))]).json()["imported"] == 0
    after = api.get("/api/insights/report", headers=org["auth"], params=period).json()
    # Gross sales are income; the payout is a transfer now; fee and refund are costs.
    assert after["total_income"] == 1150.0 and after["total_expenses"] == round(26.45 + 150, 2)
    names = {i["name"] for i in after["income"]}
    assert "Card Sales (Yoco)" in names and "Yoco Payout" not in names


def test_payshap_notices_and_matching(org):
    from app.db import SessionLocal
    from app.models import PayShapNotice
    from app.services import payshap

    n = payshap.parse_notice("PayShap payment received", "You have received R2,500.00 from THABO MOKOENA via PayShap. Ref: INV 204.")
    assert n == {"direction": "in", "amount": Decimal("2500.00"), "counterparty": "THABO MOKOENA", "reference": "INV 204"}
    out = payshap.parse_notice("PayShap", "You sent R400.00 to Lerato Plumbing using PayShap ShapID 0821234567@capitec")
    assert out["direction"] == "out" and out["amount"] == Decimal("400.00") and out["counterparty"] == "Lerato Plumbing"
    assert payshap.parse_notice("Your statement", "R100.00 debit order") is None
    assert payshap.is_payshap("PAYSHAP PAYMENT FROM J SMITH") and not payshap.is_payshap("SHOPRITE PURCHASE")

    upload(org["sales"], "Transaction Date,Description,Debits,Credits,Balance\n"
                         f"{date.today():%d/%m/%Y},PAYSHAP CREDIT THABO,,2500.00,\n")
    db = SessionLocal()
    practice_id = api.get("/api/practice", headers=org["auth"]).json()["id"]
    import datetime as dt
    db.add_all([PayShapNotice(practice_id=practice_id, client_id=org["sales_id"], gmail_id="ps-1", subject="PayShap",
                              received_at=dt.datetime.utcnow(), **n),
                PayShapNotice(practice_id=practice_id, client_id=org["sales_id"], gmail_id="ps-2", subject="PayShap",
                              received_at=dt.datetime.utcnow(), direction="in", amount=Decimal("99.00"),
                              counterparty="NOT YET", reference="")])
    db.commit()
    db.close()
    s = api.get("/api/payshap", headers=org["auth"], params={"client_id": org["sales_id"]}).json()
    assert s["received"] == 2500.0 and s["count"] == 1 and s["lines"][0]["notified"]
    assert {x["counterparty"]: x["on_statement"] for x in s["notices"]} == {"THABO MOKOENA": True, "NOT YET": False}
    assert s["pending_in"] == 99.0


def test_statement_passwords_and_auto_read(org, monkeypatch):
    from app.db import SessionLocal
    from app.models import EmailStatement, UserGmailToken
    from app.services import gmail as gm
    from app.services.parsers import parse_text

    r = api.post("/api/imports/passwords", headers=org["auth"], json={"label": "Company reg no", "password": "2025230601"})
    assert r.status_code == 201 and r.json()[0]["label"] == "Company reg no" and "password" not in r.json()[0]
    assert gm.bank_for("FNB <statements@fnb.co.za>") == "fnb" and gm.bank_for("Yoco <noreply@yoco.com>") == "yoco"
    assert parse_text("nothing that looks like a statement", "fnb") == []

    db = SessionLocal()
    practice_id = api.get("/api/practice", headers=org["auth"]).json()["id"]
    user_id = api.get("/api/auth/me", headers=org["auth"]).json()["id"]
    st = EmailStatement(practice_id=practice_id, client_id=org["build_id"], source="gmail", gmail_id="auto-1",
                        subject="Statement", bank_name="fnb", has_attachment=True, state="new")
    db.add(st)
    db.commit()
    tried = []

    def fake_parse(self, s, password=None):
        tried.append(self.passwords(s))
        raise ValueError("Incorrect PDF password: none of the saved passwords opened it")
    monkeypatch.setattr(gm.Gmail, "parse_pdf_statement", fake_parse)
    out = gm.Gmail(db, UserGmailToken(user_id=user_id)).auto_read(practice_id)
    assert out["locked"] == 1 and tried == [["2025230601"]]
    assert db.get(EmailStatement, st.id).state == "locked"
    db.delete(db.get(EmailStatement, st.id))
    db.commit()
    db.close()
    pid = api.get("/api/imports/passwords", headers=org["auth"]).json()[0]["id"]
    assert api.delete(f"/api/imports/passwords/{pid}", headers=org["auth"]).status_code == 204


def test_mcp_keys_and_tools(org):
    upload(org["sales"], SALES_CSV, "mcp.csv")
    r = api.post("/api/mcp-keys", headers=org["auth"], json={"name": "SEMBLANCE"})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    assert key.startswith("colu_") and "key" not in api.get("/api/mcp-keys", headers=org["auth"]).json()[0]

    def rpc(method, params=None, headers=None, mid=1):
        return api.post("/mcp", headers=headers or {"Authorization": f"Bearer {key}"},
                        json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    assert rpc("tools/list", headers={"Authorization": "Bearer colu_wrong"}).status_code == 401
    assert rpc("tools/list", headers=org["auth"]).status_code == 401  # a login token is not an MCP key
    init = rpc("initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert init["serverInfo"]["name"] == "colunimbus" and init["capabilities"]["tools"]
    assert api.post("/mcp", headers={"Authorization": f"Bearer {key}"},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    names = {t["name"] for t in rpc("tools/list").json()["result"]["tools"]}
    assert {"companies", "overview", "report", "transactions", "vat", "payroll", "ageing"} <= names

    def call(name, args=None):
        res = rpc("tools/call", {"name": name, "arguments": args or {}}).json()["result"]
        return res, json.loads(res["content"][0]["text"]) if not res.get("isError") else res["content"][0]["text"]

    _, cos = call("companies")
    assert {c["client"]["name"] for c in cos["companies"]} == {"Sales Co", "Building Co"}
    _, txns = call("transactions", {"company_id": org["sales_id"], "start": "2025-09-01", "end": "2025-09-30", "search": "checkers"})
    assert txns["count"] >= 1 and all("checkers" in t["description"].lower() for t in txns["transactions"])
    res, msg = call("transactions", {})
    assert res["isError"] and "company_id" in msg
    _, rep = call("report", {"start": "2025-09-01", "end": "2025-09-30"})
    assert "notes" in rep
    res, _ = call("nope")
    assert res["isError"]
    assert rpc("bogus/method").json()["error"]["code"] == -32601

    kid = api.get("/api/mcp-keys", headers=org["auth"]).json()[0]["id"]
    assert api.delete(f"/api/mcp-keys/{kid}", headers=org["auth"]).status_code == 204
    assert rpc("tools/list").status_code == 401
