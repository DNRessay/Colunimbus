"""Maintenance commands: python -m app.cli <command> [options]"""
import argparse
from datetime import date

from sqlalchemy import select

from .db import SessionLocal, init_db
from .models import BankTransaction, ERPNextConfig, TransactionCategory, User
from .services import categorize, erpnext
from .services.invoice_sync import sync_period

SEED_CATEGORIES = [
    ("Groceries", "debit", 4, "supermarket,checkers,woolworths,pick n pay,spar,shoprite,food lover,clicks food,spaza,tucksho,mart,makro,game store,usave,s2s,ccn", "checkers,woolworths,spar,shoprite,pnp,foodlovers"),
    ("Fuel", "debit", 1, "caltex,shell,sasol,engen,total,bp,astron,fuel,petrol", "caltex,shell,sasol,engen,total,bp"),
    ("Transport", "debit", 2, "uber,bolt,taxi,gautrain,metrobus,intercape,greyhound,ride,trip", "uber,bolt"),
    ("Food & Dining", "debit", 3, "nando,kfc,mcdonalds,mcdonald,steers,wimpy,debonairs,fishaways,chicken licken,burger king,roman's,pizza,restaurant,cafe,bakery,coffee,mugg,bean,ocean basket", "nandos,kfc,mcdonalds,steers,wimpy,debonairs"),
    ("Entertainment", "debit", 5, "netflix,showmax,dstv,spotify,apple music,youtube,amazon prime,hulu,disney,cinema,ster-kinekor,nu metro,gaming,playstation,xbox", "netflix,showmax,dstv,spotify"),
    ("Healthcare", "debit", 6, "dischem,pharmacy,clicks,clinic,hospital,doctor,dentist,optometrist,medirite,medihelp,discovery health,bonitas,momentum health", "dischem,clicks"),
    ("Telecommunications", "debit", 7, "vodacom,mtn,telkom,cell c,airtime,data,prepaid,recharge,rain,afrihost,webafrica", "vodacom,mtn,telkom,cellc"),
    ("Banking & Finance", "debit", 8, "fnb,absa,nedbank,standard bank,capitec,investec,african bank,service fee,bank charge,atm fee,monthly fee,admin fee,interest charged,insurance premium,monthly account admin,branch card replacement,print statement", "fnb,absa,nedbank,capitec"),
    ("Bank Charges", "debit", 13, "set-off,setoff,sms payment notification,sms notification,stop payment,unpaid debit,dishonour,penalty fee,returned item,early settlement,account maintenance,debit order fee,sms fee,card fee,statement fee,dispute fee,debicheck insufficient funds,eft debit order insufficient funds,debicheck authentication,insufficient funds fee,debit order return,unpaid debit order,debicheck,eft debit order", "setoff,sms,stop payment,debicheck,insufficient funds"),
    ("Utilities", "debit", 9, "eskom,city power,municipality,rates,water,electricity,prepaid electricity,sanitation,refuse,tshwane,joburg,ekurhuleni,cape town metro", "eskom,tshwane,joburg"),
    ("Shopping", "debit", 10, "takealot,amazon,mr price,ackermans,pep,jet,woolworths clothing,h&m,zara,edgars,truworths,foschini,sportsmans,outdoor,builders,leroy merlin", "takealot,mrprice,ackermans,pep"),
    ("Income", "credit", 11, "salary,payroll,wages,payment received,transfer received,received from,transfer in,deposit,commission,bonus,dividend,refund,payshap payment received,payshap received", "salary,payroll,payshap"),
    ("Interest Income", "credit", 15, "earned interest,interest earned,interest credited,interest paid,savings interest,interest income,interest", "interest,earned interest"),
    ("Savings & Transfers", "credit", 16, "transfer from current,transfer from savings,transfer from cheque,internal transfer,transfer between accounts,transfer from account", "transfer from current,internal transfer"),
    ("Digital Payments", "debit", 17, "client care immediate payment,immediate payment,digital payment,online payment,internet payment,card purchase online,e-payment", "immediate payment,digital payment"),
    ("Savings Round-up", "debit", 14, "live better,round-up,round up,live better round-up,live better transfer,savings round", "livebetter,roundup"),
    ("Transfer Out", "debit", 12, "transfer to,send money,ewallet,capitec pay,fnb pay,snapscan,zapper,payfast,peach payments,immediate payment,payshap send", "ewallet,snapscan"),
]


def seed_categories(db, args):
    created = updated = 0
    for name, kind, color, keywords, tags in SEED_CATEGORIES:
        cat = db.scalar(select(TransactionCategory).where(TransactionCategory.name == name))
        if not cat:
            db.add(TransactionCategory(name=name, transaction_type=kind, color=color, keywords=keywords, tags=tags, active=True))
            created += 1
        elif args.overwrite:
            cat.transaction_type, cat.keywords, cat.tags = kind, keywords, tags
            updated += 1
    db.commit()
    print(f"{created} created, {updated} updated.")


def _users(db, user_id):
    q = select(User.id).where(User.is_active.is_(True))
    return db.scalars(q.where(User.id == user_id) if user_id else q).all()


def auto_categorize(db, args):
    for uid in _users(db, args.user):
        if args.ai:
            print(uid, categorize.ai_categorize(db, uid))
        else:
            print(uid, "categorized %d of %d" % categorize.auto_categorize(db, uid))


def recategorize(db, args):
    """Re-runs keyword matching. --all wipes unsynced categories first."""
    for uid in _users(db, args.user):
        if args.all:
            for t in db.scalars(select(BankTransaction).where(BankTransaction.user_id == uid,
                                                              BankTransaction.erpnext_synced.is_(False))).unique():
                t.category_id = None
            db.commit()
        print(uid, "categorized %d of %d" % categorize.auto_categorize(db, uid))


def cleanup_categories(db, args):
    used = set(db.scalars(select(BankTransaction.category_id).where(BankTransaction.category_id.is_not(None)).distinct()))
    empty = [c for c in db.scalars(select(TransactionCategory)) if c.id not in used]
    for c in empty:
        db.delete(c)
    db.commit()
    print(f"Deleted {len(empty)} empty categories.")


def erpnext_sync(db, args):
    q = select(ERPNextConfig).where(ERPNextConfig.is_active.is_(True))
    if args.user:
        q = q.where(ERPNextConfig.user_id == args.user)
    for cfg in db.scalars(q).all():
        print(cfg.user_id, erpnext.full_sync(db, cfg, dry_run=args.dry_run, limit=args.limit))


def sync_invoices(db, args):
    for uid in _users(db, args.user):
        try:
            print(uid, sync_period(db, uid, args.year, args.month))
        except ValueError as e:
            print(uid, f"skipped ({e})")


def main():
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create missing tables")
    s = sub.add_parser("seed-categories")
    s.add_argument("--overwrite", action="store_true")
    s = sub.add_parser("auto-categorize")
    s.add_argument("--user", type=int)
    s.add_argument("--ai", action="store_true", help="use Groq for anything keywords can't match")
    s = sub.add_parser("recategorize")
    s.add_argument("--user", type=int)
    s.add_argument("--all", action="store_true")
    sub.add_parser("cleanup-categories")
    s = sub.add_parser("erpnext-sync")
    s.add_argument("--user", type=int)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--limit", type=int, default=0)
    s = sub.add_parser("sync-invoices")
    s.add_argument("--user", type=int)
    s.add_argument("--year", type=int, default=date.today().year)
    s.add_argument("--month", type=int, default=date.today().month)
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
