import json
import logging
import re
from itertools import count

import requests
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import BankTransaction, TransactionCategory

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MIN_AI_CONFIDENCE = 0.5
AI_BATCH = 40

JUNK_NAMES = {
    "uncategorised", "uncategorized", "other", "other income", "other expense",
    "other expenses", "fee fees", "terminal) fees", "***0) fees", "sweep transfer",
    "deposit investments", "applied transfer", "fnb cellphone", "digital payments",
    "4th transfer", "received interest",
}

CREDIT_TYPES = {"credit", "income"}
DEBIT_TYPES = {"debit", "expense", "transfer"}

# Built-in merchant/phrase -> category hints (first match wins, so order matters).
CLUES = {
    "supermarket": "Groceries", "mart": "Groceries", "checkers": "Groceries",
    "woolworths": "Groceries", "tucksho": "Groceries", "tuck sho": "Groceries",
    "tuck shop": "Groceries", "spaza": "Groceries", "pick n pay": "Groceries",
    "shoprite": "Groceries", "food lover": "Groceries", "spar": "Groceries",
    "usave": "Groceries", "s2s*": "Groceries", "ccn*": "Groceries", "alcohol": "Groceries",
    "caltex": "Fuel", "shell": "Fuel", "sasol": "Fuel", "engen": "Fuel", "total": "Fuel",
    "petrol": "Fuel", "fuel": "Fuel",
    "pharmacy": "Healthcare", "dischem": "Healthcare", "clicks": "Healthcare",
    "clinic": "Healthcare", "hospital": "Healthcare", "doctor": "Healthcare",
    "uber": "Transport", "bolt": "Transport", "taxi": "Transport", "gautrain": "Transport",
    "netflix": "Entertainment", "showmax": "Entertainment", "dstv": "Entertainment",
    "spotify": "Entertainment", "cinema": "Entertainment",
    "nando": "Food & Dining", "kfc": "Food & Dining", "mcdonalds": "Food & Dining",
    "steers": "Food & Dining", "wimpy": "Food & Dining", "pizza": "Food & Dining",
    "restaurant": "Food & Dining", "cafe": "Food & Dining", "coffee": "Food & Dining",
    "vodacom": "Telecommunications", "mtn": "Telecommunications", "telkom": "Telecommunications",
    "airtime": "Telecommunications", "recharge": "Telecommunications",
    "takealot": "Shopping", "mr price": "Shopping", "ackermans": "Shopping",
    "monthly account admin": "Banking & Finance", "branch card replacement": "Banking & Finance",
    "print statement fee": "Banking & Finance", "external payment": "Banking & Finance",
    "banking app": "Banking & Finance", "service fee": "Banking & Finance",
    "bank charge": "Banking & Finance", "monthly fee": "Banking & Finance", "admin fee": "Banking & Finance",
    "fnb": "Banking & Finance", "absa": "Banking & Finance", "nedbank": "Banking & Finance",
    "standard bank": "Banking & Finance", "capitec": "Banking & Finance",
    "eskom": "Utilities", "city power": "Utilities", "municipality": "Utilities",
    "electricity": "Utilities", "water rates": "Utilities",
    "set-off": "Bank Charges", "setoff": "Bank Charges", "sms payment notification": "Bank Charges",
    "stop payment": "Bank Charges", "dishonour": "Bank Charges", "unpaid debit": "Bank Charges",
    "debicheck insufficient": "Bank Charges", "eft debit order insufficient": "Bank Charges",
    "debicheck authentication": "Bank Charges", "insufficient funds": "Bank Charges",
    "earned interest": "Interest Income", "interest earned": "Interest Income", "interest": "Interest Income",
    "transfer from current": "Savings & Transfers", "transfer from savings": "Savings & Transfers",
    "transfer from cheque": "Savings & Transfers", "internal transfer": "Savings & Transfers",
    "live better": "Savings Round-up", "round-up": "Savings Round-up", "round up": "Savings Round-up",
    "transfer to": "Transfer Out", "immediate payment": "Transfer Out", "ewallet": "Transfer Out",
    "snapscan": "Transfer Out", "send money": "Transfer Out",
    "payshap": "Income", "transfer received": "Income", "received from": "Income",
    "salary": "Income", "payroll": "Income", "wages": "Income",
}


