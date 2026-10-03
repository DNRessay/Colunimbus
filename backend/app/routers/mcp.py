# Read-only Model Context Protocol endpoint, so assistants like SEMBLANCE can answer questions about the books.
# Streamable HTTP, stateless JSON replies (fits Lambda): POST /mcp with a JSON-RPC message and a bearer key —
# or add the URL as a custom connector in Claude and sign in (mcp_connect.py), which makes the key for you.
import hashlib
import json
import secrets
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import current_user, get_db
from ..models import BankTransaction, McpKey, User, utcnow
from . import core, insights, tools

router = APIRouter(tags=["mcp"])
keys_router = APIRouter(prefix="/api/mcp-keys", tags=["mcp"])

KEY_PREFIX = "colu_"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Bank-based bookkeeping for a group of South African companies. Amounts are in rand. Read-only. "
    "Start with `companies` to get company ids, then `overview` for the big picture. Dates are YYYY-MM-DD. "
    "Transfers between the group's own companies are excluded from income and expenses."
)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ── Key management (JWT-authenticated, from Settings) ──────────────────────

class KeyIn(BaseModel):
    name: str = "SEMBLANCE"


def _key_out(k: McpKey):
    return {"id": k.id, "name": k.name, "prefix": k.prefix, "created_at": k.created_at, "last_used_at": k.last_used_at}


@keys_router.get("")
def list_keys(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_key_out(k) for k in db.scalars(select(McpKey).where(McpKey.user_id == user.id).order_by(McpKey.id.desc()))]


