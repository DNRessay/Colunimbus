# C.T.H.A.I

(Repo, AWS stack and Pages project are still named `colunimbus`.)

Bookkeeping front-end for ERPNext, built for a group of South African companies. It:
- imports bank statements (PDF, CSV, Gmail)
- categorizes transactions (keywords, then Groq AI)
- posts them to each company's ERPNext books as journal entries
- reconciles each month against ERPNext
- flags money moving between the group's companies

Product plan and roadmap: [docs/PLAN.md](docs/PLAN.md).

```
backend/    FastAPI + SQLAlchemy on AWS Lambda (SAM template, eu-west-1)
frontend/   Plain HTML/CSS/JS on Cloudflare Pages (no build step)
```

## How it's organised

- **Organisation**: the group using the app. Created at sign-up. The owner adds team members under Settings.
- **Company**: one business in the group, e.g. the sales company or the building company.
  - Has its own bank accounts, transactions, statements, reconciliation and invoices.
  - Links to one Company in the group's single ERPNext site.
  - The frontend sends the selected company as the `X-Client-Id` header. In code, a company is a `Client`.
- **Categories**: shared by all companies, so merchant keywords learned once help everywhere. The ERPNext account a category posts to is set per company.

| Piece | Where it runs | Notes |
|---|---|---|
| `ApiFunction` (`app.main.handler`) | Lambda + Function URL | JSON API, JWT auth, CORS for the Pages domain |
| `WorkerFunction` (`app.worker.handler`) | Lambda, invoked asynchronously | PDF imports, AI categorization, ERPNext sync, nightly categorize (03:00 SAST) |
| `UploadsBucket` | S3 | Stages PDF uploads for the worker. Objects expire after 1 day |
| Database | Neon Postgres (any Postgres, MySQL or SQLite URL works) | Tables are created on first start |
| Frontend | Cloudflare Pages | `js/config.js` points at the API |

## Local dev

```bash
cd backend
pip install -r requirements-dev.txt
cp .env.example .env                           # SQLite by default
uvicorn app.main:app --reload --port 8000      # docs at /docs when DEBUG=true
pytest -q
```

```bash
cd frontend
npx wrangler pages dev . --port 8788           # js/config.js uses localhost:8000 automatically
```

Sign up at `/register.html`, then add your companies. Without `WORKER_FUNCTION_NAME` set, background jobs run in a local thread.

### CLI (`python -m app.cli <cmd>`)
- `init-db`
- `seed-categories [--overwrite]`
- `auto-categorize [--client N] [--ai]`
- `recategorize [--client N]`
- `cleanup-categories`
- `erpnext-sync [--client N] [--dry-run] [--limit N]`
- `sync-invoices [--client N] [--year Y --month M]`

## Deploy

**Automatic (GitHub Actions):** every push to `main` that touches `backend/` or `frontend/` runs `.github/workflows/deploy.yml`. It runs the tests, deploys the SAM stack `colunimbus-api` to eu-west-1, deploys the Pages project `colunimbus`, and smoke-tests the API. You can also run it by hand from the Actions tab.
- **Required repo secrets:** `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `LSUITE_DATABASE_URL`.
- **Optional repo secrets:** `LSUITE_GOOGLE_CLIENT_ID`, `LSUITE_GOOGLE_CLIENT_SECRET`, `LSUITE_GROQ_API_KEYS`.
- **App secret key:** created on the first run and kept in AWS SSM (`/colunimbus/secret-key`). Don't delete it.

The manual steps below do the same by hand.


**Database (Neon)**
1. Create a project in Neon's Frankfurt or London region.
2. Copy the **pooled** connection string (the host contains `-pooler`). It looks like `postgresql://user:pass@ep-xxx-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require`.

**Backend (AWS SAM, eu-west-1)**
```bash
cd backend
sam build
sam deploy --guided   # asks for SecretKey (32+ chars), DatabaseUrl (Neon), FrontendUrl, Google/Groq/SMTP
```
The stack prints these outputs:
- `ApiUrl`: put it in `frontend/js/config.js` as `PROD_API` (no trailing slash).
- `GmailRedirectUri`: add it in Google Cloud Console as an authorized redirect URI, and enable the Gmail API. For Google sign-in, also add `<ApiUrl>auth/social/google/callback`.

**Frontend (Cloudflare Pages)**
Connect the repo in the Pages dashboard (root directory `frontend`, no build command, output `.`), or run:
```bash
cd frontend && npx wrangler pages deploy .
```
Set the stack's `FrontendUrl` to the Pages URL.

**ERPNext**
1. In ERPNext, create an API key and secret for a user that can create Journal Entries.
2. In the app, add the connection under **Settings**.
3. On **Companies**, link each company to its ERPNext Company.
4. Map accounts on the **ERPNext sync** page.

## Environment / SAM parameters

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Signs JWTs and encrypts stored secrets (ERPNext API key/secret, PDF passwords). At least 32 characters. Keep it stable: changing it makes stored secrets unreadable |
| `DATABASE_URL` | Neon `postgresql://…?sslmode=require`. `mysql://` and `sqlite:///` also work |
| `FRONTEND_URL` / `CORS_ALLOWED_ORIGINS` | Pages URL, for CORS and OAuth redirects back |
| `API_URL` | Only needed behind a custom domain. The Function URL is detected automatically |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Gmail import and Google sign-in |
| `GROQ_API_KEYS` | Comma-separated, rotated on rate limits. `GROQ_MODEL` defaults to `llama-3.3-70b-versatile` |
| `EMAIL_*`, `DEFAULT_FROM_EMAIL` | SMTP for password-reset mails. When unset, mails are only logged |

Limits: request bodies to a Function URL max out at about 6 MB, so upload large PDF batches in chunks.

## License

Proprietary. Copyright (c) 2026 VICINIC (Pty) Ltd. All rights reserved. Used by partner CT Holdings and Investments (Pty) Ltd under the licence in [LICENSE](LICENSE).