def is_junk(name):
    return (name or "").strip().lower() in JUNK_NAMES


def junk_ids(db: Session):
    return [cid for cid, name in db.execute(select(TransactionCategory.id, TransactionCategory.name)) if is_junk(name)]


def good_categories(db: Session):
    cats = db.scalars(select(TransactionCategory).where(TransactionCategory.active.is_(True)).order_by(TransactionCategory.name))
    return [c for c in cats if not is_junk(c.name)]


def needs_categorizing(db: Session, user_id: int):
    junk = junk_ids(db)
    cond = BankTransaction.category_id.is_(None)
    if junk:
        cond = or_(cond, BankTransaction.category_id.in_(junk))
    return select(BankTransaction).where(
        BankTransaction.user_id == user_id, BankTransaction.erpnext_synced.is_(False), cond
    ).order_by(BankTransaction.date.desc())


def type_fits(category, txn):
    t = (category.transaction_type or "").lower()
    if t in CREDIT_TYPES:
        return txn.direction == "credit"
    if t in DEBIT_TYPES:
        return txn.direction == "debit"
    return True


def find_clue(description):
    desc = (description or "").lower()
    return next(((clue, cat) for clue, cat in CLUES.items() if clue in desc), None)


def match_category(txn, categories):
    for cat in categories:
        if type_fits(cat, txn) and (kw := cat.match(txn.description)):
            return cat, kw
    return None, None


def get_or_create_category(db: Session, name, txn_type):
    cat = db.scalar(select(TransactionCategory).where(TransactionCategory.name == name))
    if not cat:
        cat = TransactionCategory(name=name, transaction_type=txn_type, active=True)
        db.add(cat)
        db.flush()
    return cat


def keyword_pass(db: Session, txns, categories):
    """DB keywords/tags first, then built-in clues (creating the category if needed). Returns leftovers."""
    by_name = {c.name.lower(): c for c in categories}
    leftovers = []
    hits = 0
    for txn in txns:
        cat, _ = match_category(txn, categories)
        if not cat and (clue := find_clue(txn.description)):
            word, name = clue
            cat = by_name.get(name.lower()) or get_or_create_category(db, name, txn.direction)
            cat.add_keyword(word)
            if name.lower() not in by_name:
                by_name[name.lower()] = cat
                categories.append(cat)
        if cat:
            txn.category_id = cat.id
            txn.category = cat
            hits += 1
        else:
            leftovers.append(txn)
    return hits, leftovers


def auto_categorize(db: Session, user_id: int):
    txns = list(db.scalars(needs_categorizing(db, user_id)).unique())
    if not txns:
        return 0, 0
    hits, _ = keyword_pass(db, txns, good_categories(db))
    db.commit()
    return hits, len(txns)


def preview(db: Session, user_id: int):
    txns = list(db.scalars(needs_categorizing(db, user_id)).unique())
    cats = good_categories(db)
    matches, no_match = [], []
    for txn in txns:
        cat, kw = match_category(txn, cats)
        if cat:
            matches.append((txn, cat.name, kw))
        elif (clue := find_clue(txn.description)):
            matches.append((txn, clue[1], clue[0]))
        else:
            no_match.append(txn)
    return {
        "total_uncategorized": len(txns),
        "will_be_categorized": len(matches),
        "no_match": len(no_match),
        "matches": [
            {"transaction_id": t.id, "description": t.description[:50], "category": name, "keyword": kw}
            for t, name, kw in matches[:20]
        ],
    }


# ── Groq ────────────────────────────────────────────────────────────────────

_key_cycle = count()


def groq_json(system, user):
    keys = settings.groq_api_keys
    if not keys:
        raise RuntimeError("GROQ_API_KEYS is not configured.")
    start = next(_key_cycle)
    last_error = None
    for i in range(len(keys)):
        key = keys[(start + i) % len(keys)]
        try:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": settings.groq_model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                },
                timeout=60,
            )
        except requests.RequestException as e:
            last_error = e
            continue
        if r.status_code in (401, 403, 429) or r.status_code >= 500:
            last_error = RuntimeError(f"Groq {r.status_code}: {r.text[:200]}")
            continue  # rotate to the next key
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])
    raise RuntimeError(f"All Groq keys failed: {last_error}")


