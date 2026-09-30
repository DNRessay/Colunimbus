# C.T.H.A.I — Product Plan

Status: v2 (2026-09-30). Built by Vicinic (IT services & solutions).

## 1. Who it's for

An in-house accountant who keeps the books for a **group of related companies**:
- a **sales** company
- a **building / carpentry** company

The books live in **ERPNext**, with one ERPNext site and one ERPNext Company per business.

The app sits in front of ERPNext and does the grind. Bank statements come in, transactions get categorized, they're posted to ERPNext, each month is reconciled and closed, and money moving between the group's companies is flagged so it's booked correctly.

This is built generically (an organisation has many companies), so Vicinic can sell the same product to other groups or small accounting practices later.

## 2. Decisions so far

| Topic | Decision |
|---|---|
| Live bank feeds | **Not now.** Most SA banks have no usable third-party API. Statements (PDF, CSV, Gmail) are the input. Revisit per bank later: Investec first. |
| Yoco | **Dropped.** It's about 2% of volume, and most customers pay by EFT. |
| Ledger | **ERPNext** is the system of record. The app never replaces it; it feeds it and reconciles against it. |
| Database | **Neon Postgres** (free tier). Use its EU region (Frankfurt or London), closest to AWS Ireland. |
| Hosting | **AWS eu-west-1 (Ireland)** Lambda for the API and worker. Frontend on Cloudflare Pages. |
| AI | **Groq** only, with rotating keys. The AI suggests categories; a person confirms. |
| Legal / compliance | Kept to cheap defaults that don't slow the build: ERPNext secrets and PDF passwords are encrypted at rest, and each company's data is separated. Revisit when selling to outside clients. |

## 3. What's built (v2)

**Organisation and companies**
- Sign-up creates the organisation. The owner adds team members and companies.
- A company switcher in the top bar. Everything company-specific is scoped to the selected company.

**Imports**
- PDF upload for Capitec, TymeBank and GoTyme, plus a best-effort generic parser.
- CSV upload.
- Gmail fetch into a shared statements inbox. Emails that mention a known account number are auto-assigned to the right company; the rest wait in the inbox for a person to assign.

**Categorization**
- Shared category list with keywords. Keyword matching runs first.
- Groq AI for the rest, which learns new merchant keywords as it goes.
- The bank's own labels are kept as hints instead of creating junk categories.

**ERPNext**
- One connection for the whole group.
- Each company links to an ERPNext Company.
- Each category maps to an ERPNext account per company, e.g. "Groceries - SC" in Sales Co and "Staff Food - BC" in Building Co.
- Bank accounts map to ERPNext bank accounts.
- Sync posts Bank Entry journal entries through the background worker. A preflight step lists any mappings that are missing first.

**Intercompany**
- Detects transfers between the group's companies: same amount, within 3 days, out of one company and into another.
- One click books both sides as an intercompany transfer.

**Reconciliation**
- Month by month per company: fetch ERPNext journal entries, auto-match, manual match, close and reopen the period, CSV export.

**ERPNext invoices**
- Sales and purchase invoices pulled into the app per company (read-only).

**Dashboard**
- Every company at a glance: uncategorized items, items not yet in ERPNext, whether last month is closed, and the Gmail inbox count.

**Removed**
- GitHub and Facebook login (Google sign-in kept, for existing users only)
- CV-style profile fields and social links
- The local invoicing module
- Package tiers
- The hard-coded keyword list (it now lives as editable default categories)
- The junk-category logic

## 4. Next

| # | Item | Why |
|---|---|---|
| 1 | Deploy. Neon DB, SAM to eu-west-1, Pages. Connect it to his real ERPNext. | Get it in his hands. |
| 2 | Parsers for **FNB, ABSA, Standard Bank and Nedbank** (need sample statements). | Capitec, TymeBank and GoTyme alone won't cover the group's banks. |
| 3 | **Statement checks.** Opening balance + transactions = closing balance, and no gaps between consecutive statements. | Catches missing pages and months before recon. |
| 4 | **Intercompany in ERPNext.** Post both sides to "Loan to/from <company>" accounts automatically. | Today it only categorizes them. |
| 5 | **Reports from ERPNext.** P&L, balance sheet and a VAT201 working sheet per company, plus a combined group view, pulled through the ERPNext report API. | He wants a reporting tool. ERPNext holds the numbers. |
| 6 | **Job costing** for the building company: tag transactions to a project or job (ERPNext Project / Cost Center). | Carpentry and building work is quoted per job, so profit per job matters. |
| 7 | WhatsApp statement upload (reuses the BHKA bot pattern). | Convenience. |

## 5. Investing: separate app, C-Lab

Charlie's personal investing (EasyEquities holdings, property, JSE watchlist, forex/macro scorecard) lives in its own app, **C-Lab**. It is kept out of C.T.H.A.I so the business tool only ever holds the group's business data.

## 6. Open questions

**For the accountant:**
1. Which banks do the two companies use? Can we get one sample statement per bank (numbers can be blanked)?
2. ERPNext: which version is it, and is it self-hosted or Frappe Cloud? Does each company already exist as a Company in the one site?
3. How are transfers between the companies booked today (loan accounts? which ones)?
4. Which reports does he actually produce each month, and for whom (SARS, owners, bank)?
5. Does the building company track profit per job or project today?
6. Investing: can he export his EasyEquities transaction history?

**For Vicinic:**
7. ~~Branding~~: the product is called **C.T.H.A.I**.
