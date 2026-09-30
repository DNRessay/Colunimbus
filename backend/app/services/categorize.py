import json
import logging
import re
from itertools import count

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import BankTransaction, Client, TransactionCategory
from .defaults import DEFAULT_CATEGORIES

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MIN_AI_CONFIDENCE = 0.5
AI_BATCH = 40

CREDIT_TYPES = {"credit", "income"}
DEBIT_TYPES = {"debit", "expense", "transfer"}


def seed_categories(db: Session, practice_id: int, overwrite=False):
    existing = {c.name: c for c in db.scalars(select(TransactionCategory).where(TransactionCategory.practice_id == practice_id))}
    created = 0
    for name, kind, color, keywords, tags in DEFAULT_CATEGORIES:
        cat = existing.get(name)
        if not cat:
            db.add(TransactionCategory(practice_id=practice_id, name=name, transaction_type=kind, color=color,
                                       keywords=keywords, tags=tags))
            created += 1
        elif overwrite:
            cat.transaction_type, cat.keywords, cat.tags = kind, keywords, tags
    db.flush()
    return created


def categories(db: Session, practice_id: int, active_only=True):
    q = select(TransactionCategory).where(TransactionCategory.practice_id == practice_id)
    if active_only:
        q = q.where(TransactionCategory.active.is_(True))
    return list(db.scalars(q.order_by(TransactionCategory.name)))


def uncategorized(db: Session, client_id: int):
    return select(BankTransaction).where(
        BankTransaction.client_id == client_id,
        BankTransaction.erpnext_synced.is_(False),
        BankTransaction.category_id.is_(None),
    ).order_by(BankTransaction.date.desc())


def type_fits(category, txn):
    t = (category.transaction_type or "").lower()
    if txn.direction is None:
        return True
    if t in CREDIT_TYPES:
        return txn.direction == "credit"
    if t in DEBIT_TYPES:
        return txn.direction == "debit"
    return True


def match_category(txn, cats):
    for cat in cats:
        if type_fits(cat, txn) and (kw := cat.match(txn.description)):
            return cat, kw
    return None, None


def keyword_pass(txns, cats):
    leftovers, hits = [], 0
    for txn in txns:
        cat, _ = match_category(txn, cats)
        if cat:
            txn.category = cat
            hits += 1
        else:
            leftovers.append(txn)
    return hits, leftovers


def auto_categorize(db: Session, client: Client):
    txns = list(db.scalars(uncategorized(db, client.id)).unique())
    if not txns:
        return 0, 0
    hits, _ = keyword_pass(txns, categories(db, client.practice_id))
    db.commit()
    return hits, len(txns)


def preview(db: Session, client: Client):
    txns = list(db.scalars(uncategorized(db, client.id)).unique())
    cats = categories(db, client.practice_id)
    matches = [(t, *match_category(t, cats)) for t in txns]
    hits = [(t, c, kw) for t, c, kw in matches if c]
    return {
        "total_uncategorized": len(txns),
        "will_be_categorized": len(hits),
        "no_match": len(txns) - len(hits),
        "matches": [{"transaction_id": t.id, "description": t.description[:50], "category": c.name, "keyword": kw}
                    for t, c, kw in hits[:20]],
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
    "You categorize South African bank transactions for a bookkeeper. Pick the single best category for each "
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


def classify_text(db: Session, practice_id: int, text):
    cats = categories(db, practice_id)
    dummy = type("T", (), {"description": text, "direction": None})()
    cat, kw = match_category(dummy, cats)
    if cat:
        return {"raw": text, "category": cat.name, "confidence": "High", "keyword": kw, "method": "keyword"}
    if settings.groq_api_keys and cats:
        try:
            hit = ai_classify([text], [c.name for c in cats]).get(0)
            if hit:
                c = hit["confidence"]
                return {"raw": text, "category": hit["category"], "keyword": hit["keyword"], "method": "ai",
                        "confidence": "High" if c > 0.8 else "Medium" if c > 0.5 else "Low"}
        except Exception as e:
            log.warning("AI classify failed: %s", e)
    return {"raw": text, "category": "Uncategorized", "confidence": "Low", "keyword": None, "method": "none"}


def ai_categorize(db: Session, client: Client, min_confidence=MIN_AI_CONFIDENCE):
    """Keyword pass, then Groq for the rest. Learns merchant keywords from confident AI matches."""
    txns = list(db.scalars(uncategorized(db, client.id)).unique())
    cats = categories(db, client.practice_id)
    keyword_hits, leftovers = keyword_pass(txns, cats)
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
            txn.category = cat
            kw = hit["keyword"]
            if len(kw) > 2 and kw in txn.description.lower() and not re.fullmatch(r"[\d\W]+", kw):
                cat.add_keyword(kw)
            ai_hits += 1
        db.commit()
    return {"total": len(txns), "keyword": keyword_hits, "ai": ai_hits, "unmatched": low}
