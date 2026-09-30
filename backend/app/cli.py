"""Maintenance commands: python -m app.cli <command> [options]"""
import argparse
from datetime import date

from sqlalchemy import select

from .db import SessionLocal, init_db
from .models import BankTransaction, Client, Practice, TransactionCategory
from .services import categorize, erpnext
from .services.invoice_sync import sync_period


def _clients(db, client_id):
    q = select(Client).where(Client.is_active.is_(True))
    return db.scalars(q.where(Client.id == client_id) if client_id else q).all()


def seed_categories(db, args):
    for p in db.scalars(select(Practice)).all():
        print(p.name, categorize.seed_categories(db, p.id, overwrite=args.overwrite), "created")
    db.commit()


def auto_categorize(db, args):
    for c in _clients(db, args.client):
        print(c.name, categorize.ai_categorize(db, c) if args.ai else "categorized %d of %d" % categorize.auto_categorize(db, c))


def recategorize(db, args):
    """Clears categories on unsynced transactions, then re-runs keyword matching."""
    for c in _clients(db, args.client):
        for t in db.scalars(select(BankTransaction).where(BankTransaction.client_id == c.id,
                                                          BankTransaction.erpnext_synced.is_(False))).unique():
            t.category_id = None
        db.commit()
        print(c.name, "categorized %d of %d" % categorize.auto_categorize(db, c))


def cleanup_categories(db, args):
    used = set(db.scalars(select(BankTransaction.category_id).where(BankTransaction.category_id.is_not(None)).distinct()))
    empty = [c for c in db.scalars(select(TransactionCategory)) if c.id not in used]
    for c in empty:
        db.delete(c)
    db.commit()
    print(f"Deleted {len(empty)} unused categories.")


def erpnext_sync(db, args):
    for c in _clients(db, args.client):
        config = erpnext.active_config(db, c.practice_id)
        if config and c.erpnext_company:
            print(c.name, erpnext.sync_client(db, config, c, dry_run=args.dry_run, limit=args.limit))


def sync_invoices(db, args):
    for c in _clients(db, args.client):
        try:
            print(c.name, sync_period(db, c, args.year, args.month))
        except ValueError as e:
            print(c.name, f"skipped ({e})")


def main():
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create missing tables")
    s = sub.add_parser("seed-categories", help="add default categories to every organisation")
    s.add_argument("--overwrite", action="store_true")
    for name in ("auto-categorize", "recategorize", "erpnext-sync", "sync-invoices"):
        s = sub.add_parser(name)
        s.add_argument("--client", type=int, help="company id (default: all)")
        if name == "auto-categorize":
            s.add_argument("--ai", action="store_true", help="use Groq for anything keywords can't match")
        if name == "erpnext-sync":
            s.add_argument("--dry-run", action="store_true")
            s.add_argument("--limit", type=int, default=0)
        if name == "sync-invoices":
            s.add_argument("--year", type=int, default=date.today().year)
            s.add_argument("--month", type=int, default=date.today().month)
    sub.add_parser("cleanup-categories")
    args = p.parse_args()

    init_db()
    if args.cmd == "init-db":
        print("Tables ready.")
        return
    db = SessionLocal()
    try:
        {
            "seed-categories": seed_categories, "auto-categorize": auto_categorize, "recategorize": recategorize,
            "cleanup-categories": cleanup_categories, "erpnext-sync": erpnext_sync, "sync-invoices": sync_invoices,
        }[args.cmd](db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