@keys_router.post("", status_code=201)
def create_key(body: KeyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    k = McpKey(user_id=user.id, name=(body.name or "SEMBLANCE").strip()[:100], prefix=raw[:12], key_hash=_hash(raw))
    db.add(k)
    db.commit()
    return {**_key_out(k), "key": raw}


@keys_router.delete("/{key_id}", status_code=204)
def delete_key(key_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    k = db.get(McpKey, key_id)
    if not k or k.user_id != user.id:
        raise HTTPException(404, "Not found.")
    db.delete(k)
    db.commit()


def _key_user(db: Session, authorization: str) -> Optional[User]:
    raw = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not raw.startswith(KEY_PREFIX):
        return None
    k = db.scalar(select(McpKey).where(McpKey.key_hash == _hash(raw)))
    user = db.get(User, k.user_id) if k else None
    if not user or not user.is_active:
        return None
    k.last_used_at = utcnow()
    db.commit()
    return user


# ── Tools ──────────────────────────────────────────────────────────────────

def _period(args, default_days=None):
    today = date.today()
    if default_days is None:
        last_month_end = today.replace(day=1) - timedelta(days=1)
        start, end = last_month_end.replace(day=1), last_month_end
    else:
        start, end = today - timedelta(days=default_days), today
    s, e = args.get("start") or start.isoformat(), args.get("end") or end.isoformat()
    return tools._period(s, e)


def _company(args, required=False):
    cid = args.get("company_id")
    if cid in (None, "", 0):
        if required:
            raise HTTPException(400, "company_id is required; call `companies` to get ids.")
        return None
    return int(cid)


def t_companies(db, user, args):
    return core.dashboard(user=user, db=db)


def t_overview(db, user, args):
    months = max(2, min(int(args.get("months") or 12), 36))
    return insights.overview_data(db, user, _company(args), months)


def t_report(db, user, args):
    s, e = _period(args)
    return insights.report(s.isoformat(), e.isoformat(), _company(args), user=user, db=db)


def t_transactions(db, user, args):
    cid = _company(args, required=True)
    insights._clients(db, user, cid)
    s, e = _period(args, default_days=90)
    stmt = select(BankTransaction).where(BankTransaction.client_id == cid, BankTransaction.date >= s, BankTransaction.date <= e)
    if args.get("search"):
        stmt = stmt.where(BankTransaction.description.ilike(f"%{args['search']}%"))
    if args.get("uncategorized"):
        stmt = stmt.where(BankTransaction.category_id.is_(None))
    if args.get("direction") in ("credit", "debit"):
        stmt = stmt.where(BankTransaction.transaction_type == args["direction"])
    limit = max(1, min(int(args.get("limit") or 50), 500))
    rows = db.scalars(stmt.order_by(BankTransaction.date.desc(), BankTransaction.id.desc()).limit(limit)).unique()
    want = (args.get("category") or "").lower()
    out = [{"id": t.id, "date": t.date, "description": t.description, "direction": t.direction, "amount": float(t.value),
            "fee": float(t.fee or 0), "balance": float(t.balance) if t.balance is not None else None,
            "category": t.category_name, "reconciled": t.recon_status, "in_erpnext": t.erpnext_synced}
           for t in rows if not want or want in (t.category_name or "").lower()]
    return {"period": [s, e], "count": len(out), "transactions": out}


def t_vat(db, user, args):
    s, e = _period(args)
    return tools.vat_data(db, user, _company(args, required=True), s, e)


def t_payroll(db, user, args):
    s, e = _period(args)
    return tools.payroll_data(db, user, _company(args), s, e)


def t_ageing(db, user, args):
    return tools.ageing_data(db, user, _company(args))


_CO = {"company_id": {"type": "integer", "description": "Company id from `companies`. Omit for all companies."}}
_CO_REQ = {"company_id": {"type": "integer", "description": "Company id from `companies`."}}
_DATES = {"start": {"type": "string", "description": "YYYY-MM-DD. Defaults to the start of last month."},
          "end": {"type": "string", "description": "YYYY-MM-DD. Defaults to the end of last month."}}

TOOLS = {
    "companies": (t_companies, "The group's companies with ids, transaction counts, uncategorised and unsynced counts, "
                               "latest transaction date and whether last month is closed.", {}, []),
    "overview": (t_overview, "Money in/out per month, net, margin, bank fees, top expense and income categories and per-company "
                             "totals over the last N months.",
                 {**_CO, "months": {"type": "integer", "description": "2-36, default 12."}}, []),
    "report": (t_report, "Profit & loss and cash flow for a period: income and expenses by category, monthly totals, notes.",
               {**_CO, **_DATES}, []),
    "transactions": (t_transactions, "Bank transactions for one company, newest first. Defaults to the last 90 days.",
                     {**_CO_REQ, "start": {"type": "string", "description": "YYYY-MM-DD"}, "end": {"type": "string", "description": "YYYY-MM-DD"},
                      "search": {"type": "string", "description": "Text in the description."},
                      "category": {"type": "string", "description": "Part of a category name."},
                      "direction": {"type": "string", "enum": ["credit", "debit"]},
                      "uncategorized": {"type": "boolean"},
                      "limit": {"type": "integer", "description": "1-500, default 50."}}, ["company_id"]),
    "vat": (t_vat, "VAT 201 preparation for one company and period (15% inclusive), with what was left out and why.",
            {**_CO_REQ, **_DATES}, ["company_id"]),
    "payroll": (t_payroll, "Salaries and statutory payments (PAYE/UIF/SDL) found in the bank for a period.", {**_CO, **_DATES}, []),
    "ageing": (t_ageing, "Debtor and creditor ageing from synced ERPNext invoices: buckets and the most overdue parties.", _CO, []),
}


def tool_list():
    return [{"name": name, "description": desc, "annotations": {"readOnlyHint": True},
             "inputSchema": {"type": "object", "properties": props, "required": req}}
            for name, (_, desc, props, req) in TOOLS.items()]


def call_tool(db: Session, user: User, name: str, args: dict):
    if name not in TOOLS:
        return {"content": [{"type": "text", "text": f"Unknown tool {name}."}], "isError": True}
    try:
        data = jsonable_encoder(TOOLS[name][0](db, user, args or {}))
    except HTTPException as e:
        return {"content": [{"type": "text", "text": str(e.detail)}], "isError": True}
    except (ValueError, TypeError) as e:
        return {"content": [{"type": "text", "text": f"Bad arguments: {e}"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}


# ── JSON-RPC ───────────────────────────────────────────────────────────────

def _handle(db: Session, user: User, msg: dict):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None  # notification
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "colunimbus", "version": "1.0"}, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        result = call_tool(db, user, params.get("name", ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


@router.post("/mcp")
def mcp(request: Request, payload: Any = Body(...), authorization: str = Header(""), db: Session = Depends(get_db)):
    user = _key_user(db, authorization)
    if not user:
        # The header points MCP clients (e.g. Claude's custom connectors) at the sign-in (mcp_connect.py).
        from ..mcp_oauth import www_authenticate
        from .mcp_connect import base_url
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Invalid or missing MCP key."}},
                            status_code=401, headers={"WWW-Authenticate": www_authenticate(base_url(request))})
    if isinstance(payload, list):
        replies = [r for r in (_handle(db, user, m) for m in payload if isinstance(m, dict)) if r]
        return JSONResponse(replies) if replies else Response(status_code=202)
    if not isinstance(payload, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request."}}, status_code=400)
    reply = _handle(db, user, payload)
    return JSONResponse(reply) if reply else Response(status_code=202)


@router.get("/mcp")
def mcp_stream():
    return Response(status_code=405, headers={"Allow": "POST"})
