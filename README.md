# Colunimbus (L-Suite rewrite)

Financial automation for South African bank statements. It imports statements (Gmail, PDF, CSV), parses Capitec, TymeBank and GoTyme statements, categorizes transactions (keywords first, then Groq AI), posts them to ERPNext as journal entries, reconciles them against ERPNext month by month, and syncs ERPNext invoices.

See [docs/PLAN.md](docs/PLAN.md) for the product plan and roadmap.

```
backend/    FastAPI + SQLAlchemy on AWS Lambda (SAM template)
frontend/   Plain HTML/CSS/JS on Cloudflare Pages (no build step)
```

## Architecture

| Piece | Where it runs | Notes |
|---|---|---|
| `ApiFunction` (`app.main.handler`) | Lambda + Function URL | JSON API, JWT auth, CORS for the Pages domain |
| `WorkerFunction` (`app.worker.handler`) | Lambda, invoked asynchronously | PDF imports, AI categorization, full ERPNext sync, nightly categorize (03:00 SAST) |
| `UploadsBucket` | S3 | Stages PDF uploads for the worker. Objects expire after 1 day |
| Database | Any MySQL, Postgres or SQLite URL | Table names match the old Django schema, so an existing database works as is |
| Frontend | Cloudflare Pages | `js/config.js` points at the API |

Long-running jobs used to be sent to GitHub Actions. They now run on the worker Lambda and are tracked in the `lsuite_jobs` table. The API doesn't depend on GitHub at all.

### Moving over from the old Django app
- Point `DATABASE_URL` at the old database. On cold start, the only table created is `lsuite_jobs`. Existing tables are left untouched.
- Old `pbkdf2_sha256` password hashes still verify, so users keep their passwords.
- Old API paths with trailing slashes still resolve.

## Local dev

```bash
cd backend
pip install -r requirements-dev.txt
cp .env.example .env
python -m app.cli seed-categories
uvicorn app.main:app --reload --port 8000     # docs at /docs when DEBUG=true
pytest -q
```

```bash
cd frontend
npx wrangler pages dev . --port 8788          # js/config.js uses localhost:8000 automatically
```

Without `WORKER_FUNCTION_NAME` set, background jobs run in a local thread.

### CLI (`python -m app.cli <cmd>`)
`init-db`, `seed-categories [--overwrite]`, `auto-categorize [--user N] [--ai]`, `recategorize [--all]`, `cleanup-categories`, `erpnext-sync [--user N] [--dry-run] [--limit N]`, `sync-invoices [--year Y --month M]`

## Deploy

**Backend (AWS SAM)**
```bash
cd backend
sam build            # container build, see samconfig.toml
sam deploy --guided  # prompts for SecretKey, DatabaseUrl, FrontendUrl, Google/Groq/SMTP params
```
The stack prints these outputs:
- `ApiUrl`: put it in `frontend/js/config.js` as `PROD_API` (no trailing slash).
- `GmailRedirectUri`: add it as an authorized redirect URI in Google Cloud Console. Enable the Gmail API.
- For social login, also register `<ApiUrl>auth/social/google/callback` (and the `github` and `facebook` versions if you use them) with each provider.

**Frontend (Cloudflare Pages)**
Either connect the repo in the Pages dashboard (root directory `frontend`, no build command, output `.`), or run:
```bash
cd frontend && npx wrangler pages deploy .
```
Set `FrontendUrl` (and `CorsAllowedOrigins` if you use more than one domain) on the stack to the Pages URL.

## Environment / SAM parameters

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | JWT signing key, at least 32 characters |
| `DATABASE_URL` | `mysql://…`, `postgres://…` or `sqlite:///…`. Aiven `?ssl-mode=` is handled |
| `FRONTEND_URL` / `CORS_ALLOWED_ORIGINS` | Pages URL for CORS and OAuth redirects back |
| `API_URL` | Only needed behind a custom domain. The Function URL is detected automatically |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Gmail import and Google login |
| `GITHUB_*`, `FACEBOOK_*` | Optional social logins |
| `GROQ_API_KEYS` | Comma-separated. Keys rotate on 429 errors. `GROQ_MODEL` defaults to `llama-3.3-70b-versatile` |
| `EMAIL_*`, `DEFAULT_FROM_EMAIL` | SMTP for password-reset mails. When unset, mails are only logged |

Limits: request bodies to a Function URL max out at about 6 MB, so upload large PDF batches in chunks.
