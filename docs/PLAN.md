# Colunimbus — Product Plan

Status: draft v1 (2026-09-30). Owner: Vicinic. First client: CT Holdings & Investment (CTHAI).
Everything under **Open questions** needs an answer before the phase that depends on it starts.

---

## 1. What changed and why

| Original idea | Reality | Decision |
|---|---|---|
| Pull transactions live from the bank into ERPNext | SA has no open-banking mandate. Most banks don't offer a public API to third parties. Investec is the main exception (Programmable Banking). | **Statements stay the main input**: PDF, CSV, OFX, email and WhatsApp. Live feeds come later, bank by bank, starting with Investec. |
| Yoco integration | The accountant says card payments are about 2% of client volume. Most clients get paid by EFT. | **Dropped.** |
| ERPNext as the core ledger | One ERPNext site per small client is heavy to run. CTHAI files with SARS and hands clients P&L and balance sheet reports. | **The app becomes the practice's working layer.** Reports are built natively. ERPNext, Sage, Xero and Excel become optional export targets (see Q1). |
| Forex tool for the accountant | He invests through EasyEquities (JSE shares, property), not forex. | A **separate, small investing tracker** focused on SA. It reuses the forex-news engine. |

## 2. Who it's for

**CTHAI** is a bookkeeping, VAT, payroll and tax practice (directors: Charlie Mokoena and Tlisetso Malemule). How they work:

1. **Onboard**: collect the client's bank statements and backlog, and pick a package by transaction volume:
   - Starter: 10–59 transactions a month
   - Core SME: 60–150
   - SME + Payroll
2. **Process monthly**: bookkeeping, bank reconciliation, VAT schedules, payroll.
3. **Submit & report**: VAT201, EMP201/EMP501, ITR12 and business returns to SARS, plus P&L and balance sheet reports for the client.

The product has to make step 2 fast and make step 3 come out automatically. That's where the practice's hours go.

**Target user:** a bookkeeper at the practice handling 20–100 small clients, each with 1–3 bank accounts.

## 3. Product scope

### Core (the practice app)
1. **Client workspaces**: one practice, many clients. Each client has its own bank accounts, chart of accounts, VAT status, financial year-end and package tier.
2. **Statement inbox**: upload PDF, CSV or OFX, or forward statements to a practice email address (Gmail import already exists). WhatsApp upload comes later.
   - Parsers exist today for Capitec, TymeBank and GoTyme.
   - Still to add: FNB, ABSA, Standard Bank, Nedbank, Discovery and Investec.
   - Checks: opening and closing balances must tie, and there must be no gap between consecutive statements.
3. **Categorization**: mapping rules per client, then keyword matching, then Groq AI suggestions. The bookkeeper approves AI suggestions in bulk, and the system learns from each approval. Every transaction gets a VAT code: standard 15%, zero-rated, exempt or no VAT.
4. **Recon workbench**:
   - Per client, per month: statement balance vs book balance, unmatched items, and duplicates.
   - Month-close checklist: statements received, all categorized, reconciled, VAT schedule prepared, reviewed.
   - Closing a period locks it.
5. **Reports**:
   - P&L, balance sheet, trial balance and general ledger (built from bank transactions plus manual journals and opening balances)
   - VAT201 working schedule (output vs input VAT)
   - Management pack PDF
   - Export to Excel, CSV, ERPNext, Sage or Xero
6. **Practice dashboard**:
   - Which clients are late on statements, have unreconciled items or VAT due
   - SARS deadline calendar: VAT201, EMP201 by the 7th, provisional tax, ITR season
   - Transaction count per client vs their package tier, which drives billing and upsell

### Later
- Live bank feeds: an Investec pilot, then an aggregator if one becomes viable.
- Client portal: clients upload statements and see their own reports.
- Payroll helpers: EMP201 figures.

### Out of scope
Yoco, POS integrations, filing directly with SARS eFiling (submission stays manual), and a payroll engine.

## 4. Cleanup of the current codebase

Current code: `backend/` (FastAPI on SAM) and `frontend/` (Cloudflare Pages). The biggest problem is structural. **All data is scoped per user, and categories are global.** A practice needs data scoped per client. Remove these:

| Remove | Why |
|---|---|
| GitHub and Facebook social login | Accountants won't use them. Keep email+password, and Google (the practice runs on Gmail). |
| Social links and CV-style profile fields (`linkedin_url`, `github_url`, `portfolio_url`, `years_experience`, `industry`, `id_number`) | They come from an unrelated project. |
| Local `invoices` / `invoice_items` module and page | The practice doesn't invoice from this app. |
| ERPNext invoice sync (`erp_invoices`) | Keep only if Q1 answers "ERPNext". Otherwise it moves into a generic export module. |
| Hard-coded `CLUES` list in `categorize.py` | Replace it with a seeded default rule set per client, so each client's rules are editable. |
| Yoco (never built) | Out of scope. |

Keep as-is: statement parsers, CSV import, PDF job pipeline, Groq categorization, reconciliation engine, JWT auth, SAM deploy, and the Pages frontend shell.

Restructure:
- Add `Practice → Client → BankAccount` and scope every table by `client_id`.
- Replace the global `TransactionCategory` with a per-client `Account` (chart of accounts), seeded from an SA small-business template.
- Add `vat_code` to transactions, plus `Journal` / `JournalLine` for adjustments and opening balances.
- Roles: `owner`, `bookkeeper`, `client` (read-only portal).

## 5. Investing tracker (side module)