SYSTEM_PROMPT = (
    "You categorize South African bank transactions. Pick the single best category for each "
    "transaction from the allowed list only, or \"Uncategorized\" if nothing fits. Also return the "
    "merchant/keyword from the description that justifies the choice (lowercase, copied verbatim). "
    'Reply as JSON: {"results": [{"i": <index>, "category": "<name>", "confidence": <0-1>, "keyword": "<word>"}]}'
)


def ai_classify(descriptions, category_names):
    lines = "\n".join(f"{i}. {d}" for i, d in enumerate(descriptions))
    data = groq_json(SYSTEM_PROMPT, f"Allowed categories: {json.dumps(category_names)}\n\nTransactions:\n{lines}")
    allowed = {n.lower(): n for n in category_names}
    out = {}
    for r in data.get("results", []):
        try:
            i = int(r.get("i"))
            name = allowed.get(str(r.get("category", "")).lower())
            conf = float(r.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        if name and 0 <= i < len(descriptions):
            out[i] = {"category": name, "confidence": conf, "keyword": str(r.get("keyword") or "").lower().strip()}
    return out


def classify_text(db: Session, text):
    cats = good_categories(db)
    names = [c.name for c in cats]
    clue = find_clue(text)
    if settings.groq_api_keys and names:
        try:
            hit = ai_classify([text], names).get(0)
            if hit:
                score = hit["confidence"]
                if clue and clue[1] == hit["category"]:
                    score = min(score + 0.5, 1.0)
                return {
                    "raw": text, "category": hit["category"], "score": score,
                    "confidence": "High" if score > 0.8 else "Medium" if score > 0.5 else "Low",
                    "clue_detected": clue[0] if clue else None,
                    "method": "ai+clue" if clue else "ai",
                    "top3": [(hit["category"], f"{score * 100:.1f}%")],
                }
        except Exception as e:
            log.warning("AI classify failed, falling back to clues: %s", e)
    dummy = type("T", (), {"description": text, "direction": "debit"})()
    cat, kw = match_category(dummy, cats)
    if cat:
        return {"raw": text, "category": cat.name, "score": 1.0, "confidence": "Medium",
                "clue_detected": kw, "method": "keyword", "top3": [(cat.name, "100.0%")]}
    if clue:
        return {"raw": text, "category": clue[1], "score": 1.0, "confidence": "Medium",
                "clue_detected": clue[0], "method": "clue_only", "top3": [(clue[1], "100.0%")]}
    return {"raw": text, "category": "Uncategorized", "score": 0.0, "confidence": "Low",
            "clue_detected": None, "method": "none", "top3": []}


def ai_categorize(db: Session, user_id: int, min_confidence=MIN_AI_CONFIDENCE):
    """Keyword/clue pass, then Groq for whatever is left. Learns keywords from AI matches."""
    txns = list(db.scalars(needs_categorizing(db, user_id)).unique())
    cats = good_categories(db)
    keyword_hits, leftovers = keyword_pass(db, txns, cats)
    db.commit()

    ai_hits = low = 0
    by_name = {c.name: c for c in cats}
    for start in range(0, len(leftovers), AI_BATCH):
        batch = leftovers[start:start + AI_BATCH]
        results = ai_classify([t.description for t in batch], list(by_name))
        for i, txn in enumerate(batch):
            hit = results.get(i)
            if not hit or hit["confidence"] < min_confidence:
                low += 1
                continue
            cat = by_name[hit["category"]]
            txn.category_id = cat.id
            kw = hit["keyword"]
            if len(kw) > 2 and kw in txn.description.lower() and not re.fullmatch(r"[\d\W]+", kw):
                cat.add_keyword(kw)
            ai_hits += 1
        db.commit()

    return {"total": len(txns), "keyword": keyword_hits, "ai": ai_hits, "unmatched": low}