**For:** the accountant's own money. He invests with EasyEquities, likes property and some higher-risk shares. The portfolio started around R30k and is roughly 2–2.5× that now.

**Constraints:**
- EasyEquities has **no official public API**. Unofficial scrapers break and may breach their terms, so imports use their statements and exports, or manual entry.
- Showing prices and history is fine, but **personalised advice needs an FSP licence (FAIS Act)**. The tool stays a tracker and research aid for his own use, with a clear "not financial advice" note.

**Features:**
1. **Holdings tracker**:
   - Import EasyEquities transaction history or holdings (CSV, PDF or manual entry) and track cost basis.
   - Show returns vs benchmarks: JSE Top 40, the SA property index or Satrix Property ETF, USD/ZAR and CPI.
   - Show concentration risk.
2. **Property focus**:
   - Watchlist for JSE REITs (e.g. Growthpoint, Redefine, Fortress) and property ETFs, with EasyProperties listings added manually.
   - Metrics: yield, NAV discount and distribution dates.
   - Physical property: valuation, bond, rental yield.
3. **Watchlist and alerts**: prices come from yfinance using `.JO` tickers. Alerts go by email or WhatsApp on moves, dividends and results dates.
4. **SA macro event scorecard**:
   - Reuses the forex-news event-study and scoring loop, retargeted to SARB MPC decisions, SA CPI, USD/ZAR and US CPI/NFP effects on the rand.
   - Every call is logged, then scored after the event. The hit rate is shown honestly.

**Reused from forex-news:** `analysis/event_study.py`, `core/predictions.py` (scoring), `events/recurrence.py`, `reports/narrative.py` (Groq), the SerpAPI news fetcher and the SAM worker pattern.

**Not reused:** the HuggingFace NLP methods (`nlp/`), the multiple duplicate PDF generators, COT data and anything specific to FX pairs.

This module lives in `backend/app/invest/` behind a feature flag. It shares auth but is only visible to users we enable. If it grows, it can split into its own repo later.

## 6. Architecture

It stays mostly as it is today:
- **API Lambda + worker Lambda** (SAM), **Cloudflare Pages** frontend, and **MySQL/Postgres** (Aiven).
- **Region**: move to AWS `af-south-1` (Cape Town). Client financial data then stays in SA (POPIA), and latency drops. Aiven also has SA regions.
- **Storage**: statements are kept in S3, encrypted, and linked to their transactions (evidence for audits). Retention is 5 years, per SARS record-keeping rules.
- **AI**: Groq only, with rotating keys. The AI suggests, a human approves, and nothing posts automatically.
- **POPIA**:
  - Log every action in an audit log.
  - Encrypt statement passwords at rest (today they're stored as plaintext).
  - Scope all data by client.
  - Put a data processing agreement in place between Vicinic and CTHAI.

## 7. Roadmap

| Phase | Deliverable | Rough size |
|---|---|---|
| **0. Discovery + cleanup** | Answer the open questions with CTHAI. Collect sample statements from their top banks. Do the section 4 removals. Deploy the current app so they can try it. | 1 week |
| **1. Multi-client foundation** | Practice, Client, roles. Per-client chart of accounts. Data scoped per client. Parsers for FNB, ABSA and Standard Bank. Statement balance checks. | 2–3 weeks |
| **2. Recon workbench** | Month grid for each client × month. Bulk approval of categorizations with rule learning. Period close and lock. Duplicate detection. | 2 weeks |
| **3. Reports** | P&L, balance sheet, TB, GL, VAT201 schedule, management pack PDF, Excel export. | 2–3 weeks |
| **4. Practice dashboard** | Deadlines, late statements, transaction volume vs package tier. | 1 week |
| **5. Pilot with CTHAI** | 5–10 real clients for one month-end cycle, then fix what hurts. | 1 month |
| **Invest track** (parallel, low priority) | Holdings import, benchmarks and property watchlist first. Macro scorecard second. | 2 weeks |
| **Later** | Investec feed, client portal, WhatsApp statement upload (reusing the BHKA bot pattern), ERPNext/Sage/Xero export. | — |

**Success measure:** CTHAI closes a month for a Core SME client in under 30 minutes of bookkeeper time, with a reconciled VAT schedule.

## 8. Commercial

Pricing mirrors CTHAI's own tiers, so the tool's cost scales with what the practice earns per client. Proposal:
- A flat practice fee, plus a small fee per active client per month.
- CTHAI is the design partner: free or discounted during the pilot, in exchange for feedback and a case study.
- Vicinic owns the product and can resell it to other small practices.

## 9. Open questions

**For CTHAI:**
1. What do you use today for the books: Sage, Xero, Excel, ERPNext or something else? Do the reports need to match it, or can this app replace it?
2. Which banks do most of your clients use? Can we get 2–3 anonymised statements per bank?
3. How do clients send statements today: email, WhatsApp, or you download them?
4. How many active clients, and how many are VAT-registered?
5. Which reports do clients actually receive? Please share a sample management pack.
6. Do you work cash basis, or do you need debtors and creditors (invoices) as well?
7. Who else in the practice would use the app, and what roles do they have?
8. Investing: can you export your EasyEquities transaction history? Which property holdings (REITs, EasyProperties, physical property) do you want tracked?

**For Vicinic (you):**
9. Vicinic details: branding, legal entity, and whether the product carries the Vicinic name or a white-label for CTHAI.
10. Budget for AWS af-south-1 and Aiven, and who pays during the pilot.
11. Hosting of the repo and CI after the GitHub ban: stay on GitHub with manual deploys, or move CI to GitLab (forex-news and cthai already use GitLab CI)?
